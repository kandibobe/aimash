"""campaign_drafts: §19 персистентный черновик 8-этапного визарда «Создание кампании»

Накопленное состояние визарда (wizard_state JSON) переживает рестарт бота и Этап-2 round-trip с
Google Sheets (менеджер уходит редактировать таблицу и возвращается позже). current_step — курсор
стадии (0..7). В Google Ads ничего не меняется до финального «Создать черновик» (тот выпускает
отдельный proposal create_search_campaign через confirm-гейт). db/models.py обновлён симметрично;
на SQLite (dev) таблицу создаёт create_all, здесь — истина для Postgres (prod).

Цепочка: down_revision = 0011_error_event_customer (один head, см. docs/BACKUP.md).

Revision ID: 0012_campaign_drafts
Revises: 0011_error_event_customer
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_campaign_drafts"
down_revision: str | None = "0011_error_event_customer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.String(length=20), nullable=False),
        sa.Column("preview_customer_id", sa.String(length=20), nullable=True),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wizard_state", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index("ix_campaign_drafts_session_id", "campaign_drafts", ["session_id"], unique=True)
    op.create_index("ix_campaign_drafts_chat_id", "campaign_drafts", ["chat_id"], unique=False)
    op.create_index(
        "ix_campaign_drafts_chat_status", "campaign_drafts", ["chat_id", "status"], unique=False
    )
    op.create_index(
        "ix_campaign_drafts_status_updated",
        "campaign_drafts",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_drafts_status_updated", table_name="campaign_drafts")
    op.drop_index("ix_campaign_drafts_chat_status", table_name="campaign_drafts")
    op.drop_index("ix_campaign_drafts_chat_id", table_name="campaign_drafts")
    op.drop_index("ix_campaign_drafts_session_id", table_name="campaign_drafts")
    op.drop_table("campaign_drafts")
