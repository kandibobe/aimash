"""campaign_templates: именованные пресеты настроек кампании (§2B)

chat_id → именованный набор валидированных params create_search_campaign (переиспользование
прошлой работы маркетолога). Уникальный индекс (chat_id, name) — upsert по имени в рамках чата.
db/models.py обновлён симметрично; на SQLite (dev) таблицу создаёт create_all, здесь — истина
для Postgres (prod). Секретов в params нет; применение — только через confirm-гейт.

Revision ID: 0010_campaign_templates
Revises: 0009_account_access
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_campaign_templates"
down_revision: str | None = "0009_account_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("source_campaign", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_campaign_templates_chat_id", "campaign_templates", ["chat_id"], unique=False
    )
    op.create_index(
        "ux_campaign_templates_chat_name",
        "campaign_templates",
        ["chat_id", "name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_campaign_templates_chat_name", table_name="campaign_templates")
    op.drop_index("ix_campaign_templates_chat_id", table_name="campaign_templates")
    op.drop_table("campaign_templates")
