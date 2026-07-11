"""Офлайн-тесты §11: создание GDN-кампании из фото за двойным гейтом + подготовка изображений.

Без живого Google Ads — _create_gdn_campaign_via_sdk подменяется monkeypatch'ем; проверяем оба
гейта (замок аккаунта + confirm + user_initiated), валидацию состава/длины/URL/бюджета В КОДЕ ДО
claim, статус PAUSED, кроп изображений (Pillow), временное хранилище медиа и защиту от traversal.
"""

from __future__ import annotations

import io
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402


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
    campaign_name="Весна 2026",
    landscape_bytes=b"L",
    square_bytes=b"S",
    headlines=["Доставка цветов", "Букеты от 299"],
    long_headline="Свежие букеты с доставкой по городу за час — закажи онлайн",
    descriptions=["Большой выбор букетов на любой повод. Доставка за час."],
    business_name="Aimash Flowers",
    final_url="https://example.com/",
    budget_daily_micros=50_000_000,
)


def _png(w, h) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (20, 40, 60)).save(buf, format="PNG")
    return buf.getvalue()


# ── apply_create_gdn_campaign: оба гейта + user_initiated + PAUSED ───────────────
async def test_apply_create_gdn_happy_path():
    called = {}

    def fake(client, customer_id, **kw):
        called.update(customer_id=customer_id, **kw)
        return {"applied": True, "status": "PAUSED", "campaign": "customers/x/campaigns/1"}

    store = FakeStore(FakeProposal("create_gdn_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_gdn_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_create_gdn_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
            **_VALID,
        )
    assert res["applied"] is True and res["status"] == "PAUSED"
    assert called["customer_id"] == DRAFT_ACCOUNT_ID
    assert called["campaign_name"] == "Весна 2026"
    assert called["budget_micros"] == 50_000_000  # apply прокинул как budget_micros
    assert store.finalized is True


async def test_apply_create_gdn_passes_geo_to_sdk():
    """§11: geo_locations из proposal доходят до SDK-цепочки (плюс страна/локаль)."""
    called = {}

    def fake(client, customer_id, **kw):
        called.update(**kw)
        return {
            "applied": True,
            "status": "PAUSED",
            "geo": len(kw.get("geo_locations") or []),
        }

    store = FakeStore(FakeProposal("create_gdn_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_gdn_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_create_gdn_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
            geo_locations=["Кения", "Найроби"],
            geo_country_code="KE",
            geo_locale="en",
            **_VALID,
        )
    assert called["geo_locations"] == ["Кения", "Найроби"]
    assert called["geo_country_code"] == "KE" and called["geo_locale"] == "en"
    assert res["geo"] == 2


def test_geo_result_warns_on_partial_geo():
    """B2: гео медиа-кампаний ложилось в `geo_count`, которого не читает НИКТО → «кампания на Кению»
    без гео (глобальный показ при запуске) выглядела на карточке как полный успех. Теперь ключ `geo`
    + явный warning частичного успеха (контракт тот же, что у create_search_campaign)."""
    assert mut._geo_result(["Кения", "Найроби"], 2) == {"geo": 2}
    assert mut._geo_result(None, 0) == {"geo": 0}
    partial = mut._geo_result(["Кения", "Найроби"], 0)
    assert partial["geo"] == 0
    assert partial["warnings"] == [{"part": "geo", "requested": 2, "applied": 0}]


def test_create_gdn_via_sdk_invokes_geo_helper_when_provided():
    """§11: _create_gdn_campaign_via_sdk вызывает reuse-геохелпер с campaign_id и локациями."""
    geo_calls = {}

    def fake_geo(client, cid, campaign_id, locations, country, locale):
        geo_calls.update(
            cid=cid,
            campaign_id=campaign_id,
            locations=list(locations),
            country=country,
            locale=locale,
        )
        return {"count": len(locations)}

    class _Proto(list):
        """Рекурсивный proto-подобный фейк: любой атрибут — снова _Proto (и это list → .append)."""

        def __getattr__(self, k):
            v = _Proto()
            object.__setattr__(self, k, v)
            return v

    class _AllSvc:
        def mutate_campaign_budgets(self, customer_id, operations):
            return _resp(["customers/x/campaignBudgets/9"])

        def mutate_campaigns(self, customer_id, operations):
            return _resp(["customers/x/campaigns/55"])

        def mutate_ad_groups(self, customer_id, operations):
            return _resp(["customers/x/adGroups/7"])

        def mutate_ad_group_ads(self, customer_id, operations):
            return _resp(["customers/x/adGroupAds/7~1"])

    class _Client:
        enums = _Proto()

        def get_service(self, name):
            return _AllSvc()

        def get_type(self, name):
            return _Proto()

    with (
        patched(mut, "_set_geo_location_via_sdk", fake_geo),
        patched(
            __import__("ads.assets", fromlist=["upload_image_asset"]),
            "upload_image_asset",
            lambda *a, **k: "customers/x/assets/1",
        ),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        res = mut._create_gdn_campaign_via_sdk(
            _Client(),
            DRAFT_ACCOUNT_ID,
            campaign_name="Кения авто",
            landscape_bytes=b"L",
            square_bytes=b"S",
            headlines=["Заголовок"],
            long_headline="Длинный заголовок объявления",
            descriptions=["Описание"],
            business_name="Aimash",
            final_url="https://example.com/",
            budget_micros=50_000_000,
            cpc_bid_micros=500_000,
            geo_locations=["Кения"],
            geo_country_code="KE",
            geo_locale="en",
        )
    assert geo_calls["campaign_id"] == "55"  # из campaign resource_name
    assert geo_calls["locations"] == ["Кения"]
    # B2: ключ `geo` (его читает bot.texts), а не мёртвый `geo_count` — гео обязано быть на карточке.
    assert res["geo"] == 1
    assert "geo_count" not in res
    assert not res.get("warnings")  # всё гео применено → warning не порождаем


async def test_apply_create_gdn_blocked_when_not_user_initiated():
    store = FakeStore(FakeProposal("create_gdn_campaign", "confirmed", user_initiated=False))
    with (
        patched(mut, "_create_gdn_campaign_via_sdk", lambda *a, **k: {"applied": True}),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        with pytest.raises(PermissionError):
            await mut.apply_create_gdn_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="x",
                confirm_store=store,
                ads_client=object(),
                **_VALID,
            )
    assert store.finalized is False


async def test_apply_create_gdn_rejects_foreign_account():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("create_gdn_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_gdn_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            await mut.apply_create_gdn_campaign(
                customer_id="1234567890",  # чужой → замок ДО SDK
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                **_VALID,
            )
    assert calls["n"] == 0 and store.finalized is False


# ── Валидация состава/длины В КОДЕ (golden rule #4) — ДО claim ───────────────────
def _validate(**over):
    args = {**_VALID, **over}
    mut._validate_gdn_inputs(
        args["headlines"],
        args["long_headline"],
        args["descriptions"],
        args["business_name"],
        args["final_url"],
        args["budget_daily_micros"],
    )


def test_validate_gdn_rejects_overlong_headline_cyrillic():
    with pytest.raises(ValueError):
        _validate(headlines=["а" * 31])  # 31 кириллический символ (>30, кириллица=1)


def test_validate_gdn_rejects_too_many_headlines():
    with pytest.raises(ValueError):
        _validate(headlines=["заголовок"] * 6)  # >5


def test_validate_gdn_rejects_overlong_long_headline():
    with pytest.raises(ValueError):
        _validate(long_headline="о" * 91)  # >90


def test_validate_gdn_rejects_bad_url():
    with pytest.raises(ValueError):
        _validate(final_url="ftp://nope")


def test_validate_gdn_rejects_overbig_budget():
    with pytest.raises(ValueError):
        _validate(budget_daily_micros=2 * mut.MAX_AMOUNT_MICROS)


def test_validate_gdn_rejects_overlong_business_name():
    with pytest.raises(ValueError):
        _validate(business_name="К" * 26)  # >25


def test_validate_gdn_accepts_valid():
    _validate()  # не бросает


async def test_apply_create_gdn_validates_before_claim():
    """Перелимит ловится ДО claim: SDK не зван, черновик не финализирован."""
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("create_gdn_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_gdn_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(ValueError):
            await mut.apply_create_gdn_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                **{**_VALID, "headlines": ["а" * 31]},
            )
    assert calls["n"] == 0 and store.finalized is False


# ── capability-guard зеркало ─────────────────────────────────────────────────────
def test_create_gdn_in_supported_operations():
    from ads.service import SUPPORTED_OPERATIONS

    assert "create_gdn_campaign" in SUPPORTED_OPERATIONS


# ── Откат бюджета при сбое создания кампании (защита от осиротевших ресурсов) ─────
class _Auto:
    """Авто-namespace: любой неустановленный атрибут авто-создаётся (имитирует proto-объект)."""

    def __getattr__(self, k):
        v = _Auto()
        object.__setattr__(self, k, v)
        return v


def _resp(rns):
    return SimpleNamespace(results=[SimpleNamespace(resource_name=rn) for rn in rns])


def test_create_gdn_via_sdk_rolls_back_budget_on_campaign_failure():
    """Если создание кампании падает — осиротевший бюджет удаляется (best-effort), без накопления."""
    removed: list[str] = []

    class _BudgetSvc:
        def mutate_campaign_budgets(self, customer_id, operations):
            op = operations[0]
            rem = getattr(op, "remove", None)
            if isinstance(rem, str):  # remove-операция = откат
                removed.append(rem)
                return _resp([])
            return _resp(["customers/x/campaignBudgets/9"])

    class _CampSvc:
        def mutate_campaigns(self, customer_id, operations):
            raise RuntimeError("BOOM: DUPLICATE_CAMPAIGN_NAME")

    class _AssetSvc:
        def mutate_assets(self, customer_id, operations):
            return _resp(["customers/x/assets/1"])

    services = {
        "AssetService": _AssetSvc(),
        "CampaignBudgetService": _BudgetSvc(),
        "CampaignService": _CampSvc(),
    }

    class _Client:
        enums = _Auto()

        def get_service(self, name):
            return services[name]

        def get_type(self, name):
            return _Auto()

    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            mut._create_gdn_campaign_via_sdk(
                _Client(),
                DRAFT_ACCOUNT_ID,
                campaign_name="Дубль",
                landscape_bytes=b"L",
                square_bytes=b"S",
                headlines=["Заголовок"],
                long_headline="Длинный заголовок",
                descriptions=["Описание"],
                business_name="Aimash",
                final_url="https://example.com/",
                budget_micros=50_000_000,
                cpc_bid_micros=500_000,
            )
            raise AssertionError("ожидался RuntimeError от mutate_campaigns")
        except RuntimeError:
            pass
    assert removed == ["customers/x/campaignBudgets/9"]  # осиротевший бюджет удалён


# ── Подготовка изображений (Pillow): кроп в 1.91:1 + 1:1 ─────────────────────────
def test_prepare_display_images_produces_required_aspect_ratios():
    from ads.assets import prepare_display_images

    land, sq = prepare_display_images(_png(1000, 500))
    li = Image.open(io.BytesIO(land))
    si = Image.open(io.BytesIO(sq))
    assert li.size == (1200, 628)  # 1.91:1
    assert si.size == (600, 600)  # 1:1


def test_prepare_display_images_rejects_non_image():
    from ads.assets import prepare_display_images

    with pytest.raises(ValueError):
        prepare_display_images(b"not an image")


# ── Временное хранилище медиа + защита от path-traversal ─────────────────────────
def test_pending_media_roundtrip_and_clear():
    from ads.assets import clear_pending_media, load_pending_media, save_pending_media

    mid = "abc123def"
    save_pending_media(mid, b"LAND", b"SQUARE")
    land, sq = load_pending_media(mid)
    assert land == b"LAND" and sq == b"SQUARE"
    clear_pending_media(mid)
    with pytest.raises(FileNotFoundError):
        load_pending_media(mid)


def test_pending_media_rejects_traversal_id():
    from ads.assets import load_pending_media, save_pending_media

    for bad in ["../evil", "a/b", "a.b", "x" * 100 + "/"]:
        with pytest.raises(ValueError):
            save_pending_media(bad, b"x", b"y")
        with pytest.raises(ValueError):
            load_pending_media(bad)


# ── Схема CreateGdnCampaign ──────────────────────────────────────────────────────
def test_gdn_schema_validates_and_rejects():
    from agent.tools.schemas import CreateGdnCampaign

    ok = CreateGdnCampaign(
        campaign_name="Весна",
        headlines=["Заголовок"],
        long_headline="Длинный заголовок объявления для медийной сети",
        descriptions=["Описание"],
        business_name="Aimash",
        final_url="https://example.com/",
        budget_daily_micros=50_000_000,
        media_id="abc123",
    )
    assert ok.media_id == "abc123"
    with pytest.raises(Exception):
        CreateGdnCampaign(  # media_id с traversal
            campaign_name="X",
            headlines=["H"],
            long_headline="L",
            descriptions=["D"],
            business_name="B",
            final_url="https://x/",
            budget_daily_micros=1,
            media_id="../evil",
        )
    with pytest.raises(Exception):
        CreateGdnCampaign(  # заголовок >30
            campaign_name="X",
            headlines=["а" * 31],
            long_headline="L",
            descriptions=["D"],
            business_name="B",
            final_url="https://x/",
            budget_daily_micros=1,
            media_id="abc",
        )


def test_gdn_schema_accepts_geo_locations():
    """§11: схема принимает опц. geo_locations (пусто по умолчанию)."""
    from agent.tools.schemas import CreateGdnCampaign

    default = CreateGdnCampaign(
        campaign_name="X",
        headlines=["H"],
        long_headline="L",
        descriptions=["D"],
        business_name="B",
        final_url="https://x/",
        budget_daily_micros=1,
        media_id="abc",
    )
    assert default.geo_locations == []  # опционально, по умолчанию пусто
    with_geo = CreateGdnCampaign(
        campaign_name="X",
        headlines=["H"],
        long_headline="L",
        descriptions=["D"],
        business_name="B",
        final_url="https://x/",
        budget_daily_micros=1,
        media_id="abc",
        geo_locations=["Кения", "Найроби"],
    )
    assert with_geo.geo_locations == ["Кения", "Найроби"]


def test_parse_gdn_brief_optional_geo():
    """§11: _parse_gdn_brief разбирает опц. 4-е поле «гео» (локации через запятую)."""
    from bot.main import _parse_gdn_brief

    # без гео (3 поля) — совместимость
    r3 = _parse_gdn_brief("Весна | https://shop.example | 50")
    assert r3 is not None and r3[3] == []
    # с гео (4 поля)
    r4 = _parse_gdn_brief("Кения авто | https://kasimotors.co.ke | 40 | Кения, Найроби")
    assert r4 is not None
    name, url, budget, geo = r4
    assert budget == 40.0 and geo == ["Кения", "Найроби"]
    # мусорный формат (2 поля) — None
    assert _parse_gdn_brief("just text") is None
