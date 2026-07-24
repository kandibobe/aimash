"""audit_log: actor_user_id + actor_username («кто» нажал ✅/❌, §12)

chat_id в групповом чате — это чат, а не человек. Для атрибуции решения (§12 «кто/когда/что»)
добавляем user_id (и опц. username) нажавшего подтверждение/отмену. Nullable: системные строки
(applied/failed, scheduler-reject) остаются NULL — актор виден по связанной confirmed/rejected
строке (тот же confirmation_id). db/models.py обновлён симметрично.

Revision ID: 0004_audit_actor
Revises: 0003_proposal_cleanup_index
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_audit_actor"
down_revision: str | None = "0003_proposal_cleanup_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("actor_user_id", sa.BigInteger(), nullable=True))
    op.add_column("audit_log", sa.Column("actor_username", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "actor_username")
    op.drop_column("audit_log", "actor_user_id")
