"""audit_log: индекс (chat_id, created_at) для агрегата «частые аккаунты» (1.7)

bot.main._frequent_accounts считает активность оператора (audit_log ∪ proposals ∪ recommendation,
GROUP BY customer_id) на КАЖДЫЙ рендер пикера (смягчено TTL-кэшем 10 мин, но скан по chat_id всё
равно горячий). Индекс зеркалится в db/models.py.__table_args__ (конвенция 0018 — против дрейфа
autogenerate). Данные не меняются — только индекс (обратимо).

Revision ID: 0020_audit_chat_index
Revises: 0019_bug_reports
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_audit_chat_index"
down_revision: str | None = "0019_bug_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_audit_log_chat_created"


def upgrade() -> None:
    op.create_index(_INDEX, "audit_log", ["chat_id", "created_at"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="audit_log")
