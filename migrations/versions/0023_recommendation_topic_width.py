"""recommendation.topic: VARCHAR(16) → VARCHAR(32) — прод-баг /audit.

Колонка заводилась под темы /advise (optimize|keywords|rsa|structure, ≤9 символов), но /audit
пишет туда СЕМЬЮ чека (bot.main._audit_run → advisor.store.record_recommendations), а самая
длинная семья — 'conversion_tracking' (19). На Postgres это StringDataRightTruncation (22001):
/audit падал ПОСЛЕ карточки score, ровно на аккаунтах без настроенного трекинга конверсий
(находка no_conversion_tracking имеет at_risk = весь расход ⇒ всегда в топе). На dev-SQLite
ширина VARCHAR не проверяется — баг был невидим локально.

Гард класса (чтобы не вернулось с новой семьёй):
tests/test_db_schema.test_recommendation_columns_fit_audit_taxonomy.

downgrade сужает обратно, но СНАЧАЛА обрезает данные — иначе Postgres откажется сужать тип на
строках длиннее 16 (те самые, ради которых миграция и делается).

Цепочка: down_revision = 0022_account_health_snapshot (один head).

Revision ID: 0023_recommendation_topic_width
Revises: 0022_account_health_snapshot
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_recommendation_topic_width"
down_revision: str | None = "0022_account_health_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "recommendation",
        "topic",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Обрезаем ПЕРЕД сужением типа: строки с 'conversion_tracking' (19) иначе валят ALTER.
    op.execute("UPDATE recommendation SET topic = substr(topic, 1, 16)")
    op.alter_column(
        "recommendation",
        "topic",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
