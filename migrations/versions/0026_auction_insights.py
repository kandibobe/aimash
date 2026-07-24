"""auction_insight_row: импортированные срезы отчёта «Статистика аукционов» (CSV из интерфейса)

Имён конкурентов Google через API не отдаёт (ресурса `auction_insight` в GAQL нет) — единственный
легальный путь к ним лежит через выгрузку из веб-интерфейса, которую приносит человек (/competitors).
Отсюда таблица: срез аукциона на дату (домен, доля показов, пересечение, позиция выше, верх страницы,
опережение) + сравнение с прошлым импортом.

Идемпотентно per (customer_id, snapshot_date, domain) — уникальный индекс; повторный импорт за ту же
дату перезаписывает срез целиком (домены между выгрузками появляются/исчезают ⇒ мерж дал бы
«призраков», которых в новом отчёте уже нет).

Доли — nullable FLOAT: NULL = «--» в файле («не показывалось»), это НЕ 0.0 (нет данных ≠ ноль).
Ширины с запасом (урок 0023_recommendation_topic_width). Секретов не хранит: домены конкурентов —
публичный факт из отчёта Google. db/models.py обновлён симметрично; на SQLite (dev) таблицу создаёт
create_all, здесь — истина для Postgres (prod).

Цепочка: down_revision = 0025_sheet_exports (один head).

Revision ID: 0026_auction_insights
Revises: 0025_sheet_exports
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_auction_insights"
down_revision: str | None = "0025_sheet_exports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auction_insight_row",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.String(length=20), nullable=False),
        sa.Column("snapshot_date", sa.String(length=10), nullable=False),  # ISO, TZ аккаунта
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("is_you", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("impression_share", sa.Float(), nullable=True),  # NULL = «--» в отчёте
        sa.Column("overlap_rate", sa.Float(), nullable=True),
        sa.Column("position_above_rate", sa.Float(), nullable=True),
        sa.Column("top_of_page_rate", sa.Float(), nullable=True),
        sa.Column("abs_top_of_page_rate", sa.Float(), nullable=True),
        sa.Column("outranking_share", sa.Float(), nullable=True),
        sa.Column("period_label", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ux_auction_insight_cid_date_domain",
        "auction_insight_row",
        ["customer_id", "snapshot_date", "domain"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_auction_insight_cid_date_domain", table_name="auction_insight_row")
    op.drop_table("auction_insight_row")
