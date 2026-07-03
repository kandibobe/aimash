"""SQLAlchemy-стор черновиков (proposals) + audit-журнал. Реализует протокол confirm_store
(async claim/finalize), который ждут ads/mutations.apply_*.

Поток безопасности (жизненный цикл черновика):
  save_proposal (pending) → confirm (confirmed) → claim (executing, АТОМАРНО и ОДНОРАЗОВО)
  → выполнить SDK → finalize (applied) | record_failure (failed). Reject → rejected.

`claim` — ключ к защите от ПОВТОРНОГО выполнения (replay/double-spend): перевод
confirmed→executing идёт одним атомарным UPDATE … WHERE status='confirmed' (compare-and-set),
поэтому второй вызов (ретрай, гонка, второй воркер) получит rowcount=0 и НЕ выполнит мутацию.

Хранилище — db.models (Proposal/AuditLog) на движке из DATABASE_URL (dev: SQLite). Секретов тут нет.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import CursorResult, func, select, update

from core.logging import log, redact_text
from db.models import AuditLog, Proposal
from db.session import Session

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
            )

    async def claim(self, confirmation_id: str, *, operation: str) -> ConfirmedProposal | None:
        """Атомарно «застолбить» подтверждённый черновик под исполнение: confirmed → executing
        (одноразово, с проверкой операции). Возвращает снимок, если застолбил, иначе None.

        Это authoritative-гейт исполнения: один UPDATE … WHERE status='confirmed' AND operation=…
        (compare-and-set). Второй вызов с тем же confirmation_id (повтор/гонка/второй воркер)
        не совпадёт по WHERE → rowcount=0 → None → мутация не выполнится (защита от double-spend).
        Несовпадение operation тоже даёт None — confirmation_id привязан к КОНКРЕТНОЙ операции."""
        async with Session() as s:
            res = await s.execute(
                update(Proposal)
                .where(
                    Proposal.confirmation_id == confirmation_id,
                    Proposal.operation == operation,
                    Proposal.status == "confirmed",
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
        операторном режиме chat_id всегда совпадает → поведение не меняется (fail-closed, аддитивно)."""
        async with Session() as s:
            res = await s.execute(
                update(Proposal)
                .where(
                    Proposal.confirmation_id == confirmation_id,
                    Proposal.status == "pending",
                    Proposal.chat_id == chat_id,  # владение: только владелец черновика
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
    ) -> None:
        async with Session() as s:
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one_or_none()
            # Гард владения (как confirm): отклонить может только владелец черновика. Чужой chat_id
            # → тихо ничего не делаем (fail-closed). cleanup_stale_proposals передаёт p.chat_id сам.
            if p is not None and p.status == "pending" and p.chat_id == chat_id:
                p.status = "rejected"
                p.decided_at = func.now()
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
            AuditEvent(r.created_at, r.status, r.operation, au, an, r.result, r.confirmation_id)
        )
        if len(out) >= limit:
            break
    return out
