"""Make proposal creation idempotent for one trusted tool call.

Revision ID: 0043_proposal_idempotency
Revises: 0042_research_archive
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_proposal_idempotency"
down_revision: str | None = "0042_research_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ux_proposals_idempotency_key"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("proposals"):
        raise RuntimeError("proposals table is missing")
    columns = {column["name"] for column in inspector.get_columns("proposals")}
    if "idempotency_key" not in columns:
        op.add_column("proposals", sa.Column("idempotency_key", sa.Text(), nullable=True))
    inspector = sa.inspect(bind)
    if not any(index["name"] == _INDEX for index in inspector.get_indexes("proposals")):
        op.create_index(_INDEX, "proposals", ["idempotency_key"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if any(index["name"] == _INDEX for index in inspector.get_indexes("proposals")):
        op.drop_index(_INDEX, table_name="proposals")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("proposals")}
    if "idempotency_key" in columns:
        op.drop_column("proposals", "idempotency_key")
