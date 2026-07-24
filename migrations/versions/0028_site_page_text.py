"""client_site_pages: колонка text — сырой текст страницы после вычитания шаблона

Текст краула жил только в памяти: его отдавали LLM и выбрасывали, в БД оставались url/title/hash.
Пересобрать досье (clients.dossier_*) с новой схемой или другой моделью было нельзя — только заново
обойти весь сайт (минуты, нагрузка на чужой хост). Храним текст; ретеншн — scheduler.jobs
.purge_stale_rows (site_page_text_retain_days), там же он обнуляется у старых страниц.

Nullable — старые строки (докраульные) остаются с NULL, бэкфила нет: текста для них не существует.

Цепочка: down_revision = 0027_proposal_chat_index (один head).

Revision ID: 0028_site_page_text
Revises: 0027_proposal_chat_index
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_site_page_text"
down_revision: str | None = "0027_proposal_chat_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("client_site_pages", sa.Column("text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("client_site_pages", "text")
