"""outcome_log: запись результата каждой применённой мутации для self-learning.

Волна 5 (Self-Learning). Таблица заводится после успешного execute_confirmed в ads/service.py.
Через 7 дней OutcomeChecker собирает metrics_after, сравнивает с metrics_before и выставляет
вердикт (success/neutral/failure). PatternExtractor обобщает паттерны по нескольким аккаунтам в
memory-правила.

confirmation_id UNIQUE: повторный record() с тем же id = UPDATE через INSERT...ON CONFLICT.
Одна мутация = одна строка.

metrics_before/after = JSON: поля разные в зависимости от платформы (google/meta/tiktok),
потому фиксированная схема колонок не подходит; JSON без валидации на стороне БД — валидация
в Python на стороне OutcomeLogger.

Ретеншн — `rollback_watch_retain_days` (тот же что у rollback_watch, один контекст волны).
Денежного следа нет — он в audit_log и agent_run_events.

Цепочка: down_revision = 0037_proposal_risk_tier (один head).

Revision ID: 0038_outcome_log
Revises: 0037_proposal_risk_tier
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_outcome_log"
down_revision: str | None = "0037_proposal_risk_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("outcome_log"):
        return  # уже есть (dev-SQLite create_all) — идемпотентно
    op.create_table(
        "outcome_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "confirmation_id",
            sa.String(length=64),
            nullable=False,
            unique=True,
        ),
        sa.Column("account_id", sa.String(length=20), nullable=False),
        sa.Column("account_name", sa.String(length=128), nullable=True),
        sa.Column(
            "platform",
            sa.String(length=16),
            nullable=False,
            server_default="google",
        ),  # google|meta|tiktok
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        # Снимок «до» — заполняется при создании из get_campaign_stats
        sa.Column("metrics_before", sa.JSON(), nullable=True),
        sa.Column("budget_before", sa.Float(), nullable=True),
        sa.Column("bid_before", sa.Float(), nullable=True),
        # Снимок «после» — заполняется OutcomeChecker через 7+ дней
        sa.Column("metrics_after", sa.JSON(), nullable=True),
        sa.Column("budget_after", sa.Float(), nullable=True),
        sa.Column("bid_after", sa.Float(), nullable=True),
        # Вердикт — заполняется OutcomeChecker
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "verdict",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),  # pending|success|neutral|failure|error
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("delta_percent", sa.Float(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_outcome_log_confirmation_id",
        "outcome_log",
        ["confirmation_id"],
    )
    op.create_index(
        "ix_outcome_log_account_id",
        "outcome_log",
        ["account_id"],
    )
    op.create_index(
        "ix_outcome_verdict",
        "outcome_log",
        ["verdict"],
    )
    op.create_index(
        "ix_outcome_account",
        "outcome_log",
        ["account_id", "platform"],
    )
    op.create_index(
        "ix_outcome_checked",
        "outcome_log",
        ["checked_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("outcome_log"):
        op.drop_table("outcome_log")