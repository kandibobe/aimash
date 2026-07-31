"""Private-operator confirmation policy.

Hermes may execute only operations explicitly listed in ``AUTONOMOUS_ADS_OPS`` without a human
confirmation card. Unknown operations fail closed into the confirmation-required branch.
"""

from __future__ import annotations


# Direct money controls, delivery state, traffic expansion, creative/targeting changes on possibly
# active entities and destructive removals. These keep the one-tap trusted Telegram confirmation
# flow until code can prove from a fresh snapshot that the target is non-serving.
CONFIRM_REQUIRED_ADS_OPS: frozenset[str] = frozenset(
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
    }
)


# Proven non-spend actions. Campaign creation is safe here because every creator persists the
# campaign, ad groups and ads as PAUSED in deterministic SDK code; enabling them is the separate
# ``launch_campaign`` operation above. A rename changes identity only, not delivery.
AUTONOMOUS_ADS_OPS: frozenset[str] = frozenset(
    {
        "update_campaign",
        "create_gdn_campaign",
        "create_search_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
    }
)

# Account-scoped local memory is not a Google Ads spend control. Save/update is reversible and may
# run immediately for a trusted private operator; destructive clear retains one explicit confirm.
AUTONOMOUS_MEMORY_OPS: frozenset[str] = frozenset({"profile_save", "profile_update"})


def requires_confirmation(operation: str) -> bool:
    """Unknown operations require confirmation; only the explicit autonomous set bypasses it."""

    return operation not in AUTONOMOUS_ADS_OPS | AUTONOMOUS_MEMORY_OPS


def may_execute_autonomously(operation: str) -> bool:
    return operation in AUTONOMOUS_ADS_OPS | AUTONOMOUS_MEMORY_OPS
