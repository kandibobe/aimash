"""agent_runs / agent_run_events: per-run учёт cost/latency/итераций (#10 Наблюдаемость)

Волна 1 шаг 10. `core.usage` даёт срез по РОЛЯМ, сбрасываемый на рестарте; `core.quota` считает
API-операции, не деньги; latency каждого вызова жил только в лог-строках `core.resilience` и терялся.
Здесь — персистентный учёт: одна строка `agent_runs` = один ассистентский ход (cost/iterations),
одна строка `agent_run_events` = один шаг хода (LLM/tool/ads-вызов, latency, cost). Сшивка по значению
`run_id` = `core.provenance.run_id` (== `core.context.request_id`) — одна корреляция на ход.
Дизайн — HERMES_SPEC.md:929-932 (`agent_runs`), :1453-1456 (`agent_run_events`).

Deliverable шага 10 «сколько стоит прогон / группа в месяц» считается отсюда:
`SUM(cost_usd) WHERE customer_id = ? AND started_at > cutoff` (индекс ix_agent_runs_customer_started).

upgrade — create с inspector-гардом (dev-SQLite мог создать таблицы через create_all → идемпотентно).
downgrade — drop. Без секретов/PII: `args_redacted` проходит redact_text ДО записи (пишущий слой,
не миграция), `result_digest` — короткая сводка, не сырьё; customer_id — id аккаунта, не секрет.

Цепочка: down_revision = 0031_ads_quota_ops (один head).

Revision ID: 0032_agent_runs
Revises: 0031_ads_quota_ops
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_agent_runs"
down_revision: str | None = "0031_ads_quota_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("agent_runs"):
        op.create_table(
            "agent_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.String(length=16), nullable=False),  # = provenance.run_id[:16]
            sa.Column("origin", sa.String(length=16), nullable=False, server_default="machine"),
            sa.Column("chat_id", sa.BigInteger(), nullable=True),
            sa.Column("customer_id", sa.String(length=20), nullable=True),  # для отчёта по группе
            sa.Column("operation", sa.String(length=64), nullable=True),
            sa.Column("model", sa.String(length=64), nullable=True),
            sa.Column("iterations_used", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "cost_usd", sa.Float(), nullable=False, server_default="0"
            ),  # usage.cost (USD)
            sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
            # NOT NULL как в модели (Mapped[datetime]): server_default всегда заполняет started_at.
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_agent_runs_run_id", "agent_runs", ["run_id"])
        # Отчёт «сколько стоит группа за окно»: WHERE customer_id = ? AND started_at > cutoff.
        op.create_index(
            "ix_agent_runs_customer_started", "agent_runs", ["customer_id", "started_at"]
        )

    if not inspector.has_table("agent_run_events"):
        op.create_table(
            "agent_run_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.String(length=16), nullable=False),  # сшивка с agent_runs
            sa.Column("seq", sa.Integer(), nullable=False),  # порядок шага в прогоне
            sa.Column("kind", sa.String(length=16), nullable=False),  # llm|tool|ads_read|ads_mutate
            sa.Column("tool_name", sa.String(length=64), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rows_returned", sa.Integer(), nullable=True),
            sa.Column("args_redacted", sa.Text(), nullable=True),  # РЕДАКТИРОВАНО (redact_text)
            sa.Column("result_digest", sa.String(length=255), nullable=True),  # сводка, не сырьё
            sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
            # NOT NULL как в модели (Mapped[datetime]): server_default всегда заполняет created_at.
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_agent_run_events_run_seq", "agent_run_events", ["run_id", "seq"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("agent_run_events"):
        op.drop_index("ix_agent_run_events_run_seq", table_name="agent_run_events")
        op.drop_table("agent_run_events")
    if inspector.has_table("agent_runs"):
        op.drop_index("ix_agent_runs_customer_started", table_name="agent_runs")
        op.drop_index("ix_agent_runs_run_id", table_name="agent_runs")
        op.drop_table("agent_runs")
