"""§UX-память: ui_prefs (JSON) на user_settings — «агент запоминает» настройки интерфейса

Бот запоминает последний выбранный период отчётов ({"last_report_period": "7"}) и предлагает его
первой кнопкой «↻ как в прошлый раз» в пикере /report /export /sheets. Отдельная колонка (не
alert_thresholds): тот — пороги scheduler-аномалий и читается целиком (_thresholds_by_chat),
семантику не смешиваем. Nullable — старые строки без действий. db/models.py обновлён симметрично;
на SQLite (dev) столбец добавит heal_sqlite_schema/create_all, здесь — истина для Postgres (prod).

Цепочка: down_revision = 0014_site_page_hash (один head).

Revision ID: 0015_user_ui_prefs
Revises: 0014_site_page_hash
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_user_ui_prefs"
down_revision: str | None = "0014_site_page_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("ui_prefs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "ui_prefs")
