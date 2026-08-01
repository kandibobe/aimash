"""Enforce at most one pending confirmation card per human turn.

Revision ID: 0040_pending_proposal_per_run
Revises: 0039_notification_outbox
Create Date: 2026-08-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_pending_proposal_per_run"
down_revision: str | None = "0039_notification_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ux_proposals_pending_run_id"
_PREDICATE = "status = 'pending' AND run_id IS NOT NULL AND run_id <> '-'"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("proposals"):
        raise RuntimeError("proposals table is missing")
    if any(index["name"] == _INDEX for index in inspector.get_indexes("proposals")):
        return
    duplicate = bind.execute(
        sa.text(
            "SELECT run_id FROM proposals WHERE status = 'pending' AND run_id IS NOT NULL "
            "AND run_id <> '-' "
            "GROUP BY run_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise RuntimeError(
            "cannot create ux_proposals_pending_run_id: duplicate pending run_id exists"
        )
    op.create_index(
        _INDEX,
        "proposals",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text(_PREDICATE),
        sqlite_where=sa.text(_PREDICATE),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if any(index["name"] == _INDEX for index in inspector.get_indexes("proposals")):
        op.drop_index(_INDEX, table_name="proposals")
