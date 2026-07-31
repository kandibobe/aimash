"""Versioned deterministic playbooks that can only surface decisions/incidents."""

from __future__ import annotations

import operator
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update

from db.models import PlaybookVersion
from db.session import Session, db_dt
from operations.decisions import create_or_refresh_decision
from operations.incidents import record_incident
from operations.types import DecisionInput, IncidentInput

ALLOWED_FIELDS = frozenset(
    {
        "spend_micros",
        "conversions",
        "revenue_micros",
        "cpa_micros",
        "roas",
        "lost_is_budget",
        "wasted_spend_micros",
        "campaign_enabled",
        "conversion_drop_ratio",
    }
)
OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}
ALLOWED_ACTIONS = frozenset({"decision", "incident"})


def validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Validate a small declarative language; mutation/proposal actions are impossible."""
    if set(rule) != {"all", "action"}:
        raise ValueError("rule must contain exactly 'all' and 'action'")
    conditions = rule["all"]
    action = rule["action"]
    if not isinstance(conditions, list) or not conditions:
        raise ValueError("rule.all must be a non-empty list")
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != {"field", "op", "value"}:
            raise ValueError("each condition requires field, op and value")
        if condition["field"] not in ALLOWED_FIELDS or condition["op"] not in OPS:
            raise ValueError("condition field or operator is not allowed")
        if not isinstance(condition["value"], (str, int, float, bool)):
            raise ValueError("condition value must be scalar")
    if not isinstance(action, dict) or action.get("type") not in ALLOWED_ACTIONS:
        raise ValueError("playbook actions are restricted to decision or incident")
    forbidden = {"operation", "params", "confirmation_id", "execute", "mutation"} & set(action)
    if forbidden:
        raise ValueError(f"playbook action contains forbidden keys: {sorted(forbidden)}")
    required = {"type", "category", "severity", "title"}
    if not required <= set(action):
        raise ValueError(f"playbook action missing keys: {sorted(required - set(action))}")
    if action["severity"] not in {"info", "warning", "critical"}:
        raise ValueError("invalid severity")
    return rule


def evaluate_rule(rule: dict[str, Any], facts: dict[str, Any]) -> bool:
    validated = validate_rule(rule)
    for condition in validated["all"]:
        field = condition["field"]
        if field not in facts:
            return False
        try:
            if not OPS[condition["op"]](facts[field], condition["value"]):
                return False
        except TypeError:
            return False
    return True


async def save_playbook(
    *,
    name: str,
    description: str,
    rule: dict[str, Any],
    created_by: int | None,
    enable: bool = False,
) -> PlaybookVersion:
    validate_rule(rule)
    if not name.strip() or len(name) > 96:
        raise ValueError("playbook name must contain 1..96 characters")
    now = db_dt(datetime.now(timezone.utc))
    async with Session() as session:
        version = (
            int(
                (
                    await session.execute(
                        select(func.max(PlaybookVersion.version)).where(
                            PlaybookVersion.name == name.strip()
                        )
                    )
                ).scalar_one()
                or 0
            )
            + 1
        )
        if enable:
            await session.execute(
                update(PlaybookVersion)
                .where(PlaybookVersion.name == name.strip(), PlaybookVersion.enabled.is_(True))
                .values(enabled=False)
            )
        row = PlaybookVersion(
            playbook_uid=f"rule_{secrets.token_hex(12)}",
            name=name.strip(),
            version=version,
            description=description.strip(),
            rule=rule,
            enabled=enable,
            created_by=created_by,
            created_at=now,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def run_playbooks(
    *,
    customer_id: str,
    facts: dict[str, Any],
    source_ref: str | None = None,
    chat_id: int | None = None,
) -> list[str]:
    async with Session() as session:
        rules = list(
            (
                await session.execute(
                    select(PlaybookVersion).where(PlaybookVersion.enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )
    created: list[str] = []
    for row in rules:
        if not evaluate_rule(row.rule, facts):
            continue
        action = row.rule["action"]
        evidence = {key: facts[key] for key in ALLOWED_FIELDS if key in facts}
        fingerprint = {"playbook": row.name, "version": row.version, "source_ref": source_ref}
        if action["type"] == "decision":
            decision = await create_or_refresh_decision(
                DecisionInput(
                    customer_id=customer_id,
                    source="playbook",
                    source_ref=source_ref,
                    category=action["category"],
                    severity=action["severity"],
                    title=action["title"],
                    rationale=action.get("rationale", row.description),
                    recommended_action=action.get("recommended_action", "Review the evidence."),
                    confidence=1.0,
                    evidence=evidence,
                    chat_id=chat_id,
                    fingerprint_fields=fingerprint,
                )
            )
            created.append(decision.decision_uid)
        else:
            incident = await record_incident(
                IncidentInput(
                    customer_id=customer_id,
                    kind=action["category"],
                    severity=action["severity"],
                    title=action["title"],
                    evidence=evidence,
                    fingerprint_fields=fingerprint,
                )
            )
            created.append(incident.incident_uid)
    return created
