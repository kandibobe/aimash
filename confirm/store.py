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

from dataclasses import dataclass

from sqlalchemy import func, select, update

from core.logging import redact_text
from db.models import AuditLog, Proposal
from db.session import Session


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
            if res.rowcount != 1:  # не застолбили (нет/не confirmed/чужая операция/уже взят)
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
        actor_user_id/username — «кто» нажал ✅ (§12), фиксируется в audit-строке решения."""
        async with Session() as s:
            res = await s.execute(
                update(Proposal)
                .where(Proposal.confirmation_id == confirmation_id, Proposal.status == "pending")
                .values(status="confirmed", decided_at=func.now())
            )
            if res.rowcount != 1:  # нет/не pending/уже подтверждён/гонка → не перевели
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
            if p is not None and p.status == "pending":
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
        не даёт повторно застолбить черновик (claim требует status='confirmed')."""
        async with Session() as s:
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one_or_none()
            if p is not None:
                if p.status == "executing":
                    p.status = "applied"
                    p.decided_at = func.now()
                s.add(_audit(p, p.chat_id, "applied", result=result))
                await s.commit()

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
        result=result,
    )
