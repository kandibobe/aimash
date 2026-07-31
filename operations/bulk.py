"""Dry-run planning for bounded bulk operations; no direct mutation path exists here."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from operations.policy import PolicyLimits, PolicyVerdict, evaluate_policy


@dataclass(frozen=True)
class BulkPlan:
    operation: str
    target_ids: tuple[str, ...]
    params: dict[str, Any]
    scope_digest: str
    preview: tuple[dict[str, Any], ...]
    verdict: PolicyVerdict


def build_bulk_plan(
    *,
    operation: str,
    targets: list[dict[str, Any]],
    params: dict[str, Any],
    limits: PolicyLimits,
) -> BulkPlan:
    """Validate complete scope and emit a dry-run artifact suitable for one proposal."""
    if not targets:
        raise ValueError("bulk operation requires at least one target")
    ids: list[str] = []
    preview: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in targets:
        target_id = str(item.get("id", "")).strip()
        before = item.get("before")
        after = item.get("after")
        if not target_id or before is None or after is None:
            raise ValueError("each bulk target requires id, before and after")
        if target_id in seen:
            raise ValueError("duplicate target id in bulk scope")
        seen.add(target_id)
        ids.append(target_id)
        preview.append({"id": target_id, "before": before, "after": after})
    ordered = tuple(sorted(ids))
    digest = hashlib.sha256(
        json.dumps({"operation": operation, "ids": ordered}, separators=(",", ":")).encode()
    ).hexdigest()
    verdict = evaluate_policy(operation, params, target_count=len(ordered), limits=limits)
    return BulkPlan(
        operation=operation,
        target_ids=ordered,
        params=params,
        scope_digest=digest,
        preview=tuple(preview),
        verdict=verdict,
    )


def proposal_payload(plan: BulkPlan) -> dict[str, Any]:
    """Return proposal data only after policy passes; execution still uses the normal confirm path."""
    if not plan.verdict.allowed:
        raise PermissionError(f"bulk policy rejected: {','.join(plan.verdict.violations)}")
    return {
        **plan.params,
        "target_ids": list(plan.target_ids),
        "scope_digest": plan.scope_digest,
        "dry_run_preview": list(plan.preview),
    }
