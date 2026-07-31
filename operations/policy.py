"""Deterministic policy checks for proposals and bulk blast radius.

The engine is pure and must run before a proposal is created. It does not replace account locks,
provenance, confirmation, four-eyes, 2FA or mutation-side validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from confirm.risk import DESTRUCTIVE_OPS, MONEY_OPS, max_delta_pct, risk_tier


@dataclass(frozen=True)
class PolicyLimits:
    max_targets: int = 100
    max_money_increase_pct: float = 20.0
    destructive_allowed: bool = False
    allowed_operations: frozenset[str] = field(default_factory=frozenset)
    required_four_eyes_tiers: frozenset[str] = field(default_factory=lambda: frozenset({"L3"}))

    def __post_init__(self) -> None:
        if not 1 <= self.max_targets <= 10_000:
            raise ValueError("max_targets must be in 1..10000")
        if not 0 < self.max_money_increase_pct <= 100:
            raise ValueError("max_money_increase_pct must be in (0, 100]")


@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    risk_tier: str
    violations: tuple[str, ...]
    requires_four_eyes: bool


def evaluate_policy(
    operation: str,
    params: dict[str, Any],
    *,
    target_count: int,
    limits: PolicyLimits,
) -> PolicyVerdict:
    violations: list[str] = []
    tier = risk_tier(operation, params)
    if limits.allowed_operations and operation not in limits.allowed_operations:
        violations.append("operation_not_allowed")
    if operation in DESTRUCTIVE_OPS and not limits.destructive_allowed:
        violations.append("destructive_operation_disabled")
    if target_count < 1:
        violations.append("empty_scope")
    elif target_count > limits.max_targets:
        violations.append("blast_radius_exceeded")
    if operation in MONEY_OPS:
        before = params.get("_before")
        if not isinstance(before, dict):
            violations.append("money_before_snapshot_missing")
        else:
            delta = max_delta_pct(before.get("before_micros"), before.get("after_micros"))
            if delta is None:
                violations.append("money_delta_unknown")
            elif delta > limits.max_money_increase_pct:
                violations.append("money_delta_exceeded")
    return PolicyVerdict(
        allowed=not violations,
        risk_tier=tier,
        violations=tuple(violations),
        requires_four_eyes=tier in limits.required_four_eyes_tiers,
    )
