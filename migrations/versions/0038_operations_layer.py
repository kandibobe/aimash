"""Unified ad-ops control plane.

Adds persistent decisions/incidents, budget pacing, experiments, RBAC/four-eyes evidence,
PII-free CRM revenue feedback, provider-neutral channel metrics, and versioned playbooks.
None of these tables authorizes a Google Ads mutation; the existing proposal/confirm/audit path
remains the only execution boundary.

Revision ID: 0038_operations_layer
Revises: 0037_proposal_risk_tier
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_operations_layer"
down_revision: str | None = "0037_proposal_risk_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create(
    name: str, *columns, indexes: tuple[tuple[str, tuple[str, ...], bool], ...] = ()
) -> None:
    if sa.inspect(op.get_bind()).has_table(name):
        return
    op.create_table(name, *columns)
    for index_name, fields, unique in indexes:
        op.create_index(index_name, name, list(fields), unique=unique)


def upgrade() -> None:
    dt = sa.DateTime(timezone=True)
    _create(
        "operational_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("decision_uid", sa.String(64), nullable=False, unique=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("customer_id", sa.String(20), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(96), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("active_fingerprint", sa.String(64), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column("recommended_operation", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("assigned_to", sa.BigInteger(), nullable=True),
        sa.Column("snoozed_until", dt, nullable=True),
        sa.Column("expires_at", dt, nullable=True),
        sa.Column("proposal_confirmation_id", sa.String(64), nullable=True),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", dt, nullable=False),
        sa.Column("last_seen_at", dt, nullable=False),
        sa.Column("created_at", dt, nullable=False),
        sa.Column("updated_at", dt, nullable=False),
        indexes=(
            ("ix_operational_decisions_decision_uid", ("decision_uid",), True),
            ("ix_operational_decisions_chat_id", ("chat_id",), False),
            ("ix_operational_decisions_status", ("status",), False),
            ("ix_operational_decisions_assigned_to", ("assigned_to",), False),
            (
                "ix_operational_decisions_proposal_confirmation_id",
                ("proposal_confirmation_id",),
                False,
            ),
            (
                "ix_operational_decisions_queue",
                ("customer_id", "status", "severity", "created_at"),
                False,
            ),
            (
                "ix_operational_decisions_fingerprint",
                ("customer_id", "fingerprint", "last_seen_at"),
                False,
            ),
            ("ux_operational_decisions_active_fingerprint", ("active_fingerprint",), True),
        ),
    )
    _create(
        "ops_incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_uid", sa.String(64), nullable=False, unique=True),
        sa.Column("customer_id", sa.String(20), nullable=False),
        sa.Column("decision_uid", sa.String(64), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("assigned_to", sa.BigInteger(), nullable=True),
        sa.Column("acknowledged_by", sa.BigInteger(), nullable=True),
        sa.Column("acknowledged_at", dt, nullable=True),
        sa.Column("snoozed_until", dt, nullable=True),
        sa.Column("resolved_by", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", dt, nullable=True),
        sa.Column("escalation_level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_notified_at", dt, nullable=True),
        sa.Column("first_seen_at", dt, nullable=False),
        sa.Column("last_seen_at", dt, nullable=False),
        sa.Column("created_at", dt, nullable=False),
        sa.Column("updated_at", dt, nullable=False),
        indexes=(
            ("ix_ops_incidents_incident_uid", ("incident_uid",), True),
            ("ix_ops_incidents_decision_uid", ("decision_uid",), False),
            ("ix_ops_incidents_status", ("status",), False),
            (
                "ix_ops_incidents_queue",
                ("customer_id", "status", "severity", "last_seen_at"),
                False,
            ),
            ("ux_ops_incidents_fingerprint", ("customer_id", "fingerprint"), True),
        ),
    )
    _create(
        "budget_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_uid", sa.String(64), nullable=False, unique=True),
        sa.Column("customer_id", sa.String(20), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(96), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("period_start", sa.String(10), nullable=False),
        sa.Column("period_end", sa.String(10), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("planned_spend_micros", sa.BigInteger(), nullable=False),
        sa.Column("monthly_ceiling_micros", sa.BigInteger(), nullable=True),
        sa.Column("daily_ceiling_micros", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", dt, nullable=False),
        sa.Column("updated_at", dt, nullable=False),
        indexes=(
            ("ix_budget_plans_plan_uid", ("plan_uid",), True),
            ("ix_budget_plans_active", ("active",), False),
            (
                "ix_budget_plans_scope_period",
                ("customer_id", "scope_type", "scope_id", "period_start", "period_end"),
                False,
            ),
            (
                "ux_budget_plans_scope_version",
                ("customer_id", "scope_type", "scope_id", "period_start", "period_end", "version"),
                True,
            ),
        ),
    )
    _create(
        "pacing_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_uid", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.String(20), nullable=False),
        sa.Column("as_of_date", sa.String(10), nullable=False),
        sa.Column("spend_to_date_micros", sa.BigInteger(), nullable=False),
        sa.Column("expected_to_date_micros", sa.BigInteger(), nullable=False),
        sa.Column("projected_spend_micros", sa.BigInteger(), nullable=False),
        sa.Column("variance_micros", sa.BigInteger(), nullable=False),
        sa.Column("variance_ratio", sa.Float(), nullable=False),
        sa.Column("recommended_daily_budget_micros", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", dt, nullable=False),
        indexes=(
            ("ix_pacing_snapshots_plan_asof", ("plan_uid", "as_of_date"), True),
            ("ix_pacing_snapshots_customer_asof", ("customer_id", "as_of_date", "status"), False),
        ),
    )
    _create(
        "managed_experiments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("experiment_uid", sa.String(64), nullable=False, unique=True),
        sa.Column("customer_id", sa.String(20), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("control", sa.JSON(), nullable=False),
        sa.Column("treatment", sa.JSON(), nullable=False),
        sa.Column("primary_metric", sa.String(32), nullable=False),
        sa.Column("success_direction", sa.String(8), nullable=False),
        sa.Column("success_threshold", sa.Float(), nullable=False),
        sa.Column("minimum_sample", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("rollback_trigger", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", dt, nullable=False),
        sa.Column("updated_at", dt, nullable=False),
        indexes=(
            ("ix_managed_experiments_experiment_uid", ("experiment_uid",), True),
            ("ix_managed_experiments_status", ("status",), False),
            ("ix_managed_experiments_queue", ("customer_id", "status", "end_date"), False),
        ),
    )
    _create(
        "role_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.String(20), nullable=False, server_default="*"),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", dt, nullable=False),
        indexes=(
            ("ux_role_assignments_scope", ("user_id", "customer_id", "role"), True),
            ("ix_role_assignments_customer_role", ("customer_id", "role"), False),
        ),
    )
    _create(
        "approval_votes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("confirmation_id", sa.String(64), nullable=False),
        sa.Column("approver_user_id", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", dt, nullable=False),
        indexes=(
            ("ux_approval_votes_actor", ("confirmation_id", "approver_user_id"), True),
            ("ix_approval_votes_confirmation", ("confirmation_id", "decision"), False),
        ),
    )
    _create(
        "revenue_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id_hash", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.String(20), nullable=False),
        sa.Column("campaign_id", sa.String(32), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False, server_default="google"),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("qualified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revenue_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("occurred_at", dt, nullable=False),
        sa.Column("created_at", dt, nullable=False),
        indexes=(
            ("ux_revenue_events_source_id", ("source", "external_id_hash"), True),
            ("ix_revenue_events_customer_time", ("customer_id", "occurred_at"), False),
            (
                "ix_revenue_events_campaign_time",
                ("customer_id", "campaign_id", "occurred_at"),
                False,
            ),
        ),
    )
    _create(
        "channel_metric_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("customer_id", sa.String(20), nullable=False),
        sa.Column("external_account_id", sa.String(96), nullable=False),
        sa.Column("campaign_id", sa.String(96), nullable=False, server_default=""),
        sa.Column("metric_date", sa.String(10), nullable=False),
        sa.Column("spend_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("impressions", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("clicks", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("conversions", sa.Float(), nullable=False, server_default="0"),
        sa.Column("revenue_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", dt, nullable=False),
        indexes=(
            (
                "ux_channel_metric_scope_date",
                ("channel", "customer_id", "external_account_id", "campaign_id", "metric_date"),
                True,
            ),
            ("ix_channel_metric_customer_date", ("customer_id", "metric_date"), False),
        ),
    )
    _create(
        "playbook_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("playbook_uid", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(96), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("rule", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", dt, nullable=False),
        indexes=(
            ("ix_playbook_versions_playbook_uid", ("playbook_uid",), True),
            ("ux_playbook_versions_name_version", ("name", "version"), True),
            ("ix_playbook_versions_enabled", ("enabled", "name"), False),
        ),
    )
    _create(
        "external_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("issuer_hash", sa.String(64), nullable=False),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", dt, nullable=False),
        sa.Column("last_seen_at", dt, nullable=False),
        indexes=(
            ("ux_external_identities_subject", ("provider", "issuer_hash", "subject_hash"), True),
            ("ix_external_identities_user", ("user_id", "active"), False),
        ),
    )
    _create(
        "notification_routes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("route_uid", sa.String(64), nullable=False, unique=True),
        sa.Column("customer_id", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("destination_ref", sa.String(96), nullable=False),
        sa.Column("severities", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", dt, nullable=False),
        indexes=(
            ("ix_notification_routes_route_uid", ("route_uid",), True),
            ("ux_notification_routes_scope", ("customer_id", "channel", "destination_ref"), True),
            ("ix_notification_routes_enabled", ("customer_id", "enabled"), False),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "notification_routes",
        "external_identities",
        "playbook_versions",
        "channel_metric_snapshots",
        "revenue_events",
        "approval_votes",
        "role_assignments",
        "managed_experiments",
        "pacing_snapshots",
        "budget_plans",
        "ops_incidents",
        "operational_decisions",
    ):
        if sa.inspect(bind).has_table(table):
            op.drop_table(table)
