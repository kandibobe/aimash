"""account_access: пер-пользовательский доступ к аккаунтам (§12/мультиоператор)

chat_id → разрешённые customer_id. Draft доступен всем whitelisted без строки; прочие аккаунты —
только при явном гранте (fail-closed, ensure_account_allowed_for_user). Уникальный индекс
(chat_id, customer_id) — идемпотентность гранта. db/models.py обновлён симметрично; на SQLite (dev)
таблицу создаёт create_all, здесь — истина для Postgres (prod).

Revision ID: 0009_account_access
Revises: 0008_oauth_login_customer_id
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_account_access"
down_revision: str | None = "0008_oauth_login_customer_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ux_account_access", "account_access", ["chat_id", "customer_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_account_access", table_name="account_access")
    op.drop_table("account_access")
