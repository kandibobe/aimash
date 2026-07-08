"""Снапшоты health-score /audit: account_health_snapshot (волна N1.1).

Субстрат трендов (WoW-дельта в /audit, дайджесты позже): агрегаты score/grade/at_risk/штрафы семей
per (customer_id, дата-в-TZ-аккаунта, period_days) — окно аудита в уникальном ключе, чтобы
/audit 7 не клобберил 30-дневную базу тренда в тот же день. Без PII/имён кампаний. Дельты
сравнимы только в пределах одной score_model_version (N1.0a) и одного period_days — сравнение
делает КОД (audit/snapshot.py).

upgrade — create с inspector-гардом (dev-SQLite мог создать через create_all). downgrade — drop.

Цепочка: down_revision = 0021_admins_runtime (один head).

Revision ID: 0022_account_health_snapshot
Revises: 0021_admins_runtime
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_account_health_snapshot"
down_revision: str | None = "0021_admins_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("account_health_snapshot"):
        return  # уже создана (dev-SQLite create_all) — идемпотентно
    op.create_table(
        "account_health_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.String(length=20), nullable=False),
        sa.Column("snapshot_date", sa.String(length=10), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("grade", sa.String(length=4), nullable=False),
        sa.Column("total_spend", sa.Float(), nullable=False, server_default="0"),
        sa.Column("at_risk", sa.Float(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("period_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("family_penalty", sa.JSON(), nullable=True),
        sa.Column("score_model_version", sa.String(length=16), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
    )
    op.create_index(
        "ux_account_health_snapshot_cid_date_period",
        "account_health_snapshot",
        ["customer_id", "snapshot_date", "period_days"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("account_health_snapshot"):
        op.drop_index(
            "ux_account_health_snapshot_cid_date_period", table_name="account_health_snapshot"
        )
        op.drop_table("account_health_snapshot")
