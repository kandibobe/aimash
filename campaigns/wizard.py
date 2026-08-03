"""Bot-free assembly and validation for the eight-stage Search wizard."""

from __future__ import annotations

from typing import Any

from llm.schemas import MAX_CAMPAIGN_KEYWORDS, CreateSearchCampaign
from core.config import settings


def build_create_params(wizard_state: dict[str, Any]) -> dict[str, Any]:
    """Build the single composite ``create_search_campaign`` payload from durable state."""
    state = dict(wizard_state or {})
    campaign = dict(state.get("settings") or {})
    keywords = dict(state.get("keywords") or {})
    ad = dict(state.get("ad") or {})
    images = dict(state.get("images") or {})
    assets = dict(state.get("assets") or {})
    url_options = {k: v for k, v in dict(state.get("url_options") or {}).items() if v}

    keyword_values = list(keywords.get("list") or [])[:MAX_CAMPAIGN_KEYWORDS]
    match_types = list(keywords.get("match_types") or [])
    if match_types:
        match_types = match_types[: len(keyword_values)]

    bidding = dict(campaign.get("bidding") or {})
    if not bidding:
        bidding = {"strategy": campaign.get("bidding_strategy") or "manual_cpc"}
        if campaign.get("target_cpa_micros") is not None:
            bidding["target_cpa_micros"] = int(campaign["target_cpa_micros"])

    payload = {
        "campaign_name": campaign.get("campaign_name") or "Search",
        "final_url": ad.get("final_url") or "",
        "headlines": list(ad.get("headlines") or []),
        "descriptions": list(ad.get("descriptions") or []),
        "budget_daily_micros": int(campaign.get("budget_daily_micros") or 0),
        "keywords": keyword_values,
        "match_type": keywords.get("match_type") or "phrase",
        "keyword_match_types": match_types,
        "cpc_bid_micros": (
            int(campaign["cpc_bid_micros"]) if campaign.get("cpc_bid_micros") is not None else None
        ),
        "geo_locations": list(campaign.get("geo_locations") or []),
        "geo_country_code": campaign.get("geo_country_code") or settings.geo_default_country,
        "geo_locale": campaign.get("geo_locale") or settings.geo_default_locale,
        "languages": list(campaign.get("languages") or []),
        "bidding": bidding,
        "path1": ad.get("path1") or None,
        "path2": ad.get("path2") or None,
        "url_options": url_options or None,
        "asset_specs": list(assets.get("new") or []),
        "existing_asset_links": list(assets.get("reuse_links") or []),
        "image_media_ids": list(images.get("media_ids") or []),
        "networks": campaign.get("networks"),
        "ad_schedule_blocks": list(campaign.get("ad_schedule_blocks") or []),
        "start_date": campaign.get("start_date"),
        "end_date": campaign.get("end_date"),
    }
    return CreateSearchCampaign(**payload).model_dump(exclude_none=True)


def public_state(snapshot: Any) -> dict[str, Any]:
    """Serialize a wizard snapshot without ORM state or local file paths."""
    return {
        "session_id": snapshot.session_id,
        "account": snapshot.customer_id,
        "stage": int(snapshot.current_step),
        "status": snapshot.status,
        "wizard_state": snapshot.wizard_state,
    }
