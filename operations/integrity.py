"""Deterministic conversion/tracking integrity checks.

The existing account audit remains the source for Google Ads conversion-action facts. This module
adds time-series drift and optional Ads-versus-analytics/CRM reconciliation without guessing when a
source is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

from operations.types import DecisionInput


@dataclass(frozen=True)
class ConversionActionState:
    name: str
    status: str
    primary_for_goal: bool
    category: str = ""
    attribution_model: str = ""


@dataclass(frozen=True)
class PerformanceWindow:
    spend_micros: int
    clicks: int
    conversions: float
    conversion_value_micros: int = 0
    sessions: int | None = None
    crm_leads: int | None = None
    crm_qualified: int | None = None
    crm_orders: int | None = None
    crm_revenue_micros: int | None = None


def evaluate_conversion_integrity(
    customer_id: str,
    *,
    actions: list[ConversionActionState] | None,
    current: PerformanceWindow,
    baseline: PerformanceWindow | None = None,
    min_spend_micros: int = 10_000_000,
    drop_ratio: float = 0.5,
    click_session_tolerance: float = 0.35,
) -> list[DecisionInput]:
    """Return only evidence-backed findings; ``actions=None`` is a data gap, not "no tracking"."""
    findings: list[DecisionInput] = []
    active_primary = (
        [a for a in actions if a.status == "ENABLED" and a.primary_for_goal]
        if actions is not None
        else None
    )
    if active_primary == [] and current.spend_micros >= min_spend_micros:
        findings.append(
            DecisionInput(
                customer_id=customer_id,
                source="tracking_integrity",
                category="conversion_integrity",
                severity="critical",
                title="No enabled primary conversion action",
                rationale="The account spent money but no enabled primary conversion action was read.",
                recommended_action="Repair or deliberately select the primary conversion goals before optimizing traffic.",
                confidence=1.0,
                evidence={"spend_micros": current.spend_micros, "active_primary": 0},
                fingerprint_fields={"check": "no_primary"},
            )
        )
    elif active_primary and current.conversions <= 0 and current.spend_micros >= min_spend_micros:
        findings.append(
            DecisionInput(
                customer_id=customer_id,
                source="tracking_integrity",
                category="conversion_integrity",
                severity="critical",
                title="Spend continues while reported conversions are zero",
                rationale="Primary conversion actions are enabled, but the current window has zero conversions.",
                recommended_action="Validate tag firing and the conversion lag before changing bids or budgets.",
                confidence=0.95,
                evidence={
                    "spend_micros": current.spend_micros,
                    "active_primary": len(active_primary),
                },
                fingerprint_fields={"check": "zero_conversions"},
            )
        )

    if baseline and baseline.conversions > 0:
        ratio = current.conversions / baseline.conversions
        if ratio <= drop_ratio and current.spend_micros >= min_spend_micros:
            findings.append(
                DecisionInput(
                    customer_id=customer_id,
                    source="tracking_integrity",
                    category="conversion_integrity",
                    severity="warning",
                    title="Conversion volume dropped sharply",
                    rationale=(
                        f"Conversions are {ratio:.1%} of the comparable baseline while spend remains material."
                    ),
                    recommended_action="Check conversion lag, tag changes and CRM intake before diagnosing the auction.",
                    confidence=0.9,
                    evidence={
                        "current_conversions": current.conversions,
                        "baseline_conversions": baseline.conversions,
                        "ratio": ratio,
                    },
                    fingerprint_fields={"check": "conversion_drop"},
                )
            )

    if current.sessions is not None and current.clicks >= 20:
        gap = abs(current.clicks - current.sessions) / max(current.clicks, 1)
        if gap > click_session_tolerance:
            findings.append(
                DecisionInput(
                    customer_id=customer_id,
                    source="tracking_integrity",
                    category="funnel_integrity",
                    severity="warning",
                    title="Ads clicks and analytics sessions diverge",
                    rationale=f"The clicks-to-sessions gap is {gap:.1%}, above {click_session_tolerance:.0%}.",
                    recommended_action="Check consent, redirects, page load failures and analytics tagging.",
                    confidence=0.85,
                    evidence={"clicks": current.clicks, "sessions": current.sessions, "gap": gap},
                    fingerprint_fields={"check": "click_session_gap"},
                )
            )

    if current.crm_leads is not None and current.conversions >= 10:
        lead_ratio = current.crm_leads / max(current.conversions, 1)
        if lead_ratio < 0.5:
            findings.append(
                DecisionInput(
                    customer_id=customer_id,
                    source="tracking_integrity",
                    category="revenue_integrity",
                    severity="warning",
                    title="Ads conversions do not reconcile with CRM leads",
                    rationale=f"CRM received {lead_ratio:.1%} as many leads as Ads reported conversions.",
                    recommended_action="Audit duplicate conversions, offline imports and CRM ingestion before optimization.",
                    confidence=0.9,
                    evidence={
                        "ads_conversions": current.conversions,
                        "crm_leads": current.crm_leads,
                        "ratio": lead_ratio,
                    },
                    fingerprint_fields={"check": "ads_crm_gap"},
                )
            )
    return findings
