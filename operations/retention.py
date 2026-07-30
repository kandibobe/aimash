"""Retention for non-audit operational tables.

Proposal, approval-vote, role and external-identity evidence is deliberately excluded. Monetary
audit rows remain governed by the existing manual cold-archive policy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from core.config import settings
from core.logging import log
from db.models import (
    ChannelMetricSnapshot,
    ManagedExperiment,
    OperationalDecision,
    OpsIncident,
    PacingSnapshot,
    RevenueEvent,
)
from db.session import Session, db_dt


async def purge_operational_rows(*, now: datetime | None = None) -> dict[str, int]:
    point = now or datetime.now(timezone.utc)
    result = {
        "operational_decisions": 0,
        "ops_incidents": 0,
        "pacing_snapshots": 0,
        "managed_experiments": 0,
        "revenue_events": 0,
        "channel_metric_snapshots": 0,
    }

    async def sweep(key: str, days: int, model, *conditions) -> None:
        if days <= 0:
            return
        cutoff_dt = db_dt(point - timedelta(days=days))
        condition = model.created_at < cutoff_dt
        try:
            async with Session() as session:
                query = delete(model).where(condition, *conditions)
                executed = await session.execute(query)
                await session.commit()
                result[key] = int(getattr(executed, "rowcount", 0) or 0)
        except Exception as exc:  # noqa: BLE001 - one table must not cancel the rest
            log.warning("operations retention failed for %s (%s)", key, type(exc).__name__)

    await sweep(
        "operational_decisions",
        settings.operations_retain_days,
        OperationalDecision,
        OperationalDecision.status.in_(("rejected", "applied", "expired")),
    )
    await sweep(
        "ops_incidents",
        settings.operations_retain_days,
        OpsIncident,
        OpsIncident.status == "resolved",
    )
    await sweep("pacing_snapshots", settings.operations_retain_days, PacingSnapshot)
    await sweep(
        "managed_experiments",
        settings.operations_retain_days,
        ManagedExperiment,
        ManagedExperiment.status.in_(("completed", "cancelled")),
    )
    await sweep("revenue_events", settings.revenue_events_retain_days, RevenueEvent)
    await sweep(
        "channel_metric_snapshots",
        settings.channel_metrics_retain_days,
        ChannelMetricSnapshot,
    )
    return result
