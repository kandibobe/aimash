"""advisor: таблицы рекомендаций + обратной связи + замера результата (Слой A/B).

Три таблицы: recommendation (показанная рекомендация — advisory, НЕ proposal), recommendation_feedback
(👍/👎 оператора), recommendation_outcome (сшивка рекомендация→applied-мутация→delta метрик). Ни одна
не исполняет мутаций — исполнение любого совета идёт через существующий confirm-гейт (golden rule #1/#3).

upgrade — create с inspector-гардом (dev-SQLite мог создать их через create_all после появления
моделей — идемпотентно). downgrade — drop симметрично. На Postgres (prod) heal_sqlite_schema таблицы
НЕ создаёт — их ставит эта миграция.

Цепочка: down_revision = 0017_whitelist_runtime (один head).

Revision ID: 0018_recommendations
Revises: 0017_whitelist_runtime
Create Date: 2026-07-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_recommendations"
down_revision: str | None = "0017_whitelist_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("recommendation"):
        op.create_table(
            "recommendation",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("rec_uid", sa.String(length=64), nullable=False, unique=True),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("customer_id", sa.String(length=20), nullable=False),
            sa.Column("topic", sa.String(length=16), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("severity", sa.String(length=16), nullable=False),
            sa.Column("target_campaign", sa.String(length=255), nullable=True),
            sa.Column("suggested_operation", sa.String(length=64), nullable=True),
            sa.Column("priority", sa.Float(), nullable=False, server_default="0"),
            sa.Column("evidence", sa.JSON(), nullable=True),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("source", sa.String(length=16), nullable=False, server_default="advise"),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="shown"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index("ix_recommendation_rec_uid", "recommendation", ["rec_uid"])
        op.create_index("ix_recommendation_chat_id", "recommendation", ["chat_id"])
        op.create_index(
            "ix_recommendation_chat_customer_created",
            "recommendation",
            ["chat_id", "customer_id", "created_at"],
        )

    if not insp.has_table("recommendation_feedback"):
        op.create_table(
            "recommendation_feedback",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("rec_uid", sa.String(length=64), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
            sa.Column("actor_username", sa.String(length=64), nullable=True),
            sa.Column("rating", sa.String(length=8), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_recommendation_feedback_rec_uid", "recommendation_feedback", ["rec_uid"]
        )
        op.create_index(
            "ux_recommendation_feedback",
            "recommendation_feedback",
            ["rec_uid", "chat_id"],
            unique=True,
        )

    if not insp.has_table("recommendation_outcome"):
        op.create_table(
            "recommendation_outcome",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("rec_uid", sa.String(length=64), nullable=False),
            sa.Column("confirmation_id", sa.String(length=64), nullable=True),
            sa.Column("customer_id", sa.String(length=20), nullable=False),
            sa.Column("target_campaign", sa.String(length=255), nullable=True),
            sa.Column("apply_date", sa.DateTime(timezone=True), nullable=True),
            sa.Column("baseline", sa.JSON(), nullable=True),
            sa.Column("followup", sa.JSON(), nullable=True),
            sa.Column("delta", sa.JSON(), nullable=True),
            sa.Column("measure_after", sa.DateTime(timezone=True), nullable=True),
            sa.Column("measured_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("verdict", sa.String(length=16), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index("ix_recommendation_outcome_rec_uid", "recommendation_outcome", ["rec_uid"])
        op.create_index(
            "ix_recommendation_outcome_confirmation_id",
            "recommendation_outcome",
            ["confirmation_id"],
        )
        op.create_index(
            "ix_recommendation_outcome_pending",
            "recommendation_outcome",
            ["measured_at", "measure_after"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table in ("recommendation_outcome", "recommendation_feedback", "recommendation"):
        if insp.has_table(table):
            op.drop_table(table)
