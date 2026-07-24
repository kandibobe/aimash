"""proposals: индекс (chat_id, status) для истории чата (db.history.list_recent_applied)

`WHERE chat_id=? AND status='applied' ORDER BY id DESC LIMIT n` — читается на каждом «повторить
последнюю операцию»/подстановке контекста. Существующий (status, created_at) заставлял Postgres
перебирать ВСЕ applied-строки ВСЕХ чатов: доля applied растёт со временем, селективности по чату
у него нет. Индекс зеркалится в db/models.py.__table_args__ (конвенция 0018/0020 — против дрейфа
autogenerate). Данные не меняются — только индекс (обратимо).

Цепочка: down_revision = 0026_auction_insights (один head).

Revision ID: 0027_proposal_chat_index
Revises: 0026_auction_insights
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027_proposal_chat_index"
down_revision: str | None = "0026_auction_insights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_proposals_chat_status"


def upgrade() -> None:
    op.create_index(_INDEX, "proposals", ["chat_id", "status"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="proposals")
