"""Provider-neutral portfolio metrics and advisory cross-channel allocation."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.config import normalize_customer_id
from db.models import ChannelMetricSnapshot, OperationalDecision, OpsIncident
from db.session import Session, db_dt
from operations.types import ChannelMetricInput


async def upsert_channel_metric(spec: ChannelMetricInput) -> ChannelMetricSnapshot:
    values = {
        "spend_micros": spec.spend_micros,
        "impressions": spec.impressions,
        "clicks": spec.clicks,
        "conversions": spec.conversions,
        "revenue_micros": spec.revenue_micros,
        "currency": spec.currency,
        "source": spec.source,
    }
    async with Session() as session:
        row = (
            await session.execute(
                select(ChannelMetricSnapshot).where(
                    ChannelMetricSnapshot.channel == spec.channel,
                    ChannelMetricSnapshot.customer_id == spec.customer_id,
                    ChannelMetricSnapshot.external_account_id == spec.external_account_id,
                    ChannelMetricSnapshot.campaign_id == spec.campaign_id,
                    ChannelMetricSnapshot.metric_date == spec.metric_date.isoformat(),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = ChannelMetricSnapshot(
                channel=spec.channel,
                customer_id=spec.customer_id,
                external_account_id=spec.external_account_id,
                campaign_id=spec.campaign_id,
                metric_date=spec.metric_date.isoformat(),
                created_at=db_dt(datetime.now(timezone.utc)),
                **values,
            )
            session.add(row)
        else:
            for field, value in values.items():
                setattr(row, field, value)
        try:
            await session.commit()
        except IntegrityError:
            # A concurrent importer won the unique key. Retry as a deterministic update.
            await session.rollback()
            row = (
                await session.execute(
                    select(ChannelMetricSnapshot).where(
                        ChannelMetricSnapshot.channel == spec.channel,
                        ChannelMetricSnapshot.customer_id == spec.customer_id,
                        ChannelMetricSnapshot.external_account_id == spec.external_account_id,
                        ChannelMetricSnapshot.campaign_id == spec.campaign_id,
                        ChannelMetricSnapshot.metric_date == spec.metric_date.isoformat(),
                    )
                )
            ).scalar_one()
            for field, value in values.items():
                setattr(row, field, value)
            await session.commit()
        await session.refresh(row)
        return row


async def portfolio_summary(*, date_from: str, date_to: str, customer_ids: set[str]) -> dict:
    """Aggregate per currency; values in different currencies are never silently added."""
    allowed = {normalize_customer_id(value) for value in customer_ids}
    if not allowed or any(not 6 <= len(value) <= 20 for value in allowed):
        raise PermissionError("portfolio_summary requires an explicit non-empty customer scope")
    async with Session() as session:
        rows = list(
            (
                await session.execute(
                    select(ChannelMetricSnapshot).where(
                        ChannelMetricSnapshot.metric_date >= date_from,
                        ChannelMetricSnapshot.metric_date <= date_to,
                        ChannelMetricSnapshot.customer_id.in_(allowed),
                    )
                )
            )
            .scalars()
            .all()
        )
        critical_incidents = list(
            (
                await session.execute(
                    select(OpsIncident).where(
                        OpsIncident.status.in_(("open", "acknowledged")),
                        OpsIncident.severity == "critical",
                        OpsIncident.customer_id.in_(allowed),
                    )
                )
            )
            .scalars()
            .all()
        )
        open_decisions = list(
            (
                await session.execute(
                    select(OperationalDecision).where(
                        OperationalDecision.status.in_(("new", "acknowledged", "snoozed")),
                        OperationalDecision.customer_id.in_(allowed),
                    )
                )
            )
            .scalars()
            .all()
        )

    buckets: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (row.customer_id, row.channel, row.currency)
        bucket = buckets.setdefault(
            key,
            {
                "customer_id": row.customer_id,
                "channel": row.channel,
                "currency": row.currency,
                "spend_micros": 0,
                "revenue_micros": 0,
                "clicks": 0,
                "conversions": 0.0,
            },
        )
        bucket["spend_micros"] += row.spend_micros
        bucket["revenue_micros"] += row.revenue_micros
        bucket["clicks"] += row.clicks
        bucket["conversions"] += row.conversions
    for bucket in buckets.values():
        spend = bucket["spend_micros"]
        conversions = bucket["conversions"]
        bucket["roas"] = round(bucket["revenue_micros"] / spend, 6) if spend else None
        bucket["cpa_micros"] = round(spend / conversions) if conversions else None
    return {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": sorted(
            buckets.values(), key=lambda item: (item["currency"], -item["spend_micros"])
        ),
        "critical_incidents": len(critical_incidents),
        "open_decisions": len(open_decisions),
    }


def recommend_reallocation(metrics: list[dict], *, max_shift_ratio: float = 0.1) -> list[dict]:
    """Advisory only; never combines currencies or calls a channel API."""
    if not 0 < max_shift_ratio <= 0.25:
        raise ValueError("max_shift_ratio must be in (0, 0.25]")
    recommendations: list[dict] = []
    by_customer_currency: dict[tuple[str, str], list[dict]] = {}
    for item in metrics:
        if item.get("roas") is not None and int(item.get("spend_micros", 0)) > 0:
            key = (str(item["customer_id"]), str(item["currency"]))
            by_customer_currency.setdefault(key, []).append(item)
    for (customer_id, currency), items in by_customer_currency.items():
        if len(items) < 2:
            continue
        source = min(items, key=lambda item: float(item["roas"]))
        target = max(items, key=lambda item: float(item["roas"]))
        if source is target or float(target["roas"]) <= float(source["roas"]):
            continue
        amount = round(int(source["spend_micros"]) * max_shift_ratio)
        recommendations.append(
            {
                "currency": currency,
                "from": {"customer_id": customer_id, "channel": source["channel"]},
                "to": {"customer_id": customer_id, "channel": target["channel"]},
                "amount_micros": amount,
                "reason": "higher observed ROAS in the selected period",
                "approval_required": True,
            }
        )
    return recommendations
