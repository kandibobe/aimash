"""Возврат таблицы whitelist — теперь ПОДКЛЮЧЕНА к рантайму (P0-A, доведение до продакшена 2026-07).

0016 удалила whitelist как мёртвую схему (allow-list был только env). Теперь бот читает env ∪ БД
(bot.main.WhitelistMiddleware), а админ добавляет операторов без рестарта (/adduser →
core.access.add_whitelisted_user). Форма шире, чем в 0001: добавлен added_by (какой админ добавил).

upgrade — create с inspector-гардом (dev-SQLite мог создать её через create_all после возврата
модели — не падаем). downgrade — drop (симметрично 0016.upgrade).

Цепочка: down_revision = 0016_drop_whitelist (один head).

Revision ID: 0017_whitelist_runtime
Revises: 0016_drop_whitelist
Create Date: 2026-07-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_whitelist_runtime"
down_revision: str | None = "0016_drop_whitelist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("whitelist"):
        return  # уже создана (dev-SQLite create_all) — идемпотентно
    op.create_table(
        "whitelist",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
    )
    op.create_index("ix_whitelist_chat_id", "whitelist", ["chat_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("whitelist"):
        op.drop_table("whitelist")
