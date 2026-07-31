"""PII-free CRM/revenue feedback ingestion and campaign quality diagnostics."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError

from core.config import settings
from db.models import RevenueEvent
from db.session import Session, db_dt
from operations.decisions import create_or_refresh_decision
from operations.types import DecisionInput, RevenueEventInput


def external_id_digest(source: str, external_id: str) -> str:
    """Keyed stable dedup value; raw/guessable CRM identifiers cannot be dictionary-hashed."""
    root_key = settings.pseudonymization_hmac_key.get_secret_value()
    if len(root_key.encode()) < 32:
        raise RuntimeError("PSEUDONYMIZATION_HMAC_KEY must contain at least 32 bytes")
    digest_key = hmac.new(root_key.encode(), b"aimash:revenue-id:v1", hashlib.sha256).digest()
    message = f"{source.strip().lower()}\0{external_id}".encode()
    return hmac.new(digest_key, message, hashlib.sha256).hexdigest()


async def ingest_revenue_event(spec: RevenueEventInput) -> tuple[RevenueEvent, bool]:
    digest = external_id_digest(spec.source, spec.external_id)
    now = db_dt(datetime.now(timezone.utc))
    row = RevenueEvent(
        source=spec.source.lower(),
        external_id_hash=digest,
        customer_id=spec.customer_id,
        campaign_id=spec.campaign_id,
        channel=spec.channel,
        stage=spec.stage.lower(),
        qualified=spec.qualified,
        revenue_micros=spec.revenue_micros,
        currency=spec.currency,
        occurred_at=db_dt(spec.occurred_at),
        created_at=now,
    )
    async with Session() as session:
        session.add(row)
        try:
            await session.commit()
            await session.refresh(row)
            return row, True
        except IntegrityError:
            await session.rollback()
            existing = (
                await session.execute(
                    select(RevenueEvent).where(
                        RevenueEvent.source == spec.source.lower(),
                        RevenueEvent.external_id_hash == digest,
                    )
                )
            ).scalar_one()
            return existing, False


async def campaign_revenue_summary(
    customer_id: str, *, since: datetime, until: datetime
) -> list[dict]:
    """Aggregate without exposing CRM identifiers or contact-level data."""
    async with Session() as session:
        rows = (
            await session.execute(
                select(
                    RevenueEvent.channel,
                    RevenueEvent.campaign_id,
                    RevenueEvent.currency,
                    func.count(RevenueEvent.id),
                    func.sum(case((RevenueEvent.qualified.is_(True), 1), else_=0)),
                    func.sum(RevenueEvent.revenue_micros),
                )
                .where(
                    RevenueEvent.customer_id == customer_id,
                    RevenueEvent.occurred_at >= db_dt(since),
                    RevenueEvent.occurred_at < db_dt(until),
                )
                .group_by(RevenueEvent.channel, RevenueEvent.campaign_id, RevenueEvent.currency)
            )
        ).all()
    return [
        {
            "channel": channel,
            "campaign_id": campaign_id,
            "currency": currency,
            "leads": int(leads or 0),
            "qualified_leads": int(qualified or 0),
            "qualified_rate": round(float(qualified or 0) / int(leads or 1), 6),
            "revenue_micros": int(revenue or 0),
        }
        for channel, campaign_id, currency, leads, qualified, revenue in rows
    ]


async def diagnose_lead_quality(
    *,
    customer_id: str,
    campaign_id: str,
    ads_conversions: float,
    crm_leads: int,
    qualified_leads: int,
    minimum_leads: int = 10,
    minimum_qualified_rate: float = 0.2,
) -> str | None:
    """Surface 'good in Ads, bad in CRM' as a decision, never as an optimization command."""
    if min(ads_conversions, crm_leads, qualified_leads) < 0:
        raise ValueError("metrics cannot be negative")
    if crm_leads < minimum_leads:
        return None
    rate = qualified_leads / crm_leads if crm_leads else 0.0
    if ads_conversions <= 0 or rate >= minimum_qualified_rate:
        return None
    decision = await create_or_refresh_decision(
        DecisionInput(
            customer_id=customer_id,
            source="crm",
            source_ref=campaign_id,
            category="lead_quality",
            severity="warning",
            title="Ads conversions are not producing enough qualified CRM leads",
            rationale=(
                f"Campaign reports {ads_conversions:g} Ads conversions, but only "
                f"{qualified_leads}/{crm_leads} CRM leads are qualified."
            ),
            recommended_action="Inspect lead sources, search terms and conversion goal quality.",
            confidence=0.95,
            evidence={
                "campaign_id": campaign_id,
                "ads_conversions": ads_conversions,
                "crm_leads": crm_leads,
                "qualified_leads": qualified_leads,
                "qualified_rate": round(rate, 6),
            },
            fingerprint_fields={"campaign_id": campaign_id},
        )
    )
    return decision.decision_uid
