"""user_settings: language (язык интерфейса RU/EN, §4)

Полная RU/EN-локализация (ТЗ §4): выбор языка (/lang) должен переживать рестарт. Колонка
nullable — NULL трактуется как дефолт (RU), существующие строки не ломаются. db/models.py
обновлён симметрично; на SQLite (dev) колонку добавляет ещё и self-heal (db.session), здесь —
истина для Postgres (prod).

Revision ID: 0005_user_language
Revises: 0004_audit_actor
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_user_language"
down_revision: str | None = "0004_audit_actor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("language", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "language")
