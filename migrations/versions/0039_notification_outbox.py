"""Durable notification outbox for incident escalation delivery.

Revision ID: 0039_notification_outbox
Revises: 0038_operations_layer
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_notification_outbox"
down_revision: str | None = "0038_operations_layer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("notification_outbox"):
        return
    dt = sa.DateTime(timezone=True)
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("outbox_uid", sa.String(64), nullable=False, unique=True),
        sa.Column("incident_uid", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.String(20), nullable=False),
        sa.Column("route_uid", sa.String(64), nullable=False),
        sa.Column("escalation_level", sa.Integer(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("destination_ref", sa.String(96), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", dt, nullable=False),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", dt, nullable=True),
        sa.Column("delivered_at", dt, nullable=True),
        sa.Column("last_error_code", sa.String(96), nullable=True),
        sa.Column("created_at", dt, nullable=False),
        sa.Column("updated_at", dt, nullable=False),
    )
    op.create_index(
        "ix_notification_outbox_outbox_uid",
        "notification_outbox",
        ["outbox_uid"],
        unique=True,
    )
    op.create_index(
        "ux_notification_outbox_dedup",
        "notification_outbox",
        ["dedup_key"],
        unique=True,
    )
    op.create_index(
        "ix_notification_outbox_due",
        "notification_outbox",
        ["state", "available_at"],
    )
    op.create_index(
        "ix_notification_outbox_lease",
        "notification_outbox",
        ["state", "lease_expires_at"],
    )
    op.create_index(
        "ix_notification_outbox_incident",
        "notification_outbox",
        ["incident_uid", "created_at"],
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("notification_outbox"):
        op.drop_table("notification_outbox")
