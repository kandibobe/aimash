"""Авто-память действий пользователя (§2C): recall ПРИМЕНЁННЫХ операций из proposals.

Детерминированно, в КОДЕ — НЕ в промпт LLM (golden rule: модели нельзя доверять исполнение;
recalled-значения всё равно проходят Pydantic + confirm-гейт). Источник — Proposal.status='applied'
(переход ставит confirm.store.finalize). Инертные ключи (_before и т.п.) срезаем перед выдачей.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from db.models import Proposal
from db.session import Session

# Ключи, инертные для повтора (служебные снимки/резолвы) — не показываем и не повторяем.
_INERT_KEYS = frozenset({"_before", "_audience_names"})


@dataclass
class RecentAction:
    confirmation_id: str
    operation: str
    params: dict  # очищенные от инертных ключей
    summary: str
    decided_at: datetime | None


def _clean(params: dict | None) -> dict:
    return {k: v for k, v in (params or {}).items() if k not in _INERT_KEYS}


async def list_recent_applied(
    chat_id: int, operation: str | None = None, limit: int = 5
) -> list[RecentAction]:
    """Последние ПРИМЕНЁННЫЕ операции чата (новые сверху). operation сужает по типу. Порядок —
    по id убыванию (монотонный = надёжнее decided_at, который мог быть NULL у старых строк)."""
    async with Session() as s:
        q = select(Proposal).where(Proposal.chat_id == chat_id, Proposal.status == "applied")
        if operation:
            q = q.where(Proposal.operation == operation)
        q = q.order_by(Proposal.id.desc()).limit(max(1, limit))
        rows = (await s.execute(q)).scalars().all()
    return [
        RecentAction(
            confirmation_id=r.confirmation_id,
            operation=r.operation,
            params=_clean(r.params),
            summary=r.summary,
            decided_at=r.decided_at,
        )
        for r in rows
    ]


async def last_applied(chat_id: int, operation: str) -> RecentAction | None:
    """Самое свежее применённое действие данного типа (для дефолтов визардов). None — нет такого."""
    rows = await list_recent_applied(chat_id, operation, limit=1)
    return rows[0] if rows else None
