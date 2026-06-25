"""proposals: индекс (status, created_at) для очистки просроченных черновиков

Горячий запрос scheduler.cleanup_stale_proposals — `WHERE status='pending'` (возраст по
created_at). Композитный индекс ускоряет скан pending и позволяет в будущем опустить отсечку
возраста в SQL. db/models.py НЕ трогаем (горячий файл) — на SQLite индекса не будет (там
create_all по моделям), это чисто перформанс, корректность не страдает.

⚠️ ЦЕПОЧКА МИГРАЦИЙ (коллизия с Фазой 3): сейчас head = 0001_initial, поэтому down_revision
указывает на него. Параллельный процесс, вероятно, добавит 0002_* (UserSettings) → при мердже
ПЕРЕНАЦЕЛИТЬ down_revision на реальный head (0002_*), иначе Alembic получит two-heads.

Revision ID: 0003_proposal_cleanup_index
Revises: 0001_initial
Create Date: 2026-06-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_proposal_cleanup_index"
down_revision: str | None = "0001_initial"  # ⚠️ при наличии 0002_* — переставить на него
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_proposals_status_created_at"


def upgrade() -> None:
    op.create_index(_INDEX, "proposals", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="proposals")
