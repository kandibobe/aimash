"""oauth_tokens: login_customer_id (MCC аккаунта, §8/Фаза 3 — аккаунты под разными MCC)

Под мультиаккаунт аккаунты живут под РАЗНЫМИ менеджерами, поэтому MCC (login_customer_id)
хранится per-account (ads.client.build_client подставит при сборке клиента). Колонка nullable
(аддитивно). db/models.py обновлён симметрично; на SQLite (dev) колонку добавляет self-heal,
здесь — истина для Postgres (prod).

Revision ID: 0008_oauth_login_customer_id
Revises: 0007_user_selected_account
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_oauth_login_customer_id"
down_revision: str | None = "0007_user_selected_account"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "oauth_tokens", sa.Column("login_customer_id", sa.String(length=20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("oauth_tokens", "login_customer_id")
