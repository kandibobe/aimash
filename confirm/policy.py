"""Private-operator confirmation policy.

Every user-facing mutation requires one trusted human confirmation. Hermes remains autonomous for
reading, analysis, tool selection and proposal composition; execution never bypasses the card.
Unknown operations fail closed into the confirmation-required branch.
"""

from __future__ import annotations


# Complete Google Ads mutation surface. Every operation stops at the same one-tap trusted Telegram
# confirmation flow, including renames and creation of PAUSED campaigns.
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
        "update_campaign",
        "create_gdn_campaign",
        "create_search_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
    }
)


# Compatibility exports make the policy partition explicit. They must stay empty: execution of any
# mutation without a human decision would violate the product contract.
AUTONOMOUS_ADS_OPS: frozenset[str] = frozenset()
AUTONOMOUS_MEMORY_OPS: frozenset[str] = frozenset()


def requires_confirmation(operation: str) -> bool:
    """Every mutation, including an unknown future operation, requires confirmation."""

    return True


def may_execute_autonomously(operation: str) -> bool:
    """No mutation may execute autonomously."""

    return False
