"""Durable at-least-once notification delivery for incident escalations.

The enqueue transaction advances the incident escalation cursor and creates one immutable delivery
row per effective route.  A worker then leases rows and records success or a bounded retry.  Sending
is deliberately before the delivered commit: a process crash can duplicate a message, but cannot
silently lose it.  Transports receive ``dedup_key`` so channels with idempotency support can collapse
that narrow crash window.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from core.logging import log, redact_text
from db.models import NotificationOutbox, NotificationRoute, OpsIncident
from db.session import Session, db_dt
from operations.routing import AlertRouter, Notification, Route

DELIVERABLE_SEVERITIES = frozenset({"warning", "critical"})
TERMINAL_STATES = frozenset({"delivered", "dead", "cancelled"})
_ERROR_CODE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,95}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def _due(
    incident: OpsIncident,
    *,
    point: datetime,
    critical_after: timedelta,
    warning_after: timedelta,
    cooldown: timedelta,
) -> bool:
    if incident.status not in {"open", "acknowledged"}:
        return False
    if incident.severity not in DELIVERABLE_SEVERITIES:
        return False
    threshold = critical_after if incident.severity == "critical" else warning_after
    if point - _aware(incident.first_seen_at) < threshold:
        return False
    if incident.last_notified_at and point - _aware(incident.last_notified_at) < cooldown:
        return False
    return True


def _effective_routes(rows: list[NotificationRoute], *, severity: str) -> list[NotificationRoute]:
    """Prefer a customer route over an identical global route and reject corrupt rows."""
    chosen: dict[tuple[str, str], NotificationRoute] = {}
    for row in sorted(rows, key=lambda item: (item.customer_id != "*", item.id), reverse=True):
        key = (row.channel, row.destination_ref)
        if key in chosen or severity not in set(row.severities or []):
            continue
        try:
            Route(row.channel, row.destination_ref, frozenset({severity}))
        except (TypeError, ValueError) as exc:
            log.warning(
                "notification route %s rejected (%s)",
                row.route_uid,
                type(exc).__name__,
            )
            continue
        chosen[key] = row
    return list(chosen.values())


def _dedup_key(
    *, incident_uid: str, occurrence_count: int, escalation_level: int, route_uid: str
) -> str:
    raw = f"{incident_uid}:{occurrence_count}:{escalation_level}:{route_uid}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _body(incident: OpsIncident, *, escalation_level: int) -> str:
    return redact_text(
        "\n".join(
            (
                f"Customer: {incident.customer_id}",
                f"Incident: {incident.incident_uid}",
                f"Occurrences: {incident.occurrence_count}",
                f"Escalation: {escalation_level}",
            )
        )
    )


async def enqueue_due_escalations(
    *,
    now: datetime | None = None,
    critical_after: timedelta = timedelta(minutes=15),
    warning_after: timedelta = timedelta(hours=4),
    cooldown: timedelta = timedelta(hours=1),
    limit: int = 100,
) -> list[NotificationOutbox]:
    """Atomically advance due incidents and persist all matching route deliveries."""
    if min(critical_after, warning_after, cooldown) < timedelta(0):
        raise ValueError("escalation delays cannot be negative")
    point = now or utcnow()
    capped = max(1, min(int(limit), 500))
    async with Session() as session:
        candidate_ids = list(
            (
                await session.execute(
                    select(OpsIncident.id)
                    .where(
                        OpsIncident.status.in_(("open", "acknowledged")),
                        OpsIncident.severity.in_(DELIVERABLE_SEVERITIES),
                        OpsIncident.first_seen_at
                        <= db_dt(point - min(critical_after, warning_after)),
                    )
                    .order_by(OpsIncident.first_seen_at)
                    .limit(capped)
                )
            ).scalars()
        )

    enqueued: list[NotificationOutbox] = []
    for incident_id in candidate_ids:
        async with Session() as session:
            incident = (
                await session.execute(select(OpsIncident).where(OpsIncident.id == incident_id))
            ).scalar_one_or_none()
            if incident is None or not _due(
                incident,
                point=point,
                critical_after=critical_after,
                warning_after=warning_after,
                cooldown=cooldown,
            ):
                continue
            route_rows = list(
                (
                    await session.execute(
                        select(NotificationRoute).where(
                            NotificationRoute.customer_id.in_((incident.customer_id, "*")),
                            NotificationRoute.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            routes = _effective_routes(route_rows, severity=incident.severity)
            if not routes:
                continue

            next_level = incident.escalation_level + 1
            claimed = await session.execute(
                update(OpsIncident)
                .where(
                    OpsIncident.id == incident.id,
                    OpsIncident.status == incident.status,
                    OpsIncident.escalation_level == incident.escalation_level,
                )
                .values(
                    escalation_level=next_level,
                    last_notified_at=db_dt(point),
                    updated_at=db_dt(point),
                )
            )
            if int(getattr(claimed, "rowcount", 0) or 0) != 1:
                await session.rollback()
                continue

            rows: list[NotificationOutbox] = []
            for route in routes:
                row = NotificationOutbox(
                    outbox_uid=f"out_{secrets.token_hex(12)}",
                    incident_uid=incident.incident_uid,
                    customer_id=incident.customer_id,
                    route_uid=route.route_uid,
                    escalation_level=next_level,
                    occurrence_count=incident.occurrence_count,
                    channel=route.channel,
                    destination_ref=route.destination_ref,
                    dedup_key=_dedup_key(
                        incident_uid=incident.incident_uid,
                        occurrence_count=incident.occurrence_count,
                        escalation_level=next_level,
                        route_uid=route.route_uid,
                    ),
                    severity=incident.severity,
                    title=redact_text(incident.title),
                    body=_body(incident, escalation_level=next_level),
                    state="pending",
                    attempts=0,
                    available_at=db_dt(point),
                    created_at=db_dt(point),
                    updated_at=db_dt(point),
                )
                session.add(row)
                rows.append(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                continue
            enqueued.extend(rows)
    return enqueued


async def claim_delivery_batch(
    *,
    now: datetime | None = None,
    lease_for: timedelta = timedelta(minutes=2),
    max_attempts: int = 5,
    limit: int = 100,
) -> list[NotificationOutbox]:
    """Lease due rows with per-row CAS so concurrent workers cannot send the same live lease."""
    if lease_for <= timedelta(0):
        raise ValueError("lease_for must be positive")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    point = now or utcnow()
    capped = max(1, min(int(limit), 500))
    due = or_(
        and_(
            NotificationOutbox.state == "pending",
            NotificationOutbox.available_at <= db_dt(point),
        ),
        and_(
            NotificationOutbox.state == "leased",
            NotificationOutbox.lease_expires_at.is_not(None),
            NotificationOutbox.lease_expires_at <= db_dt(point),
        ),
    )
    active_incident = NotificationOutbox.incident_uid.in_(
        select(OpsIncident.incident_uid).where(OpsIncident.status.in_(("open", "acknowledged")))
    )
    async with Session() as session:
        await session.execute(
            update(NotificationOutbox)
            .where(due, ~active_incident)
            .values(
                state="cancelled",
                lease_token=None,
                lease_expires_at=None,
                last_error_code=None,
                updated_at=db_dt(point),
            )
        )
        await session.execute(
            update(NotificationOutbox)
            .where(due, active_incident, NotificationOutbox.attempts >= max_attempts)
            .values(
                state="dead",
                lease_token=None,
                lease_expires_at=None,
                last_error_code="LeaseExpired",
                updated_at=db_dt(point),
            )
        )
        candidate_ids = list(
            (
                await session.execute(
                    select(NotificationOutbox.id)
                    .where(due, active_incident, NotificationOutbox.attempts < max_attempts)
                    .order_by(NotificationOutbox.available_at, NotificationOutbox.id)
                    .limit(capped)
                )
            ).scalars()
        )
        await session.commit()

    claimed_rows: list[NotificationOutbox] = []
    for row_id in candidate_ids:
        token = f"lease_{secrets.token_hex(12)}"
        async with Session() as session:
            claimed = await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id == row_id,
                    due,
                    active_incident,
                    NotificationOutbox.attempts < max_attempts,
                )
                .values(
                    state="leased",
                    attempts=NotificationOutbox.attempts + 1,
                    lease_token=token,
                    lease_expires_at=db_dt(point + lease_for),
                    updated_at=db_dt(point),
                )
            )
            if int(getattr(claimed, "rowcount", 0) or 0) != 1:
                await session.rollback()
                continue
            await session.commit()
            claimed_rows.append(
                (
                    await session.execute(
                        select(NotificationOutbox).where(
                            NotificationOutbox.id == row_id,
                            NotificationOutbox.lease_token == token,
                        )
                    )
                ).scalar_one()
            )
    return claimed_rows


async def mark_delivery_succeeded(
    outbox_uid: str, *, lease_token: str, now: datetime | None = None
) -> bool:
    point = now or utcnow()
    async with Session() as session:
        result = await session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.outbox_uid == outbox_uid,
                NotificationOutbox.state == "leased",
                NotificationOutbox.lease_token == lease_token,
            )
            .values(
                state="delivered",
                delivered_at=db_dt(point),
                lease_token=None,
                lease_expires_at=None,
                last_error_code=None,
                updated_at=db_dt(point),
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            await session.rollback()
            return False
        await session.commit()
        return True


async def mark_delivery_cancelled(
    outbox_uid: str, *, lease_token: str, now: datetime | None = None
) -> bool:
    point = now or utcnow()
    async with Session() as session:
        result = await session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.outbox_uid == outbox_uid,
                NotificationOutbox.state == "leased",
                NotificationOutbox.lease_token == lease_token,
            )
            .values(
                state="cancelled",
                lease_token=None,
                lease_expires_at=None,
                last_error_code=None,
                updated_at=db_dt(point),
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            await session.rollback()
            return False
        await session.commit()
        return True


def _error_code(value: str) -> str:
    clean = value.strip()
    return clean if _ERROR_CODE_RE.fullmatch(clean) else "DeliveryError"


async def mark_delivery_failed(
    outbox_uid: str,
    *,
    lease_token: str,
    error_code: str,
    now: datetime | None = None,
    max_attempts: int = 5,
    base_delay: timedelta = timedelta(seconds=30),
    max_delay: timedelta = timedelta(hours=1),
) -> str | None:
    """Release a lease to retry or dead-letter it; never persist exception messages."""
    if max_attempts < 1 or base_delay <= timedelta(0) or max_delay < base_delay:
        raise ValueError("invalid retry policy")
    point = now or utcnow()
    async with Session() as session:
        row = (
            await session.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.outbox_uid == outbox_uid,
                    NotificationOutbox.state == "leased",
                    NotificationOutbox.lease_token == lease_token,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        terminal = row.attempts >= max_attempts
        delay_seconds = min(
            max_delay.total_seconds(),
            base_delay.total_seconds() * (2 ** max(0, row.attempts - 1)),
        )
        row.state = "dead" if terminal else "pending"
        row.available_at = db_dt(point if terminal else point + timedelta(seconds=delay_seconds))
        row.lease_token = None
        row.lease_expires_at = None
        row.last_error_code = _error_code(error_code)
        row.updated_at = db_dt(point)
        await session.commit()
        return row.state


@dataclass(frozen=True)
class DeliveryResult:
    claimed: int = 0
    delivered: int = 0
    retrying: int = 0
    dead: int = 0
    cancelled: int = 0
    lost_lease: int = 0


async def _incident_is_active(incident_uid: str) -> bool:
    async with Session() as session:
        return (
            await session.execute(
                select(OpsIncident.id).where(
                    OpsIncident.incident_uid == incident_uid,
                    OpsIncident.status.in_(("open", "acknowledged")),
                )
            )
        ).first() is not None


async def deliver_outbox(
    router: AlertRouter,
    *,
    now: datetime | None = None,
    lease_for: timedelta = timedelta(minutes=2),
    max_attempts: int = 5,
    base_delay: timedelta = timedelta(seconds=30),
    limit: int = 100,
) -> DeliveryResult:
    rows = await claim_delivery_batch(
        now=now,
        lease_for=lease_for,
        max_attempts=max_attempts,
        limit=limit,
    )
    delivered = retrying = dead = cancelled = lost = 0
    for row in rows:
        if not await _incident_is_active(row.incident_uid):
            if await mark_delivery_cancelled(
                row.outbox_uid,
                lease_token=row.lease_token or "",
                now=now,
            ):
                cancelled += 1
            else:
                lost += 1
            continue
        notification = Notification(
            dedup_key=row.dedup_key,
            severity=row.severity,
            title=row.title,
            body=row.body,
        )
        route = Route(row.channel, row.destination_ref, frozenset({row.severity}))
        try:
            await router.deliver(notification, [route])
        except Exception as exc:  # noqa: BLE001 - each row has an independent durable retry
            state = await mark_delivery_failed(
                row.outbox_uid,
                lease_token=row.lease_token or "",
                error_code=type(exc).__name__,
                now=now,
                max_attempts=max_attempts,
                base_delay=base_delay,
            )
            retrying += state == "pending"
            dead += state == "dead"
            lost += state is None
        else:
            if await mark_delivery_succeeded(
                row.outbox_uid,
                lease_token=row.lease_token or "",
                now=now,
            ):
                delivered += 1
            else:
                lost += 1
    return DeliveryResult(
        claimed=len(rows),
        delivered=delivered,
        retrying=retrying,
        dead=dead,
        cancelled=cancelled,
        lost_lease=lost,
    )
