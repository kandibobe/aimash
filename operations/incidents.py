"""Incident deduplication, acknowledgement, snooze and escalation state."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.exc import IntegrityError

from core.logging import redact_text
from db.models import NotificationOutbox, OpsIncident
from db.session import Session, db_dt
from operations.decisions import _clean
from operations.types import IncidentInput

ACTIVE = frozenset({"open", "acknowledged", "snoozed"})
ALL_STATUSES = ACTIVE | {"resolved"}
_TRANSITIONS = {
    "acknowledged": frozenset({"open", "snoozed"}),
    "snoozed": frozenset({"open", "acknowledged"}),
    "resolved": frozenset({"open", "acknowledged", "snoozed"}),
    "open": frozenset({"snoozed", "resolved"}),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def incident_fingerprint(spec: IncidentInput) -> str:
    payload = {
        "customer_id": spec.customer_id,
        "kind": spec.kind,
        "fields": _clean(spec.fingerprint_fields),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


async def record_incident(spec: IncidentInput) -> OpsIncident:
    """Fold another event into the stable incident identity, reopening a resolved incident."""
    now = utcnow()
    fingerprint = incident_fingerprint(spec)

    async def refresh_existing() -> OpsIncident | None:
        point = db_dt(now)
        was_resolved = OpsIncident.status == "resolved"
        expired_snooze = and_(
            OpsIncident.status == "snoozed",
            OpsIncident.snoozed_until.is_not(None),
            OpsIncident.snoozed_until <= point,
        )
        reopen = or_(was_resolved, expired_snooze)
        async with Session() as session:
            result = await session.execute(
                update(OpsIncident)
                .where(
                    OpsIncident.customer_id == spec.customer_id,
                    OpsIncident.fingerprint == fingerprint,
                )
                .values(
                    decision_uid=spec.decision_uid or OpsIncident.decision_uid,
                    severity=spec.severity,
                    title=redact_text(spec.title),
                    evidence=_clean(spec.evidence),
                    status=case((reopen, "open"), else_=OpsIncident.status),
                    occurrence_count=OpsIncident.occurrence_count + 1,
                    last_seen_at=point,
                    updated_at=point,
                    resolved_by=case((was_resolved, None), else_=OpsIncident.resolved_by),
                    resolved_at=case((was_resolved, None), else_=OpsIncident.resolved_at),
                    acknowledged_by=case((was_resolved, None), else_=OpsIncident.acknowledged_by),
                    acknowledged_at=case((was_resolved, None), else_=OpsIncident.acknowledged_at),
                    snoozed_until=case((reopen, None), else_=OpsIncident.snoozed_until),
                    escalation_level=case((was_resolved, 0), else_=OpsIncident.escalation_level),
                    last_notified_at=case((was_resolved, None), else_=OpsIncident.last_notified_at),
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                await session.rollback()
                return None
            await session.commit()
            return (
                await session.execute(
                    select(OpsIncident).where(
                        OpsIncident.customer_id == spec.customer_id,
                        OpsIncident.fingerprint == fingerprint,
                    )
                )
            ).scalar_one()

    existing = await refresh_existing()
    if existing is not None:
        return existing

    async with Session() as session:
        row = OpsIncident(
            incident_uid=f"inc_{secrets.token_hex(12)}",
            customer_id=spec.customer_id,
            decision_uid=spec.decision_uid,
            fingerprint=fingerprint,
            kind=spec.kind,
            severity=spec.severity,
            title=redact_text(spec.title),
            evidence=_clean(spec.evidence),
            status="open",
            occurrence_count=1,
            escalation_level=0,
            first_seen_at=db_dt(now),
            last_seen_at=db_dt(now),
            created_at=db_dt(now),
            updated_at=db_dt(now),
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            winner = await refresh_existing()
            if winner is None:
                raise RuntimeError("incident dedup conflict without an existing row") from exc
            return winner
        await session.refresh(row)
        return row


async def transition_incident(
    incident_uid: str,
    target: str,
    *,
    actor_user_id: int,
    customer_id: str | None = None,
    snoozed_until: datetime | None = None,
    assigned_to: int | None = None,
) -> bool:
    if target not in _TRANSITIONS:
        raise ValueError(f"unsupported incident transition: {target}")
    now = utcnow()
    if target == "snoozed":
        if snoozed_until is None or snoozed_until <= now:
            raise ValueError("snoozed_until must be in the future")
    elif snoozed_until is not None:
        raise ValueError("snoozed_until is valid only for snoozed")

    values: dict[str, Any] = {"status": target, "updated_at": db_dt(now)}
    if assigned_to is not None:
        values["assigned_to"] = assigned_to
    if target == "acknowledged":
        values.update(acknowledged_by=actor_user_id, acknowledged_at=db_dt(now))
    elif target == "snoozed":
        values["snoozed_until"] = db_dt(snoozed_until)
    elif target == "resolved":
        values.update(resolved_by=actor_user_id, resolved_at=db_dt(now), snoozed_until=None)
    elif target == "open":
        values.update(
            snoozed_until=None,
            resolved_by=None,
            resolved_at=None,
            acknowledged_by=None,
            acknowledged_at=None,
        )

    conditions = [
        OpsIncident.incident_uid == incident_uid,
        OpsIncident.status.in_(_TRANSITIONS[target]),
    ]
    if customer_id is not None:
        conditions.append(OpsIncident.customer_id == customer_id)

    async with Session() as session:
        result = await session.execute(update(OpsIncident).where(*conditions).values(**values))
        if int(getattr(result, "rowcount", 0) or 0) == 1:
            if target in {"resolved", "snoozed"}:
                await session.execute(
                    update(NotificationOutbox)
                    .where(
                        NotificationOutbox.incident_uid == incident_uid,
                        NotificationOutbox.state == "pending",
                    )
                    .values(
                        state="cancelled",
                        last_error_code=None,
                        updated_at=db_dt(now),
                    )
                )
            await session.commit()
            return True
        await session.rollback()
        return False


async def list_incidents(
    customer_id: str,
    *,
    statuses: set[str] | None = None,
    limit: int = 100,
) -> list[OpsIncident]:
    chosen = statuses or set(ACTIVE)
    if not chosen <= ALL_STATUSES:
        raise ValueError(f"unknown incident status: {sorted(chosen - ALL_STATUSES)}")
    async with Session() as session:
        return list(
            (
                await session.execute(
                    select(OpsIncident)
                    .where(
                        OpsIncident.customer_id == customer_id,
                        OpsIncident.status.in_(chosen),
                    )
                    .order_by(
                        case(
                            (OpsIncident.severity == "critical", 0),
                            (OpsIncident.severity == "warning", 1),
                            else_=2,
                        ),
                        OpsIncident.last_seen_at.desc(),
                    )
                    .limit(max(1, min(int(limit), 500)))
                )
            )
            .scalars()
            .all()
        )
