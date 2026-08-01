"""Aimash v3 approval policy.

Operational Google Ads mutations execute from the current trusted human turn.  One approval is
reserved for a critical global budget change: an ``update_budget`` classified as L3 from its live
``before -> after`` snapshot.  Unknown operations still stop because they are outside the typed
tool registry.
"""

from __future__ import annotations


# Complete typed Google Ads mutation surface.
ALL_ADS_OPS: frozenset[str] = frozenset(
    {
        "add_call_asset",
        "add_callouts",
        "add_keywords",
        "add_negative_keywords",
        "add_negatives_to_shared_set",
        "add_price_asset",
        "add_promotion",
        "add_sitelinks",
        "add_structured_snippets",
        "attach_audience",
        "attach_image_asset",
        "attach_shared_set",
        "create_rsa",
        "detach_audience",
        "update_budget",
        "update_bid",
        "update_keyword_bid",
        "set_bidding_strategy",
        "pause_campaign",
        "resume_campaign",
        "launch_campaign",
        "pause_ad_group",
        "resume_ad_group",
        "pause_ad",
        "resume_ad",
        "set_campaign_network",
        "set_campaign_display_network",
        "set_campaign_geo_target_type",
        "set_geo_location",
        "set_geo_proximity",
        "remove_campaign",
        "remove_ad_group",
        "remove_ad",
        "remove_keywords",
        "remove_negative_keywords",
        "remove_asset_link",
        "update_campaign",
        "create_gdn_campaign",
        "create_search_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
        "create_app_campaign",
    }
)


# Approval candidates are deliberately narrow. ``update_budget`` is conditional: L1/L2 execute
# directly, while L3 returns APPROVAL_REQUIRED with the attested diff.
CONFIRM_REQUIRED_ADS_OPS: frozenset[str] = frozenset({"update_budget"})
AUTONOMOUS_ADS_OPS: frozenset[str] = ALL_ADS_OPS - CONFIRM_REQUIRED_ADS_OPS
AUTONOMOUS_MEMORY_OPS: frozenset[str] = frozenset()


def requires_confirmation(operation: str, params: dict | None = None) -> bool:
    """Return whether this exact typed action needs the single approval step."""

    if operation not in ALL_ADS_OPS:
        return True
    if operation != "update_budget":
        return False

    from confirm.risk import TIER_L3, risk_tier

    return risk_tier(operation, params) == TIER_L3


def may_execute_autonomously(operation: str, params: dict | None = None) -> bool:
    """Return whether the trusted Hermes turn may continue straight to execution."""

    return operation in ALL_ADS_OPS and not requires_confirmation(operation, params)
