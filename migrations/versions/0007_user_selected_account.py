"""user_settings: selected_customer_id (активный аккаунт чата, §8/мультиаккаунт)

Активный аккаунт на чат (читаем/минтуем черновики на нём). Колонка nullable — NULL трактуется
как Draft (bot.account_ctx.get_active_account), существующие строки не ломаются. db/models.py
обновлён симметрично; на SQLite (dev) колонку добавляет self-heal (db.session), здесь — истина
для Postgres (prod).

Revision ID: 0007_user_selected_account
Revises: 0006_error_events
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_user_selected_account"
down_revision: str | None = "0006_error_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_settings", sa.Column("selected_customer_id", sa.String(length=20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("user_settings", "selected_customer_id")
