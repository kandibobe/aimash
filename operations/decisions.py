"""Unified Decision / Action Queue.

Rows are advisory. ``approved`` means an operator accepted the recommendation; it does not create,
confirm, or execute a proposal. Google Ads changes still require the existing trusted human turn,
reply anchor, ConfirmStore CAS and audit path.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, exists, or_, select, update
from sqlalchemy.exc import IntegrityError

from core.logging import redact_text
from db.models import AuditLog, OperationalDecision, Proposal
from db.session import Session, db_dt
from operations.types import DecisionInput

ACTIVE = frozenset({"new", "acknowledged", "approved", "snoozed"})
TERMINAL = frozenset({"rejected", "applied", "expired"})
ALL_STATUSES = ACTIVE | TERMINAL
_TRANSITIONS: dict[str, frozenset[str]] = {
    "acknowledged": frozenset({"new", "snoozed"}),
    "approved": frozenset({"new", "acknowledged", "snoozed"}),
    "rejected": frozenset({"new", "acknowledged", "snoozed"}),
    "snoozed": frozenset({"new", "acknowledged"}),
    "expired": frozenset({"new", "acknowledged", "approved", "snoozed"}),
    "new": frozenset({"snoozed"}),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k)[:128]: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def decision_fingerprint(spec: DecisionInput) -> str:
    payload = {
        "customer_id": spec.customer_id,
        "source": spec.source,
        "source_ref": spec.source_ref,
        "category": spec.category,
        "title": spec.title.casefold(),
        "fields": _clean(spec.fingerprint_fields),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _refresh_active_decision(
    spec: DecisionInput, *, fingerprint: str, now: datetime
) -> OperationalDecision | None:
    """Atomically fold one occurrence into the active row, including concurrent detectors."""
    point = db_dt(now)
    wake = and_(
        OperationalDecision.status == "snoozed",
        OperationalDecision.snoozed_until.is_not(None),
        OperationalDecision.snoozed_until <= point,
    )
    async with Session() as session:
        result = await session.execute(
            update(OperationalDecision)
            .where(
                OperationalDecision.active_fingerprint == fingerprint,
                OperationalDecision.status.in_(ACTIVE),
            )
            .values(
                chat_id=spec.chat_id,
                source_ref=spec.source_ref,
                severity=spec.severity,
                title=redact_text(spec.title),
                rationale=redact_text(spec.rationale),
                recommended_action=redact_text(spec.recommended_action),
                recommended_operation=spec.recommended_operation,
                confidence=spec.confidence,
                evidence=_clean(spec.evidence),
                occurrence_count=OperationalDecision.occurrence_count + 1,
                last_seen_at=point,
                updated_at=point,
                status=case((wake, "new"), else_=OperationalDecision.status),
                snoozed_until=case((wake, None), else_=OperationalDecision.snoozed_until),
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            await session.rollback()
            return None
        await session.commit()
        return (
            await session.execute(
                select(OperationalDecision).where(
                    OperationalDecision.active_fingerprint == fingerprint
                )
            )
        ).scalar_one()


async def create_or_refresh_decision(spec: DecisionInput) -> OperationalDecision:
    """Deduplicate active signals and refresh their evidence/last-seen timestamp."""
    now = utcnow()
    fingerprint = decision_fingerprint(spec)
    existing = await _refresh_active_decision(spec, fingerprint=fingerprint, now=now)
    if existing is not None:
        return existing

    async with Session() as session:
        row = OperationalDecision(
            decision_uid=f"dec_{secrets.token_hex(12)}",
            chat_id=spec.chat_id,
            customer_id=spec.customer_id,
            source=spec.source,
            source_ref=spec.source_ref,
            fingerprint=fingerprint,
            active_fingerprint=fingerprint,
            category=spec.category,
            severity=spec.severity,
            title=redact_text(spec.title),
            rationale=redact_text(spec.rationale),
            recommended_action=redact_text(spec.recommended_action),
            recommended_operation=spec.recommended_operation,
            confidence=spec.confidence,
            evidence=_clean(spec.evidence),
            status="new",
            expires_at=db_dt(spec.expires_at) if spec.expires_at else None,
            occurrence_count=1,
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
            winner = await _refresh_active_decision(spec, fingerprint=fingerprint, now=now)
            if winner is None:
                raise RuntimeError("decision dedup conflict without an active winner") from exc
            return winner
        await session.refresh(row)
        return row


async def list_decisions(
    customer_id: str,
    *,
    statuses: set[str] | None = None,
    assigned_to: int | None = None,
    limit: int = 100,
) -> list[OperationalDecision]:
    chosen = statuses or set(ACTIVE)
    if not chosen <= ALL_STATUSES:
        raise ValueError(f"unknown decision status: {sorted(chosen - ALL_STATUSES)}")
    conditions = [
        OperationalDecision.customer_id == customer_id,
        OperationalDecision.status.in_(chosen),
    ]
    if assigned_to is not None:
        conditions.append(OperationalDecision.assigned_to == assigned_to)
    async with Session() as session:
        return list(
            (
                await session.execute(
                    select(OperationalDecision)
                    .where(*conditions)
                    .order_by(
                        case(
                            (OperationalDecision.severity == "critical", 0),
                            (OperationalDecision.severity == "warning", 1),
                            else_=2,
                        ),
                        OperationalDecision.last_seen_at.desc(),
                    )
                    .limit(max(1, min(int(limit), 500)))
                )
            )
            .scalars()
            .all()
        )


async def transition_decision(
    decision_uid: str,
    target: str,
    *,
    actor_user_id: int,
    customer_id: str | None = None,
    note: str | None = None,
    snoozed_until: datetime | None = None,
    assigned_to: int | None = None,
) -> bool:
    """Atomic lifecycle transition. It intentionally has no call into ``ads`` or ``confirm``."""
    if target not in _TRANSITIONS:
        raise ValueError(f"unsupported decision transition: {target}")
    if target == "snoozed":
        if snoozed_until is None or snoozed_until <= utcnow():
            raise ValueError("snoozed_until must be in the future")
    elif snoozed_until is not None:
        raise ValueError("snoozed_until is valid only for the snoozed state")
    values: dict[str, Any] = {
        "status": target,
        "decided_by": actor_user_id,
        "decision_note": redact_text(note) if note else None,
        "updated_at": db_dt(utcnow()),
    }
    if assigned_to is not None:
        values["assigned_to"] = assigned_to
    if target == "snoozed":
        values["snoozed_until"] = db_dt(snoozed_until)
    else:
        values["snoozed_until"] = None
    if target in TERMINAL:
        values["active_fingerprint"] = None

    conditions = [
        OperationalDecision.decision_uid == decision_uid,
        OperationalDecision.status.in_(_TRANSITIONS[target]),
    ]
    if customer_id is not None:
        conditions.append(OperationalDecision.customer_id == customer_id)

    async with Session() as session:
        result = await session.execute(
            update(OperationalDecision).where(*conditions).values(**values)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 1:
            await session.commit()
            return True
        await session.rollback()
        return False


async def mark_decision_applied_from_audit(
    decision_uid: str,
    *,
    proposal_confirmation_id: str,
    actor_user_id: int,
    note: str | None = None,
) -> bool:
    """approved → applied only with a matching, currently-applied proposal and audit row.

    This is one correlated UPDATE: an arbitrary confirmation id, another account/operation, or a
    proposal later moved to needs_review cannot manufacture an ``applied`` decision.
    """
    proof = exists(
        select(AuditLog.id)
        .select_from(AuditLog)
        .join(Proposal, Proposal.confirmation_id == AuditLog.confirmation_id)
        .where(
            AuditLog.confirmation_id == proposal_confirmation_id,
            AuditLog.status == "applied",
            AuditLog.customer_id == OperationalDecision.customer_id,
            AuditLog.operation == Proposal.operation,
            Proposal.confirmation_id == proposal_confirmation_id,
            Proposal.customer_id == OperationalDecision.customer_id,
            Proposal.status == "applied",
            or_(
                OperationalDecision.recommended_operation.is_(None),
                Proposal.operation == OperationalDecision.recommended_operation,
            ),
        )
    )
    async with Session() as session:
        result = await session.execute(
            update(OperationalDecision)
            .where(
                OperationalDecision.decision_uid == decision_uid,
                OperationalDecision.status == "approved",
                proof,
            )
            .values(
                status="applied",
                active_fingerprint=None,
                proposal_confirmation_id=proposal_confirmation_id,
                decided_by=actor_user_id,
                decision_note=redact_text(note) if note else None,
                snoozed_until=None,
                updated_at=db_dt(utcnow()),
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) == 1:
            await session.commit()
            return True
        await session.rollback()
        return False


async def expire_and_wake_decisions(*, now: datetime | None = None) -> dict[str, int]:
    """Scheduler-safe CAS updates for TTL expiry and completed snoozes."""
    point = db_dt(now or utcnow())
    async with Session() as session:
        expired = await session.execute(
            update(OperationalDecision)
            .where(
                OperationalDecision.status.in_(ACTIVE),
                OperationalDecision.expires_at.isnot(None),
                OperationalDecision.expires_at <= point,
            )
            .values(status="expired", active_fingerprint=None, updated_at=point)
        )
        woken = await session.execute(
            update(OperationalDecision)
            .where(
                OperationalDecision.status == "snoozed",
                OperationalDecision.snoozed_until.isnot(None),
                OperationalDecision.snoozed_until <= point,
            )
            .values(status="new", snoozed_until=None, updated_at=point)
        )
        await session.commit()
        return {
            "expired": int(getattr(expired, "rowcount", 0) or 0),
            "woken": int(getattr(woken, "rowcount", 0) or 0),
        }
