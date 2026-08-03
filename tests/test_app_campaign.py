from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials
from pydantic import ValidationError

import ads.client as ads_client_module
import ads.mutations as mutations
from ads.client import DRAFT_ACCOUNT_ID
from llm.schemas import CreateAppCampaign, MUTATION_TOOLS, SCHEMAS
from conftest import FakeConfirmStore, FakeProposal
from mcp_server.tools_write import ACTION_TOOL_FUNCS


@contextmanager
def allowed_ids(value: str):
    old = ads_client_module.settings.google_ads_allowed_customer_ids
    ads_client_module.settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        ads_client_module.settings.google_ads_allowed_customer_ids = old


def _schema_params() -> dict:
    return {
        "campaign_name": "App Draft",
        "app_id": "com.example.app",
        "app_store": "google_play",
        "headlines": ["Установите приложение", "Попробуйте сегодня"],
        "descriptions": ["Быстро и удобно.", "Все функции в приложении."],
        "budget_daily_micros": 10_000_000,
        "target_cpa_micros": 1_000_000,
        "youtube_video_ids": ["dQw4w9WgXcQ"],
    }


def test_app_campaign_schema_and_surfaces():
    model = CreateAppCampaign(**_schema_params())
    assert model.app_store == "google_play"
    assert SCHEMAS["create_app_campaign"] is CreateAppCampaign
    assert "create_app_campaign" in MUTATION_TOOLS
    assert "create_app_campaign" in ACTION_TOOL_FUNCS


def test_app_campaign_schema_requires_trusted_media_or_youtube():
    params = _schema_params()
    params["youtube_video_ids"] = []
    with pytest.raises(ValidationError, match="изображение или YouTube"):
        CreateAppCampaign(**params)


async def test_apply_app_campaign_uses_confirm_and_finalizes(monkeypatch):
    captured = {}

    def fake_sdk(client, customer_id, **kwargs):
        captured.update(customer_id=customer_id, **kwargs)
        return {"campaign_name": kwargs["campaign_name"], "status": "PAUSED", "applied": True}

    monkeypatch.setattr(mutations, "_create_app_campaign_via_sdk", fake_sdk)
    store = FakeConfirmStore(
        FakeProposal("create_app_campaign", user_initiated=True, origin_human_turn=True)
    )
    with allowed_ids(DRAFT_ACCOUNT_ID):
        result = await mutations.apply_create_app_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            confirmation_id="app-ok",
            confirm_store=store,
            ads_client=object(),
            image_assets=[],
            **_schema_params(),
        )

    assert result["applied"] is True and result["status"] == "PAUSED"
    assert captured["target_cpa_micros"] == 1_000_000
    assert captured["youtube_video_ids"] == ["dQw4w9WgXcQ"]
    assert store.finalized is True


class _Service:
    def __init__(self, name: str, captured: dict):
        self.name = name
        self.captured = captured
        self.n = 0

    def _mutate(self, operations):
        self.n += 1
        self.captured.setdefault(self.name, []).extend(operations)
        suffix = {
            "CampaignBudgetService": "campaignBudgets/1",
            "CampaignService": "campaigns/2",
            "AdGroupService": "adGroups/3",
            "AdGroupAdService": "adGroupAds/3~4",
            "AssetService": f"assets/{100 + self.n}",
        }[self.name]
        return SimpleNamespace(results=[SimpleNamespace(resource_name=f"customers/1/{suffix}")])

    def mutate_campaign_budgets(self, *, customer_id, operations):
        return self._mutate(operations)

    def mutate_campaigns(self, *, customer_id, operations):
        return self._mutate(operations)

    def mutate_ad_groups(self, *, customer_id, operations):
        return self._mutate(operations)

    def mutate_ad_group_ads(self, *, customer_id, operations):
        return self._mutate(operations)

    def mutate_assets(self, *, customer_id, operations):
        return self._mutate(operations)


class _LocalV25Client:
    def __init__(self):
        self.real = GoogleAdsClient(
            AnonymousCredentials(), "test", version="v25", use_proto_plus=True
        )
        self.enums = self.real.enums
        self.captured: dict = {}
        self.services = {
            name: _Service(name, self.captured)
            for name in (
                "CampaignBudgetService",
                "CampaignService",
                "AdGroupService",
                "AdGroupAdService",
                "AssetService",
            )
        }

    def get_type(self, name: str):
        return self.real.get_type(name)

    def get_service(self, name: str):
        return self.services[name]


def test_app_campaign_sdk_builds_official_v25_shape_paused():
    client = _LocalV25Client()
    with allowed_ids(DRAFT_ACCOUNT_ID):
        result = mutations._create_app_campaign_via_sdk(
            client,
            DRAFT_ACCOUNT_ID,
            campaign_name="App Draft",
            app_id="com.example.app",
            app_store="google_play",
            headlines=["Install the app", "Try it today"],
            descriptions=["Fast and simple.", "Everything in one app."],
            budget_micros=10_000_000,
            target_cpa_micros=1_000_000,
            image_assets=[(b"jpeg", "app-landscape")],
            youtube_video_ids=["dQw4w9WgXcQ"],
        )

    campaign = client.captured["CampaignService"][0].create
    assert campaign.status == client.enums.CampaignStatusEnum.PAUSED
    assert (
        campaign.advertising_channel_type == client.enums.AdvertisingChannelTypeEnum.MULTI_CHANNEL
    )
    assert (
        campaign.advertising_channel_sub_type
        == client.enums.AdvertisingChannelSubTypeEnum.APP_CAMPAIGN
    )
    assert campaign.target_cpa.target_cpa_micros == 1_000_000
    assert campaign.app_campaign_setting.app_id == "com.example.app"
    assert (
        campaign.app_campaign_setting.app_store
        == client.enums.AppCampaignAppStoreEnum.GOOGLE_APP_STORE
    )
    assert (
        client.captured["AdGroupService"][0].create.status == client.enums.AdGroupStatusEnum.PAUSED
    )
    app_ad = client.captured["AdGroupAdService"][0].create
    assert app_ad.status == client.enums.AdGroupAdStatusEnum.PAUSED
    assert len(app_ad.ad.app_ad.headlines) == 2
    assert len(app_ad.ad.app_ad.descriptions) == 2
    assert len(app_ad.ad.app_ad.images) == 1
    assert len(app_ad.ad.app_ad.youtube_videos) == 1
    assert result["status"] == "PAUSED" and result["images"] == 1 and result["videos"] == 1
