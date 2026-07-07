"""Рантайм-админы: таблица admins (P4, фиксы по живому тесту заказчика 2026-07-06).

Раньше админы задавались ТОЛЬКО через env ADMIN_CHAT_IDS (правка .env + рестарт на VPS).
Теперь is_admin = env ∪ БД (core.access.is_admin, зеркало рантайм-whitelist 0017): env — бутстрап
первого админа (неснимаемый в рантайме), таблица — /addadmin//removeadmin без рестарта.
Fail-closed: сбой БД ⇒ действует только env; пустые оба ⇒ админов нет.

upgrade — create с inspector-гардом (dev-SQLite мог создать через create_all). downgrade — drop.

Цепочка: down_revision = 0020_audit_chat_index (один head).

Revision ID: 0021_admins_runtime
Revises: 0020_audit_chat_index
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_admins_runtime"
down_revision: str | None = "0020_audit_chat_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("admins"):
        return  # уже создана (dev-SQLite create_all) — идемпотентно
    op.create_table(
        "admins",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
    )
    op.create_index("ix_admins_chat_id", "admins", ["chat_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("admins"):
        op.drop_table("admins")
