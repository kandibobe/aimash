"""sheet_exports: реестр созданных ботом Google-таблиц (отчёты /sheets и таблицы ключей визарда)

До этого ссылка на таблицу жила только в сообщении Telegram, а для ключей — ещё и в
CampaignDraft.wizard_state, который умирает по TTL 72ч: после закрытия визарда найти таблицу было
негде. Здесь ссылка переживает и рестарт, и черновик (команда /mysheets отдаёт последние таблицы
чата). share — исход anyone-with-link на момент создания (роль | 'off' | 'failed'), чтобы /mysheets
честно помечал таблицы, которые доступны не всем.

Секретов не хранит: url — публичная ссылка, уже отправленная в чат. Ширины с запасом (урок
0023_recommendation_topic_width: узкий VARCHAR(16) под строковый тег ронял вставку на Postgres).
db/models.py обновлён симметрично; на SQLite (dev) таблицу создаёт create_all, здесь — истина для
Postgres (prod).

Цепочка: down_revision = 0024_unify_recommendation_kind (один head).

Revision ID: 0025_sheet_exports
Revises: 0024_unify_recommendation_kind
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_sheet_exports"
down_revision: str | None = "0024_unify_recommendation_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sheet_exports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),  # keywords|report
        sa.Column("spreadsheet_id", sa.String(length=128), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("share", sa.String(length=16), nullable=False),  # роль|off|failed
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_sheet_exports_chat_id", "sheet_exports", ["chat_id"], unique=False)
    op.create_index("ix_sheet_exports_chat_id_id", "sheet_exports", ["chat_id", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sheet_exports_chat_id_id", table_name="sheet_exports")
    op.drop_index("ix_sheet_exports_chat_id", table_name="sheet_exports")
    op.drop_table("sheet_exports")
