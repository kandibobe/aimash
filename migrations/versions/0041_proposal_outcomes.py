"""Track seven-day outcomes on applied proposals.

Revision ID: 0041_proposal_outcomes
Revises: 0040_pending_proposal_per_run
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_proposal_outcomes"
down_revision: str | None = "0040_pending_proposal_per_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_proposals_outcome_due"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("proposals"):
        raise RuntimeError("proposals table is missing")
    columns = {column["name"] for column in inspector.get_columns("proposals")}
    additions = (
        ("outcome_context", sa.JSON()),
        ("outcome_due_at", sa.DateTime(timezone=True)),
        ("outcome_state", sa.String(length=16)),
        ("outcome_claimed_at", sa.DateTime(timezone=True)),
        ("outcome_checked_at", sa.DateTime(timezone=True)),
        ("outcome_attempts", sa.Integer(), False, "0"),
        ("outcome_result", sa.JSON()),
    )
    for item in additions:
        name, type_ = item[:2]
        if name in columns:
            continue
        nullable = item[2] if len(item) > 2 else True
        server_default = sa.text(item[3]) if len(item) > 3 else None
        op.add_column(
            "proposals",
            sa.Column(name, type_, nullable=nullable, server_default=server_default),
        )
    inspector = sa.inspect(bind)
    if not any(index["name"] == _INDEX for index in inspector.get_indexes("proposals")):
        op.create_index(_INDEX, "proposals", ["outcome_state", "outcome_due_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("proposals"):
        return
    if any(index["name"] == _INDEX for index in inspector.get_indexes("proposals")):
        op.drop_index(_INDEX, table_name="proposals")
    columns = {column["name"] for column in inspector.get_columns("proposals")}
    for name in (
        "outcome_result",
        "outcome_attempts",
        "outcome_checked_at",
        "outcome_claimed_at",
        "outcome_state",
        "outcome_due_at",
        "outcome_context",
    ):
        if name in columns:
            op.drop_column("proposals", name)
