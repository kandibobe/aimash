"""Fail-closed approval policy for every Google Ads and client-memory mutation.

The trusted human turn may prepare one exact proposal, but it is never the approval itself.  The
mutation executes only after a separate trusted reply/button claims that proposal through CAS.
Risk tiers enrich the preview and hard caps; they never waive confirmation.
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


# Every Ads mutation changes delivery, account state or spend.  Keep the sets explicit so a newly
# registered operation cannot silently become autonomous.
CONFIRM_REQUIRED_ADS_OPS: frozenset[str] = ALL_ADS_OPS
AUTONOMOUS_ADS_OPS: frozenset[str] = frozenset()
AUTONOMOUS_MEMORY_OPS: frozenset[str] = frozenset()


def requires_confirmation(operation: str, params: dict | None = None) -> bool:
    """Return whether this exact typed action needs the single approval step."""
    del params
    return True


def may_execute_autonomously(operation: str, params: dict | None = None) -> bool:
    """Return whether the trusted Hermes turn may continue straight to execution."""

    del operation, params
    return False
