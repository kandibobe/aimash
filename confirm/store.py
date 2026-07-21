"""SQLAlchemy-стор черновиков (proposals) + audit-журнал. Реализует протокол confirm_store
(async claim/finalize), который ждут ads/mutations.apply_*.

Поток безопасности (жизненный цикл черновика):
  save_proposal (pending) → confirm (confirmed) → claim (executing, АТОМАРНО и ОДНОРАЗОВО)
  → выполнить SDK → finalize (applied) | record_failure (failed). Reject → rejected.

`claim` — ключ к защите от ПОВТОРНОГО выполнения (replay/double-spend): перевод
confirmed→executing идёт одним атомарным UPDATE … WHERE status='confirmed' (compare-and-set),
поэтому второй вызов (ретрай, гонка, второй воркер) получит rowcount=0 и НЕ выполнит мутацию.

TTL — тоже в CAS, а не в фоновой джобе (Волна 1.2). Раньше срок жизни черновика энфорсил ровно
один энфорсер — `scheduler.jobs.cleanup_stale_proposals`; джоба не поднялась/упала/отстала на час
(интервал `CLEANUP_INTERVAL_MINUTES`) ⇒ вчерашний черновик оставался исполним бессрочно, и человек
подтверждал «было→станет», снятое неизвестно когда. Возраст теперь стоит в `WHERE` у `confirm` и
`claim`: просроченный черновик не совпадает по условию → rowcount=0 → False/None, без audit-строки
и без вызова SDK. Джоба осталась уборщиком (перевести в `rejected`, освободить временные медиа) —
гардом она больше не является. Это не заменяет freshness-recheck (сверку снимка с живым Google Ads,
Волна 1.1): TTL ограничивает ВОЗРАСТ подтверждения, freshness — РАСХОЖДЕНИЕ данных.

Провенанс хода штампует САМ стор (Волна 1.4): `origin_human_turn`/`author_user_id`/`run_id` берутся
из `core.provenance`, аргументов для них у `save_proposal` нет. `user_initiated` — по-прежнему
аргумент, и в headless-контуре его напишет вызывающий (MCP-инструмент, cron, self-improvement-форк);
второй бит писать некому — его поднимает только доверенный вход. Денежные `apply_*` требуют оба.

Хранилище — db.models (Proposal/AuditLog) на движке из DATABASE_URL (dev: SQLite). Секретов тут нет.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

from sqlalchemy import CursorResult, func, select, update

from core.config import settings
from core.logging import log, redact_text
from core.provenance import get_provenance
from db.models import AuditLog, Proposal
from db.session import Session, db_dt

# Потолок размера JSON результата в audit_log.result (защита от раздувания: некоторые мутации
# возвращают длинные списки resource_name'ов — add_keywords во многих группах и т.п.). Зеркалит
# усечение error_events.traceback (core.errors._TB_MAX). Усекаем длинные списки/строки, не теряя
# структуру (count/applied остаются).
_RESULT_MAX = 4000


def _cap_result(result: object) -> object:
    """Ограничить размер result для audit_log. Если JSON ≤ потолка — как есть. Иначе усекаем длинные
    списки до первых 10 + счётчик и длинные строки до 500 символов, помечаем `_truncated`."""
    if result is None:
        return None
    try:
        s = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 — несериализуемое не должно ронять запись audit
        return {"_unserializable": True}
    if len(s) <= _RESULT_MAX:
        return result
    if isinstance(result, dict):
        capped: dict = {}
        for k, v in result.items():
            if isinstance(v, list) and len(v) > 10:
                capped[k] = list(v[:10]) + [f"…ещё {len(v) - 10}"]
            elif isinstance(v, str) and len(v) > 500:
                capped[k] = v[:500] + "…"
            else:
                capped[k] = v
        capped["_truncated"] = True
        return capped
    return {"_truncated": True, "preview": s[:_RESULT_MAX]}


def _ttl_boundary() -> datetime:
    """Граница возраста черновика для CAS-условий `confirm`/`claim`: созданные РАНЬШЕ неё мертвы.

    Значение живое (`settings.proposal_ttl_hours`, env `PROPOSAL_TTL_HOURS`) — тот же источник, что
    у джобы-уборщика и у текста карточки (`{ttl_h}`), иначе человек видел бы один срок, а код
    применял другой. `db_dt` приводит границу к диалекту (SQLite хранит наивный UTC, Postgres —
    timestamptz); сравнение naive-колонки с tz-aware границей на SQLite молча даёт мусор.

    `ttl_hours <= 0` НЕ означает «TTL выключен»: граница уезжает в будущее и подтверждать становится
    нечего — отказ, а не проход (правило 10). Выключателя тут нет намеренно: «отключить срок жизни
    подтверждения» — это и есть та дыра, ради которой эпик заведён."""
    return datetime.now(timezone.utc) - timedelta(hours=int(settings.proposal_ttl_hours))


@dataclass
class ConfirmedProposal:
    """Лёгкий снимок черновика для гейтов apply_* и оркестратора."""

    operation: str
    status: str
    user_initiated: bool
    params: dict
    customer_id: str
    summary: str
    chat_id: int
    # Волна 1.4 — второй, независимый бит провенанса (см. save_proposal). Дефолт False, потому что
    # позиционное конструирование в старом коде/тестах не должно молча выдавать «это был человек»:
    # отсутствие сведений о провенансе = машинный ход = отказ на денежной операции (правило 10).
    origin_human_turn: bool = False


class ConfirmStore:
    """confirm_store на SQLAlchemy. Все методы async (apply_* их await-ят)."""

    async def save_proposal(
        self,
        *,
        confirmation_id: str,
        operation: str,
        customer_id: str,
        params: dict,
        summary: str,
        chat_id: int,
        user_initiated: bool = False,
    ) -> None:
        """Создать черновик (pending). Провенанс хода — `origin_human_turn`/`author_user_id`/`run_id`
        — стор берёт САМ из `core.provenance`, и параметра для него нет намеренно (Волна 1.4).

        `user_initiated` остаётся аргументом ради обратной совместимости вызывающих, но одного его
        мало: аргумент — это то, что напишет вызывающий, а в headless-контуре вызывающим станет
        MCP-инструмент, cron-джоба или self-improvement-форк. Второй бит подделать нечем: поднять
        его может только вход в `human_turn(...)`, а его открывает единственное место — доверенный
        слой Telegram (`bot.main.WhitelistMiddleware`), уже установивший, что апдейт пришёл от
        живого человека из whitelist в private-чате. Денежные `apply_*` требуют ОБА бита."""
        prov = get_provenance()
        async with Session() as s:
            s.add(
                Proposal(
                    confirmation_id=confirmation_id,
                    operation=operation,
                    customer_id=customer_id,
                    summary=summary,
                    params=params,
                    chat_id=chat_id,
                    user_initiated=user_initiated,
                    origin_human_turn=prov.human_turn,
                    author_user_id=prov.actor_user_id,
                    run_id=prov.run_id[:16],  # 8 hex; срез — страховка от чужого длинного id
                    status="pending",
                )
            )
            await s.commit()

    async def get_confirmed(self, confirmation_id: str) -> ConfirmedProposal | None:
        async with Session() as s:
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one_or_none()
            if p is None:
                return None
            return ConfirmedProposal(
                operation=p.operation,
                status=p.status,
                user_initiated=p.user_initiated,
                params=p.params,
                customer_id=p.customer_id,
                summary=p.summary,
                chat_id=p.chat_id,
                origin_human_turn=p.origin_human_turn,
            )

    async def load_proposals(self, confirmation_ids: list[str]) -> dict[str, ConfirmedProposal]:
        """Пакетно снять черновики по списку confirmation_id ОДНИМ запросом (Доп.2B: /journal решает,
        какие applied-строки обратимы, без N+1 обращений). Read-only. Отсутствующие id просто не
        попадут в словарь. Пустой/фейковый список → {}."""
        ids = [c for c in confirmation_ids if c]
        if not ids:
            return {}
        async with Session() as s:
            rows = (
                (await s.execute(select(Proposal).where(Proposal.confirmation_id.in_(ids))))
                .scalars()
                .all()
            )
        return {
            p.confirmation_id: ConfirmedProposal(
                operation=p.operation,
                status=p.status,
                user_initiated=p.user_initiated,
                params=p.params,
                customer_id=p.customer_id,
                summary=p.summary,
                chat_id=p.chat_id,
                origin_human_turn=p.origin_human_turn,
            )
            for p in rows
        }

    async def claim(self, confirmation_id: str, *, operation: str) -> ConfirmedProposal | None:
        """Атомарно «застолбить» подтверждённый черновик под исполнение: confirmed → executing
        (одноразово, с проверкой операции). Возвращает снимок, если застолбил, иначе None.

        Это authoritative-гейт исполнения: один UPDATE … WHERE status='confirmed' AND operation=…
        (compare-and-set). Второй вызов с тем же confirmation_id (повтор/гонка/второй воркер)
        не совпадёт по WHERE → rowcount=0 → None → мутация не выполнится (защита от double-spend).
        Несовпадение operation тоже даёт None — confirmation_id привязан к КОНКРЕТНОЙ операции.

        Возраст (`created_at >= TTL-граница`) — часть того же WHERE, а не отдельная проверка после
        выборки: между «прочитали и увидели, что свежий» и «застолбили» проходит время, и на
        Postgres в него влезает параллельный воркер. Второй рубеж после `confirm` нужен затем, что
        подтверждение и исполнение — разные моменты: процесс мог упасть между ними и подняться
        назавтра, найдя черновик в статусе 'confirmed'. Просрочка ⇒ None ⇒ `PermissionError` в
        `ads.mutations._require_confirmation` — SDK не вызывается вовсе."""
        async with Session() as s:
            res = await s.execute(
                update(Proposal)
                .where(
                    Proposal.confirmation_id == confirmation_id,
                    Proposal.operation == operation,
                    Proposal.status == "confirmed",
                    Proposal.created_at >= db_dt(_ttl_boundary()),  # TTL в CAS, не в джобе
                )
                .values(status="executing", decided_at=func.now())
            )
            # cast: DML возвращает CursorResult (есть .rowcount); async-стаб видит общий Result.
            if cast(CursorResult, res).rowcount != 1:  # не застолбили (нет/не confirmed/чужая/взят)
                await s.rollback()
                return None
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one()
            snap = ConfirmedProposal(
                operation=p.operation,
                status=p.status,  # "executing"
                user_initiated=p.user_initiated,
                params=p.params,
                customer_id=p.customer_id,
                summary=p.summary,
                chat_id=p.chat_id,
                origin_human_turn=p.origin_human_turn,
            )
            await s.commit()
            return snap

    async def confirm(
        self,
        confirmation_id: str,
        *,
        chat_id: int,
        actor_user_id: int | None = None,
        actor_username: str | None = None,
    ) -> bool:
        """pending → confirmed (АТОМАРНО, одноразово). True если перевёл. Пишет audit.

        Compare-and-set (как claim): один UPDATE … WHERE status='pending'. Защита от TOCTOU при
        двойной доставке ✅ (Telegram может прислать callback дважды): второй параллельный confirm
        не совпадёт по WHERE → rowcount=0 → False, без второй audit-строки и без второго запуска
        execute_confirmed. На SQLite (dev) single-writer и так исключает гонку; на Postgres — нет.
        actor_user_id/username — «кто» нажал ✅ (§12), фиксируется в audit-строке решения.

        Гард ВЛАДЕНИЯ (мультиоператор): в WHERE добавлен `chat_id == proposal.chat_id` — подтвердить
        черновик может ТОЛЬКО его владелец. Чужой chat_id (утёкший/угаданный confirmation_id) не
        совпадёт по WHERE → False, неотличимо от устаревшего (безопасный generic-UX). В одно-
        операторном режиме chat_id всегда совпадает → поведение не меняется (fail-closed, аддитивно).

        Гард ВОЗРАСТА (Волна 1.2): `created_at >= TTL-граница` в том же WHERE. Просроченная карточка
        (кнопки в Telegram живут вечно, даже когда джоба-уборщик не отработала) даёт False —
        и вызывающий уже показывает на него текст «Черновик не найден или устарел» (i18n `stale`),
        менять UX не нужно."""
        async with Session() as s:
            res = await s.execute(
                update(Proposal)
                .where(
                    Proposal.confirmation_id == confirmation_id,
                    Proposal.status == "pending",
                    Proposal.chat_id == chat_id,  # владение: только владелец черновика
                    Proposal.created_at >= db_dt(_ttl_boundary()),  # TTL в CAS, не в джобе
                )
                .values(status="confirmed", decided_at=func.now())
            )
            # cast: DML возвращает CursorResult (есть .rowcount); async-стаб видит общий Result.
            if cast(CursorResult, res).rowcount != 1:  # нет/не pending/уже подтверждён/гонка
                await s.rollback()
                return False
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one()
            s.add(
                _audit(
                    p,
                    chat_id,
                    "confirmed",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
            )
            await s.commit()
            return True

    async def reject(
        self,
        confirmation_id: str,
        *,
        chat_id: int,
        actor_user_id: int | None = None,
        actor_username: str | None = None,
    ) -> bool:
        """pending → rejected (АТОМАРНО, одноразово). True если отклонил, иначе False.

        B2: compare-and-set (зеркало confirm), а НЕ select-then-modify — раньше это был единственный
        не-CAS переход статуса. Гонка ❌/✅ (Telegram может доставить оба колбэка почти одновременно;
        на Postgres нет single-writer): при select-then-modify reject мог прочитать status='pending'
        ДО коммита confirm и затем перезаписать уже confirmed/executing черновик в 'rejected'
        (пометив применяемую мутацию отменённой). Один UPDATE … WHERE status='pending' AND
        chat_id=? исключает это: второй/гоночный переход не совпадёт по WHERE → rowcount=0 → False,
        без спурьёзной audit-строки. Гард владения (chat_id) сохранён — отклонить может только
        владелец (cleanup_stale_proposals передаёт p.chat_id сам)."""
        async with Session() as s:
            res = await s.execute(
                update(Proposal)
                .where(
                    Proposal.confirmation_id == confirmation_id,
                    Proposal.status == "pending",
                    Proposal.chat_id == chat_id,  # владение: только владелец черновика
                )
                .values(status="rejected", decided_at=func.now())
            )
            # cast: DML возвращает CursorResult (есть .rowcount); async-стаб видит общий Result.
            if cast(CursorResult, res).rowcount != 1:  # нет/не pending/чужой/гонка
                await s.rollback()
                return False
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one()
            s.add(
                _audit(
                    p,
                    chat_id,
                    "rejected",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
            )
            await s.commit()
            return True

    async def finalize(self, confirmation_id: str, *, result: object) -> None:
        """Успех: executing → applied (терминальный) + audit applied. Терминальный статус
        не даёт повторно застолбить черновик (claim требует status='confirmed').

        Guard (зеркало record_failure): смену статуса И audit-строку applied пишем ТОЛЬКО из
        'executing' — нормального состояния после claim+успешного SDK. finalize вызывается лишь
        после успешного SDK за claim, поэтому любой иной статус здесь = рассинхрон потока: пишем
        log.warning и НЕ кладём спурьёзную applied-строку в журнал (раньше audit писался всегда,
        даже при статусе ≠ executing — асимметрия с record_failure)."""
        async with Session() as s:
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one_or_none()
            if p is None:
                return
            if p.status != "executing":
                log.warning(
                    "finalize() на черновике %s в статусе %s (ожидался executing) — "
                    "пропускаю запись applied в журнал",
                    confirmation_id,
                    p.status,
                )
                return
            p.status = "applied"
            p.decided_at = func.now()
            s.add(_audit(p, p.chat_id, "applied", result=result))
            await s.commit()

    async def mark_needs_review(self, confirmation_id: str, *, error: str) -> bool:
        """executing → needs_review (АТОМАРНО, одноразово; для реконсиляции зависших мутаций).

        Процесс упал ПОСЛЕ claim, посреди SDK-вызова: исход НЕИЗВЕСТЕН — изменение могло
        примениться в Google Ads. Поэтому НЕ 'failed' (это утверждало бы «не применено»), а
        честный терминальный needs_review: оператор сверяет аккаунт вручную. Терминальность
        конструктивна: claim требует 'confirmed', finalize — 'executing' → needs_review никем
        не «воскрешается» (replay-защита сохранена).

        CAS: UPDATE … WHERE status='executing' — параллельный finalize/record_failure ЖИВОГО
        процесса выигрывает гонку (rowcount=0 → False, без спурьёзной audit-строки).
        Пишет audit-строку 'needs_review' с редактированной ошибкой (golden rule #5)."""
        async with Session() as s:
            res = await s.execute(
                update(Proposal)
                .where(
                    Proposal.confirmation_id == confirmation_id,
                    Proposal.status == "executing",
                )
                .values(status="needs_review", decided_at=func.now())
            )
            # cast: DML возвращает CursorResult (есть .rowcount); async-стаб видит общий Result.
            if cast(CursorResult, res).rowcount != 1:  # живой процесс успел finalize/failed
                await s.rollback()
                return False
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one()
            s.add(_audit(p, p.chat_id, "needs_review", result={"error": redact_text(str(error))}))
            await s.commit()
            return True

    async def record_verification(self, confirmation_id: str, *, verification: dict) -> bool:
        """Доп.2A: пост-проверка применённой мутации разошлась с ожидаемым «станет» →
        applied → needs_review (АТОМАРНО) + audit-строка needs_review со сводкой сверки.
        Вызывается ТОЛЬКО при verified=False.

        CAS: UPDATE … WHERE status='applied' — переводим лишь из ТЕРМИНАЛЬНОГО applied (норма
        сразу после finalize). Иной статус (реконсиляция/гонка) ⇒ rowcount≠1 → False, без
        спурьёзной audit-строки. Терминальность needs_review сохраняет replay-защиту (claim
        требует 'confirmed'). Значения сверки — числа/enum'ы (не секреты), но текст всё равно
        прогоняем через redact_text (единая дисциплина границы БД, golden rule #5)."""
        exp, act = verification.get("expected"), verification.get("actual")
        msg = redact_text(f"пост-проверка не сошлась: применено {act}, ожидалось {exp}")
        async with Session() as s:
            res = await s.execute(
                update(Proposal)
                .where(
                    Proposal.confirmation_id == confirmation_id,
                    Proposal.status == "applied",
                )
                .values(status="needs_review", decided_at=func.now())
            )
            # cast: DML возвращает CursorResult (есть .rowcount); async-стаб видит общий Result.
            if cast(CursorResult, res).rowcount != 1:
                await s.rollback()
                return False
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one()
            s.add(
                _audit(
                    p,
                    p.chat_id,
                    "needs_review",
                    result={"error": msg, "verification": verification},
                )
            )
            await s.commit()
            return True

    async def mark_confirmed_failed(self, confirmation_id: str, *, error: str) -> bool:
        """confirmed → failed (АТОМАРНО, одноразово; для реконсиляции A6 «завис в confirmed»).

        Процесс упал в окне МЕЖДУ confirm() (pending→confirmed) и claim() (confirmed→executing,
        внутри apply_* перед самим SDK-вызовом). Пока статус 'confirmed', SDK НЕ вызывался — в
        отличие от 'executing' (mark_needs_review, там исход неизвестен). Поэтому здесь безопасен
        честный 'failed' («не применено»): оператор просто повторяет команду.

        CAS: UPDATE … WHERE status='confirmed' — гонку с ЖИВЫМ claim выигрывает тот, кто первый:
        claim победил → 'executing', наш rowcount=0 → False (не трогаем исполняемое); мы победили →
        'failed', последующий claim живого процесса даст rowcount=0 → None → мутация не выполнится
        (double-spend невозможен). Порог ≫ времени до claim (секунды) → живой процесс не зацепим.
        Пишет audit-строку 'failed' с редактированной ошибкой (golden rule #5)."""
        async with Session() as s:
            res = await s.execute(
                update(Proposal)
                .where(
                    Proposal.confirmation_id == confirmation_id,
                    Proposal.status == "confirmed",
                )
                .values(status="failed", decided_at=func.now())
            )
            # cast: DML возвращает CursorResult (есть .rowcount); async-стаб видит общий Result.
            if cast(CursorResult, res).rowcount != 1:  # живой процесс успел claim/иное
                await s.rollback()
                return False
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one()
            s.add(_audit(p, p.chat_id, "failed", result={"error": redact_text(str(error))}))
            await s.commit()
            return True

    async def record_failure(self, confirmation_id: str, *, error: str) -> None:
        """Ошибка выполнения → failed (терминальный) + audit failed.

        Переводим в failed и из 'executing' (упало после claim), и из 'confirmed' (упало ДО claim,
        напр. резолв имени) — чтобы статус черновика и audit-строка совпадали (без рассинхрона
        'confirmed' vs audit 'failed'). Уже терминальные applied/failed/rejected НЕ трогаем
        (нельзя «понизить» успешно применённую операцию). SDK при ошибке до claim не вызывался —
        повтор = новая команда (тех же кнопок у старого черновика уже нет)."""
        async with Session() as s:
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one_or_none()
            if p is not None:
                if p.status in ("confirmed", "executing"):
                    p.status = "failed"
                    p.decided_at = func.now()
                # Авторитетная редакция на границе БД (golden rule #5): str(e) от SDK/google.auth
                # может нести креды; редактируем здесь, чтобы НИ один вызывающий (бот, dev-скрипты,
                # будущий код) не записал секрет в audit_log. redact_text идемпотентен.
                s.add(_audit(p, p.chat_id, "failed", result={"error": redact_text(str(error))}))
                await s.commit()


def _audit(
    p: Proposal,
    chat_id: int,
    status: str,
    result: object = None,
    *,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
) -> AuditLog:
    return AuditLog(
        confirmation_id=p.confirmation_id,
        operation=p.operation,
        customer_id=p.customer_id,
        chat_id=chat_id,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        status=status,
        result=_cap_result(result),  # потолок размера (защита audit_log от раздувания)
    )


@dataclass
class AuditEvent:
    """Одна строка журнала изменений для показа (ТЗ §12: что/когда/кто/результат). Без секретов."""

    created_at: datetime | None
    status: str  # applied | failed | rejected | needs_review
    operation: str
    actor_user_id: int | None
    actor_username: str | None
    result: dict | None
    confirmation_id: str
    # Доп.2B: чат-владелец строки — для персистентного «↩️ Откатить» из /journal только СВОИХ
    # применённых операций (fail-closed на клике). Дефолт 0 = совместимость с позиционным
    # конструированием (тесты/легаси) без chat_id.
    chat_id: int = 0


async def list_recent_audit(limit: int = 15) -> list[AuditEvent]:
    """Последние ЗНАЧИМЫЕ события журнала (applied/failed/rejected/needs_review) — «что и когда
    изменилось» (ТЗ §12/§18), reverse-chron. «Кто» для applied/failed (там actor=NULL — см.
    db.models.AuditLog) восстанавливаем из связанной по confirmation_id строки confirmed/rejected,
    где actor записан. Read-only, секретов нет (result отредактирован на записи)."""
    limit = max(1, min(int(limit), 50))
    async with Session() as s:
        rows = list(
            (
                await s.execute(
                    select(AuditLog)
                    .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                    .limit(limit * 4)  # запас, т.к. confirmed-строки отфильтруются
                )
            )
            .scalars()
            .all()
        )
    # actor по confirmation_id из строк, где он есть (confirmed/rejected пишут actor).
    actor: dict[str, tuple[int | None, str | None]] = {}
    for r in rows:
        if (r.actor_user_id is not None or r.actor_username) and r.confirmation_id not in actor:
            actor[r.confirmation_id] = (r.actor_user_id, r.actor_username)
    out: list[AuditEvent] = []
    for r in rows:
        if r.status not in ("applied", "failed", "rejected", "needs_review"):
            continue
        au, an = r.actor_user_id, r.actor_username
        if au is None and not an:  # applied/failed: actor восстановим из confirmed-строки
            au, an = actor.get(r.confirmation_id, (None, None))
        out.append(
            AuditEvent(
                r.created_at,
                r.status,
                r.operation,
                au,
                an,
                r.result,
                r.confirmation_id,
                chat_id=int(r.chat_id or 0),
            )
        )
        if len(out) >= limit:
            break
    return out


async def audit_activity_since(days: int) -> dict:
    """Сводка активности за последние `days` дней (для еженедельного дайджеста, §6/§15): счётчики
    строк audit_log по статусам + число созданных кампаний (applied create_*_campaign). Read-only,
    секретов нет.

    Окно режется в SQL (db.db_dt приводит границу к диалекту: наивный UTC на SQLite, tz-aware на
    Postgres). Раньше фильтр был в Python — и дайджест тянул в память ВЕСЬ audit_log, который растёт
    с каждой мутацией: на живом аккаунте это единственный запрос без границы."""
    from datetime import timedelta, timezone

    start = datetime.now(timezone.utc) - timedelta(days=int(days))
    async with Session() as s:
        rows = list(
            (
                await s.execute(
                    select(AuditLog.status, AuditLog.operation).where(
                        AuditLog.created_at >= db_dt(start)
                    )
                )
            ).all()
        )
    statuses: dict[str, int] = {}
    created_campaigns = 0
    for status, operation in rows:
        statuses[status] = statuses.get(status, 0) + 1
        if (
            status == "applied"
            and str(operation or "").startswith("create_")
            and str(operation).endswith("_campaign")
        ):
            created_campaigns += 1
    return {"statuses": statuses, "created_campaigns": created_campaigns}


async def audit_applied_by_account_since(days: int) -> dict[str, int]:
    """3.3: число ПРИМЕНЁННЫХ мутаций per customer_id за последние `days` дней — для строки
    «за сутки применено: N» и тихого режима планового дайджеста (C2: вызывающий суммирует только
    по видимым оператору аккаунтам). Read-only, один GROUP BY, секретов нет."""
    from datetime import timedelta, timezone

    start = datetime.now(timezone.utc) - timedelta(days=int(days))
    async with Session() as s:
        rows = (
            await s.execute(
                select(AuditLog.customer_id, func.count())
                .where(AuditLog.created_at >= db_dt(start), AuditLog.status == "applied")
                .group_by(AuditLog.customer_id)
            )
        ).all()
    return {str(cid): int(n) for cid, n in rows}
