"""bug_reports: пользовательские баг-репорты (/reportbug, §6 «сообщить об ошибке»).

Одна таблица bug_reports: оператор описывает проблему текстом → бот сохраняет (текст РЕДАКТИРОВАН
через core.logging.redact_text ПЕРЕД записью, golden rule #5), форвардит админам и включает в
еженедельный дайджест. Ничего не мутирует в Google Ads (локальная память, как error_events).

upgrade — create с inspector-гардом (dev-SQLite мог создать таблицу через create_all после
появления модели — идемпотентно). downgrade — drop. На Postgres (prod) heal_sqlite_schema таблицу
НЕ создаёт — её ставит эта миграция.

Цепочка: down_revision = 0018_recommendations (один head).

Revision ID: 0019_bug_reports
Revises: 0018_recommendations
Create Date: 2026-07-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_bug_reports"
down_revision: str | None = "0018_recommendations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("bug_reports"):
        op.create_table(
            "bug_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("username", sa.String(length=64), nullable=True),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("context_request_id", sa.String(length=16), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
            sa.Column("triaged_by", sa.BigInteger(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index("ix_bug_reports_chat_id", "bug_reports", ["chat_id"])
        op.create_index("ix_bug_reports_status_created", "bug_reports", ["status", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("bug_reports"):
        op.drop_table("bug_reports")
