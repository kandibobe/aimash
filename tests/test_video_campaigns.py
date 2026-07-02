"""Офлайн-тесты §11: кампании из видео (Demand Gen / Video) за двойным гейтом. Без живого SDK.

Зеркалят test_gdn_campaign.py: оба гейта (замок аккаунта + confirm + user_initiated), валидация
состава/длины/URL/бюджета/YouTube-id В КОДЕ ДО claim, статус PAUSED, разбор YouTube-ссылок, схемы.
⚠️ SDK-цепочки (_create_*_via_sdk) требуют live-сверки на тест-аккаунте перед сдачей.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
from ads.assets import parse_youtube_video_id  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402

_YT = "dQw4w9WgXcQ"


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@dataclass
class FakeProposal:
    operation: str
    status: str
    user_initiated: bool


class FakeStore:
    def __init__(self, proposal=None):
        self._p = proposal
        self.finalized = False
        self._claimed = False

    async def claim(self, confirmation_id, *, operation):
        p = self._p
        if p is None or p.status != "confirmed" or p.operation != operation or self._claimed:
            return None
        self._claimed = True
        return p

    async def finalize(self, confirmation_id, *, result):
        self.finalized = True


_VALID = dict(
    campaign_name="Кения авто видео",
    youtube_video_id=_YT,
    headlines=["Поддержанные авто в Кении", "Авто с гарантией"],
    long_headline="Проверенные б/у автомобили с гарантией 12 месяцев — доставка по Кении",
    descriptions=["Большой выбор седанов и внедорожников. Рассрочка и trade-in."],
    business_name="Kasi Motors",
    final_url="https://kasimotors.co.ke/",
    budget_daily_micros=40_000_000,
)


# ── Разбор YouTube-ссылок (чистая функция, КОД) ───────────────────────────────────
def test_parse_youtube_variants():
    assert parse_youtube_video_id(_YT) == _YT  # голый id
    assert parse_youtube_video_id(f"https://www.youtube.com/watch?v={_YT}") == _YT
    assert parse_youtube_video_id(f"https://youtu.be/{_YT}") == _YT
    assert parse_youtube_video_id(f"https://youtube.com/shorts/{_YT}?feature=share") == _YT
    assert parse_youtube_video_id(f"https://www.youtube.com/embed/{_YT}") == _YT
    assert parse_youtube_video_id(f"https://www.youtube.com/watch?list=PL123&v={_YT}&t=10") == _YT


def test_parse_youtube_rejects_garbage():
    for bad in ("", "not a link", "https://example.com/watch?v=abc", "https://youtube.com/", "id"):
        assert parse_youtube_video_id(bad) is None


# ── apply_create_demand_gen_campaign: оба гейта + user_initiated + PAUSED ─────────
async def test_apply_create_dg_happy_path():
    called = {}

    def fake(client, customer_id, **kw):
        called.update(customer_id=customer_id, **kw)
        return {"applied": True, "status": "PAUSED", "campaign": "customers/x/campaigns/1"}

    store = FakeStore(FakeProposal("create_demand_gen_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_demand_gen_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_create_demand_gen_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
            goal="clicks",
            geo_locations=["Кения"],
            **_VALID,
        )
    assert res["applied"] is True and res["status"] == "PAUSED"
    assert called["customer_id"] == DRAFT_ACCOUNT_ID
    assert called["youtube_video_id"] == _YT
    assert called["goal"] == "clicks"
    assert called["geo_locations"] == ["Кения"]
    assert store.finalized is True


async def test_apply_create_dg_blocked_when_not_user_initiated():
    store = FakeStore(FakeProposal("create_demand_gen_campaign", "confirmed", user_initiated=False))
    with (
        patched(mut, "_create_demand_gen_campaign_via_sdk", lambda *a, **k: {"applied": True}),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        with pytest.raises(PermissionError):
            await mut.apply_create_demand_gen_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="x",
                confirm_store=store,
                ads_client=object(),
                **_VALID,
            )
    assert store.finalized is False


async def test_apply_create_dg_rejects_foreign_account():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("create_demand_gen_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_demand_gen_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            await mut.apply_create_demand_gen_campaign(
                customer_id="1234567890",  # чужой → замок ДО SDK
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                **_VALID,
            )
    assert calls["n"] == 0 and store.finalized is False


async def test_apply_create_dg_validates_before_claim():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("create_demand_gen_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_demand_gen_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(ValueError):
            await mut.apply_create_demand_gen_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                **{**_VALID, "headlines": ["а" * 31]},  # >30 (кириллица=1)
            )
    assert calls["n"] == 0 and store.finalized is False


async def test_apply_create_dg_rejects_bad_goal_and_bad_youtube():
    store = FakeStore(FakeProposal("create_demand_gen_campaign", "confirmed", user_initiated=True))
    with (
        patched(mut, "_create_demand_gen_campaign_via_sdk", lambda *a, **k: {"applied": True}),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        with pytest.raises(ValueError):
            await mut.apply_create_demand_gen_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                goal="autopilot",  # не из списка
                **_VALID,
            )
        with pytest.raises(ValueError):
            await mut.apply_create_demand_gen_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                # ⚠️ не 11-символьная строка из [A-Za-z0-9_-] (та была бы валидным id!)
                **{**_VALID, "youtube_video_id": "просто мусорный текст"},
            )
    assert store.finalized is False


# ── apply_create_video_campaign: гейты + лимит описаний ≤70 ───────────────────────
async def test_apply_create_video_happy_path():
    called = {}

    def fake(client, customer_id, **kw):
        called.update(customer_id=customer_id, **kw)
        return {"applied": True, "status": "PAUSED"}

    store = FakeStore(FakeProposal("create_video_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_video_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_create_video_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
            **{**_VALID, "descriptions": ["Короткое описание до 70 символов."]},
        )
    assert res["applied"] is True and res["status"] == "PAUSED"
    assert called["youtube_video_id"] == _YT
    assert store.finalized is True


async def test_apply_create_video_rejects_description_over_70():
    store = FakeStore(FakeProposal("create_video_campaign", "confirmed", user_initiated=True))
    with (
        patched(mut, "_create_video_campaign_via_sdk", lambda *a, **k: {"applied": True}),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        with pytest.raises(ValueError):
            await mut.apply_create_video_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                **{**_VALID, "descriptions": ["о" * 71]},  # >70 (VIDEO_DESCRIPTION_MAX)
            )
    assert store.finalized is False


async def test_apply_create_video_blocked_when_not_user_initiated():
    store = FakeStore(FakeProposal("create_video_campaign", "confirmed", user_initiated=False))
    with (
        patched(mut, "_create_video_campaign_via_sdk", lambda *a, **k: {"applied": True}),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        with pytest.raises(PermissionError):
            await mut.apply_create_video_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="x",
                confirm_store=store,
                ads_client=object(),
                **{**_VALID, "descriptions": ["Короткое описание."]},
            )
    assert store.finalized is False


# ── capability-guard зеркало + схемы ─────────────────────────────────────────────
def test_video_ops_in_supported_operations():
    from ads.service import SUPPORTED_OPERATIONS

    assert "create_demand_gen_campaign" in SUPPORTED_OPERATIONS
    assert "create_video_campaign" in SUPPORTED_OPERATIONS


def test_video_ops_in_mutation_tools():
    from agent.tools.schemas import MUTATION_TOOLS

    assert "create_demand_gen_campaign" in MUTATION_TOOLS
    assert "create_video_campaign" in MUTATION_TOOLS


def test_dg_schema_validates_and_rejects():
    from agent.tools.schemas import CreateDemandGenCampaign

    ok = CreateDemandGenCampaign(**_VALID)
    assert ok.goal == "clicks" and ok.logo_media_id is None  # дефолты
    ok2 = CreateDemandGenCampaign(
        **{**_VALID, "youtube_video_id": f"https://youtu.be/{_YT}"},
        goal="conversions",
        logo_media_id="abc123",
    )
    assert ok2.goal == "conversions"
    with pytest.raises(Exception):
        CreateDemandGenCampaign(**{**_VALID, "youtube_video_id": "мусор"})
    with pytest.raises(Exception):
        CreateDemandGenCampaign(**_VALID, logo_media_id="../evil")  # traversal
    with pytest.raises(Exception):
        CreateDemandGenCampaign(**{**_VALID, "headlines": ["а" * 31]})  # >30


def test_video_schema_validates():
    from agent.tools.schemas import CreateVideoCampaign

    ok = CreateVideoCampaign(**_VALID, geo_locations=["Кения"])
    assert ok.geo_locations == ["Кения"]
    with pytest.raises(Exception):
        CreateVideoCampaign(**{**_VALID, "final_url": "ftp://nope"})


if __name__ == "__main__":
    print("Запуск: pytest -q tests/test_video_campaigns.py")
