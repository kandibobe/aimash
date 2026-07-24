"""Авто-память действий пользователя (§2C): recall ПРИМЕНЁННЫХ операций из proposals.

Детерминированно, в КОДЕ — НЕ в промпт LLM (golden rule: модели нельзя доверять исполнение;
recalled-значения всё равно проходят Pydantic + confirm-гейт). Источник — Proposal.status='applied'
(переход ставит confirm.store.finalize). Инертные ключи (_before и т.п.) срезаем перед выдачей.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from ads.freshness import ATTESTATION_KEYS
from db.models import Proposal
from db.session import Session

# Ключи, инертные для повтора (служебные снимки/резолвы/метки) — не показываем и не повторяем.
# _rec_uid кладёт bot._advise_apply (привязка замера эффекта к рекомендации) — повтору он не нужен и,
# если схемы когда-нибудь ужесточат до extra="forbid", лишний ключ уронил бы повтор из /recent.
# ATTESTATION_KEYS (_before/_freshness, Волна 1.1) — снимок и аттестация ТОГО хода; повтор обязан
# получить СВОЮ, свежую, иначе унаследовал бы чужое «прочитано» и проехал бы гейт на устаревшем
# состоянии. Берём общей константой, а не литералом: новый ключ аттестации добавляется в одном месте.
_INERT_KEYS = ATTESTATION_KEYS | {"_audience_names", "_rec_uid"}


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


async def list_recent_applied_by_customer(
    customer_id: str, operation: str | None = None, limit: int = 5
) -> list[RecentAction]:
    """Как list_recent_applied, но по customer_id, а не chat_id — для Hermes-контура A (MCP), где
    аккаунт задаётся аргументом инструмента, а не привязан к Telegram-чату. Это НАШ audit-trail из
    proposals (applied), НЕ Google Ads change-history. Обёртка всё равно первой строкой зовёт
    ensure_read_allowed(customer_id) — защита от кросс-клиентного чтения (И6). Порядок — по id
    убыванию (монотонный, как в list_recent_applied). customer_id нормализуем в str (в БД — String)."""
    async with Session() as s:
        q = select(Proposal).where(
            Proposal.customer_id == str(customer_id), Proposal.status == "applied"
        )
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
