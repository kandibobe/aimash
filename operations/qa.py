"""Creative, policy and landing-page QA using existing code validators and SSRF-safe fetching."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from adcopy.validate import find_duplicates, keyword_coverage, validate
from clients.crawl_fetch import fetch_url_html
from operations.types import DecisionInput


@dataclass(frozen=True)
class CreativeSpec:
    customer_id: str
    campaign_id: str
    ad_id: str
    headlines: list[str]
    descriptions: list[str]
    paths: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    policy_status: str = "APPROVED"
    ad_strength: str = ""
    final_url: str = ""
    asset_types: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class LandingProbe:
    url: str
    ok: bool
    latency_ms: int
    expected_text_present: bool | None
    form_present: bool | None
    form_ok: bool | None
    utm_present: bool
    error_type: str | None = None


def audit_creative(spec: CreativeSpec) -> list[DecisionInput]:
    issues: list[str] = []
    for kind, values in (
        ("headline", spec.headlines),
        ("description", spec.descriptions),
        ("path", spec.paths),
    ):
        for index, text in enumerate(values, 1):
            ok, length = validate(text, kind)
            if not ok:
                issues.append(f"{kind} {index} length {length} exceeds the code limit")
    duplicate_count = len(find_duplicates(spec.headlines)) + len(find_duplicates(spec.descriptions))
    if duplicate_count:
        issues.append(f"{duplicate_count} duplicate creative assets")
    coverage = keyword_coverage(spec.headlines, spec.keywords)
    if coverage < 0.5:
        issues.append(f"headline keyword coverage is {coverage:.0%}")
    if spec.policy_status not in {"APPROVED", "APPROVED_LIMITED"}:
        issues.append(f"policy status is {spec.policy_status}")
    if spec.ad_strength in {"POOR", "PENDING"}:
        issues.append(f"ad strength is {spec.ad_strength}")
    missing = {"SITELINK", "CALLOUT"} - {a.upper() for a in spec.asset_types}
    if missing:
        issues.append(f"missing asset coverage: {', '.join(sorted(missing))}")
    if not issues:
        return []
    return [
        DecisionInput(
            customer_id=spec.customer_id,
            source="creative_qa",
            source_ref=spec.ad_id,
            category="creative_qa",
            severity=("critical" if spec.policy_status == "DISAPPROVED" else "warning"),
            title=f"Creative QA issues in ad {spec.ad_id}",
            rationale="; ".join(issues),
            recommended_action="Fix the rejected or low-quality assets, then re-run deterministic QA.",
            confidence=1.0,
            evidence={
                "campaign_id": spec.campaign_id,
                "ad_id": spec.ad_id,
                "issues": issues,
                "keyword_coverage": coverage,
            },
            fingerprint_fields={"ad_id": spec.ad_id},
        )
    ]


async def probe_landing_page(
    url: str,
    *,
    expected_text: str | None = None,
    form_ok: bool | None = None,
) -> LandingProbe:
    """GET a page through the existing pinned-IP SSRF guard.

    ``form_ok`` is accepted only from an external synthetic probe. This function never submits a
    real lead form because that is an outward-facing mutation, not a safe health check.
    """
    started = time.monotonic()
    try:
        html = await fetch_url_html(url)
    except Exception as exc:  # caller receives only the exception class, never raw network text
        return LandingProbe(
            url=url,
            ok=False,
            latency_ms=round((time.monotonic() - started) * 1000),
            expected_text_present=None,
            form_present=None,
            form_ok=form_ok,
            utm_present=any(k.startswith("utm_") for k in parse_qs(urlparse(url).query)),
            error_type=type(exc).__name__,
        )
    low = html.casefold()
    return LandingProbe(
        url=url,
        ok=True,
        latency_ms=round((time.monotonic() - started) * 1000),
        expected_text_present=(expected_text.casefold() in low if expected_text else None),
        form_present=("<form" in low),
        form_ok=form_ok,
        utm_present=any(k.startswith("utm_") for k in parse_qs(urlparse(url).query)),
    )


def audit_landing_probe(
    customer_id: str, campaign_id: str, probe: LandingProbe
) -> list[DecisionInput]:
    issues: list[str] = []
    if not probe.ok:
        issues.append(f"landing fetch failed ({probe.error_type or 'unknown'})")
    if probe.latency_ms > 3000:
        issues.append(f"response took {probe.latency_ms} ms")
    if probe.expected_text_present is False:
        issues.append("the expected offer text is absent")
    if probe.form_present is False:
        issues.append("no HTML form was detected")
    if probe.form_ok is False:
        issues.append("the external synthetic form probe failed")
    if not probe.utm_present:
        issues.append("UTM parameters are missing")
    if not issues:
        return []
    return [
        DecisionInput(
            customer_id=customer_id,
            source="landing_qa",
            source_ref=campaign_id,
            category="landing_page",
            severity=("critical" if not probe.ok or probe.form_ok is False else "warning"),
            title=f"Landing-page issue for campaign {campaign_id}",
            rationale="; ".join(issues),
            recommended_action="Repair the destination or tracking before increasing traffic.",
            confidence=1.0,
            evidence={
                "url": probe.url,
                "latency_ms": probe.latency_ms,
                "issues": issues,
            },
            fingerprint_fields={"campaign_id": campaign_id, "url": probe.url},
        )
    ]
