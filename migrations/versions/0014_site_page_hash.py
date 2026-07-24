"""§20.5: content_hash на client_site_pages для инкрементального перекраула

Инкрементальный краул («перекраулить только новое») сравнивает сигнатуру контента страницы с
прошлым краулом: новый/изменённый hash ⇒ страница новая/изменилась. Храним короткий sha1 (16 hex)
по title+усечённому тексту. Столбец nullable — старые страницы (без хэша) в diff не участвуют.
db/models.py обновлён симметрично; на SQLite (dev) столбец создаёт create_all, здесь — истина для
Postgres (prod).

Цепочка: down_revision = 0013_client_kb (один head).

Revision ID: 0014_site_page_hash
Revises: 0013_client_kb
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_site_page_hash"
down_revision: str | None = "0013_client_kb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "client_site_pages",
        sa.Column("content_hash", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("client_site_pages", "content_hash")
