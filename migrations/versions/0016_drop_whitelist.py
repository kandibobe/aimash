"""Удаление мёртвой таблицы whitelist (dead schema, предсдаточный аудит 2026-07).

Таблица создана в 0001, но НИКОГДА не читалась/писалась рантаймом: реальный allow-list — env
TELEGRAM_WHITELIST_CHAT_IDS (bot.main.WhitelistMiddleware, fail-closed). Наличие таблицы создавало
иллюзию БД-управляемого доступа. Мультиюзер-доступ к АККАУНТАМ живёт в account_access (0009).

upgrade — drop с inspector-гардом: dev-SQLite, созданные create_all ПОСЛЕ удаления модели,
таблицы не имеют — не падаем. downgrade — воссоздание по форме 0001 (id, chat_id BigInteger
unique+index, note, created_at).

Цепочка: down_revision = 0015_user_ui_prefs (один head).

Revision ID: 0016_drop_whitelist
Revises: 0015_user_ui_prefs
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_drop_whitelist"
down_revision: str | None = "0015_user_ui_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("whitelist"):
        op.drop_table("whitelist")


def downgrade() -> None:
    op.create_table(
        "whitelist",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
    )
    op.create_index("ix_whitelist_chat_id", "whitelist", ["chat_id"])
