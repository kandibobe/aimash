"""error_events: customer_id (per-account триаж ошибок, §8) + индекс (customer_id, created_at)

На масштабе ~10 аккаунтов /diag должен фильтровать ошибки по аккаунту. Колонка nullable (ошибки
вне per-account контекста — NULL). Индекс ускоряет пер-аккаунтные выборки. db/models.py обновлён
симметрично; на SQLite (dev) колонку добавляет self-heal, здесь — истина для Postgres (prod).

Цепочка: down_revision = 0010_campaign_templates (а не 0009) — линеаризуем после параллельной
ветки шаблонов, чтобы не было двух head'ов (см. docs/BACKUP.md «один head»).

Revision ID: 0011_error_event_customer
Revises: 0010_campaign_templates
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_error_event_customer"
down_revision: str | None = "0010_campaign_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("error_events", sa.Column("customer_id", sa.String(length=20), nullable=True))
    op.create_index(
        "ix_error_events_customer_created", "error_events", ["customer_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_error_events_customer_created", table_name="error_events")
    op.drop_column("error_events", "customer_id")
