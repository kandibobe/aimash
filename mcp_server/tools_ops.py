"""Thin trusted MCP adapters for the operational decision and incident control plane."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Awaitable, Callable, Literal

from core.access import is_admin
from core.config import normalize_customer_id
from mcp_server.envelope import ok
from mcp_server.trusted_transport import get_trusted_turn
from operations.decisions import list_decisions as _list_decisions
from operations.decisions import transition_decision
from operations.governance import has_capability
from operations.incidents import list_incidents as _list_incidents
from operations.incidents import transition_incident, utcnow


async def _require_capability(account: str, capability: str) -> int:
    actor = get_trusted_turn().actor_user_id
    if not await is_admin(actor) and not await has_capability(actor, account, capability):
        raise PermissionError(f"операция требует RBAC capability: {capability}")
    return actor


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _decision_row(row) -> dict[str, Any]:
    return {
        "decision_uid": row.decision_uid,
        "customer_id": row.customer_id,
        "category": row.category,
        "severity": row.severity,
        "title": row.title,
        "rationale": row.rationale,
        "recommended_action": row.recommended_action,
        "recommended_operation": row.recommended_operation,
        "confidence": row.confidence,
        "evidence": row.evidence or {},
        "status": row.status,
        "assigned_to": row.assigned_to,
        "occurrence_count": row.occurrence_count,
        "snoozed_until": _iso(row.snoozed_until),
        "expires_at": _iso(row.expires_at),
        "last_seen_at": _iso(row.last_seen_at),
    }


def _incident_row(row) -> dict[str, Any]:
    return {
        "incident_uid": row.incident_uid,
        "customer_id": row.customer_id,
        "decision_uid": row.decision_uid,
        "kind": row.kind,
        "severity": row.severity,
        "title": row.title,
        "evidence": row.evidence or {},
        "status": row.status,
        "assigned_to": row.assigned_to,
        "occurrence_count": row.occurrence_count,
        "escalation_level": row.escalation_level,
        "snoozed_until": _iso(row.snoozed_until),
        "last_seen_at": _iso(row.last_seen_at),
    }


async def list_decisions(
    account: str,
    statuses: list[str] | None = None,
    assigned_to: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """List the account decision queue; requires a trusted operator with read capability."""
    customer_id = normalize_customer_id(account)
    await _require_capability(customer_id, "read")
    rows = await _list_decisions(
        customer_id,
        statuses=set(statuses) if statuses else None,
        assigned_to=assigned_to,
        limit=500,
    )
    return ok([_decision_row(row) for row in rows], offset=offset, limit=limit, reader_limit=500)


async def update_decision(
    account: str,
    decision_uid: str,
    action: Literal["acknowledged", "approved", "rejected", "snoozed", "new"],
    note: str | None = None,
    snooze_minutes: int | None = None,
    assigned_to: int | None = None,
) -> dict[str, Any]:
    """Atomically ACK/approve/reject/snooze/reopen one scoped advisory decision."""
    customer_id = normalize_customer_id(account)
    capability = {
        "acknowledged": "ack",
        "approved": "approve",
        "rejected": "reject",
        "snoozed": "snooze",
        "new": "ack",
    }[action]
    actor = await _require_capability(customer_id, capability)
    snoozed_until = None
    if action == "snoozed":
        minutes = int(snooze_minutes or 0)
        if not 1 <= minutes <= 43_200:
            raise ValueError("snooze_minutes must be between 1 and 43200")
        snoozed_until = utcnow() + timedelta(minutes=minutes)
    elif snooze_minutes is not None:
        raise ValueError("snooze_minutes is valid only for action=snoozed")
    changed = await transition_decision(
        decision_uid,
        action,
        actor_user_id=actor,
        customer_id=customer_id,
        note=note,
        snoozed_until=snoozed_until,
        assigned_to=assigned_to,
    )
    return {
        "decision_uid": decision_uid,
        "customer_id": customer_id,
        "status": action if changed else "conflict_or_not_found",
        "updated": changed,
        "error": None,
        "error_code": None,
    }


async def list_incidents(
    account: str,
    statuses: list[str] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """List deduplicated incidents for the account; requires trusted read capability."""
    customer_id = normalize_customer_id(account)
    await _require_capability(customer_id, "read")
    rows = await _list_incidents(
        customer_id,
        statuses=set(statuses) if statuses else None,
        limit=500,
    )
    return ok([_incident_row(row) for row in rows], offset=offset, limit=limit, reader_limit=500)


async def update_incident(
    account: str,
    incident_uid: str,
    action: Literal["acknowledged", "snoozed", "resolved", "open"],
    snooze_minutes: int | None = None,
    assigned_to: int | None = None,
) -> dict[str, Any]:
    """Atomically ACK/snooze/resolve/reopen one account-scoped incident."""
    customer_id = normalize_customer_id(account)
    capability = {
        "acknowledged": "ack",
        "snoozed": "snooze",
        "resolved": "resolve",
        "open": "resolve",
    }[action]
    actor = await _require_capability(customer_id, capability)
    snoozed_until = None
    if action == "snoozed":
        minutes = int(snooze_minutes or 0)
        if not 1 <= minutes <= 43_200:
            raise ValueError("snooze_minutes must be between 1 and 43200")
        snoozed_until = utcnow() + timedelta(minutes=minutes)
    elif snooze_minutes is not None:
        raise ValueError("snooze_minutes is valid only for action=snoozed")
    changed = await transition_incident(
        incident_uid,
        action,
        actor_user_id=actor,
        customer_id=customer_id,
        snoozed_until=snoozed_until,
        assigned_to=assigned_to,
    )
    return {
        "incident_uid": incident_uid,
        "customer_id": customer_id,
        "status": action if changed else "conflict_or_not_found",
        "updated": changed,
        "error": None,
        "error_code": None,
    }


OPS_STATE_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "list_decisions": list_decisions,
    "update_decision": update_decision,
    "list_incidents": list_incidents,
    "update_incident": update_incident,
}
OPS_STATE_MCP_TOOLS: frozenset[str] = frozenset(OPS_STATE_TOOL_FUNCS)
