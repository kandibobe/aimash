"""SQLAlchemy-стор черновиков (proposals) + audit-журнал. Реализует протокол confirm_store
(async get_confirmed/finalize), который ждут ads/mutations.apply_*.

Поток безопасности:
  save_proposal (pending) → confirm (confirmed) → apply_* проверяет status=confirmed
  по confirmation_id → выполняет → finalize (audit: applied). Reject/ошибки тоже в audit_log.

Хранилище — db.models (Proposal/AuditLog) на движке из DATABASE_URL (dev: SQLite). Секретов тут нет.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

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
        user_initiated: bool = True,
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

    async def confirm(self, confirmation_id: str, *, chat_id: int) -> bool:
        """pending → confirmed (одноразово). True если перевёл. Пишет audit."""
        async with Session() as s:
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one_or_none()
            if p is None or p.status != "pending":
                return False
            p.status = "confirmed"
            p.decided_at = func.now()
            s.add(_audit(p, chat_id, "confirmed"))
            await s.commit()
            return True

    async def reject(self, confirmation_id: str, *, chat_id: int) -> None:
        async with Session() as s:
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one_or_none()
            if p is not None and p.status == "pending":
                p.status = "rejected"
                p.decided_at = func.now()
                s.add(_audit(p, chat_id, "rejected"))
                await s.commit()

    async def finalize(self, confirmation_id: str, *, result: object) -> None:
        """Запись результата выполнения в audit (status=applied)."""
        async with Session() as s:
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one_or_none()
            if p is not None:
                s.add(_audit(p, p.chat_id, "applied", result=result))
                await s.commit()

    async def record_failure(self, confirmation_id: str, *, error: str) -> None:
        async with Session() as s:
            p = (
                await s.execute(select(Proposal).where(Proposal.confirmation_id == confirmation_id))
            ).scalar_one_or_none()
            if p is not None:
                s.add(_audit(p, p.chat_id, "failed", result={"error": error}))
                await s.commit()


def _audit(p: Proposal, chat_id: int, status: str, result: object = None) -> AuditLog:
    return AuditLog(
        confirmation_id=p.confirmation_id,
        operation=p.operation,
        customer_id=p.customer_id,
        chat_id=chat_id,
        status=status,
        result=result,
    )
