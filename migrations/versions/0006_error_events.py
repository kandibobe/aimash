"""error_events: перехваченные исключения для триажа (§15)

Система ловли ошибок/багов: глобальный on_error и scheduler-джобы пишут редактированный снимок
исключения (request_id/где/тип/текст/traceback — БЕЗ секретов, golden rule #5) для пост-разбора
и команды /diag. db/models.py обновлён симметрично; на SQLite (dev) таблицу создаёт create_all,
здесь — истина для Postgres (prod).

Revision ID: 0006_error_events
Revises: 0005_user_language
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_error_events"
down_revision: str | None = "0005_user_language"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "error_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.String(length=16), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("where", sa.String(length=160), nullable=False),
        sa.Column("exc_type", sa.String(length=100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_error_events_request_id", "error_events", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_error_events_request_id", table_name="error_events")
    op.drop_table("error_events")
