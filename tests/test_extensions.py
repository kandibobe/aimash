"""§3-assets: текстовые расширения (sitelinks/callouts/structured snippets) + список/удаление.

Офлайн (SDK подменён фейком). Покрываем: длины (asset_len_ok), схемы, два гейта (замок + confirm),
validate-before-claim, replay one-shot, SDK-ветки (oneof/final_urls/field_type), list_campaign_assets,
SUPPORTED_OPERATIONS. Стиль — как tests/test_audiences.py.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.extensions as ext  # noqa: E402
import ads.mutations as mut  # noqa: E402
from ads import read  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402

# Дублёры стора/черновика — общие (tests/conftest.py). Своя копия здесь была байт-в-байт такой же и
# уже отстала от контракта: Волна 1.1 добавила `get_confirmed` (гейт A читает снимок из стора), и без
# него любая STRICT-операция получала «снимка нет ⇒ отказ». Предмет этого файла — ассеты, не свежесть.
from conftest import FakeConfirmStore, FakeProposal  # noqa: E402
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


# ── длины ассетов ─────────────────────────────────────────────────────────────────
def test_asset_len_cyrillic_is_one():
    from adcopy.validate import asset_len_ok

    assert asset_len_ok("А" * 25, "callout") == (True, 25)  # кириллица = 1
    assert asset_len_ok("А" * 26, "callout")[0] is False
    with pytest.raises(ValueError):
        asset_len_ok("x", "unknown_kind")


# ── схемы ───────────────────────────────────────────────────────────────────────────
def test_sitelink_schema_validates():
    from agent.tools.schemas import AddSitelinks

    ok = AddSitelinks(
        campaign="X",
        sitelinks=[
            {"link_text": "Купить", "final_url": "https://shop.ua", "description1": "быстро"}
        ],
    )
    assert ok.sitelinks[0].link_text == "Купить"
    with pytest.raises(Exception):  # link_text > 25
        AddSitelinks(campaign="X", sitelinks=[{"link_text": "А" * 26, "final_url": "https://s.ua"}])
    with pytest.raises(Exception):  # description2 без description1
        AddSitelinks(
            campaign="X",
            sitelinks=[{"link_text": "ok", "final_url": "https://s.ua", "description2": "d2"}],
        )
    with pytest.raises(Exception):  # плохой url
        AddSitelinks(campaign="X", sitelinks=[{"link_text": "ok", "final_url": "ftp://x"}])


def test_callouts_schema_dedup_and_len():
    from agent.tools.schemas import AddCallouts

    ok = AddCallouts(campaign="X", callouts=[" Доставка ", "доставка", "Гарантия"])
    # trim + dedup (casefold): «Доставка»/«доставка» → одно
    assert ok.callouts == ["Доставка", "Гарантия"]
    with pytest.raises(Exception):
        AddCallouts(campaign="X", callouts=["А" * 26])


def test_snippets_schema_header_and_values():
    from agent.tools.schemas import AddStructuredSnippets

    ok = AddStructuredSnippets(campaign="X", header="Brands", values=["A", "B", "C"])
    assert ok.header == "Brands"
    with pytest.raises(Exception):  # header не из канонического списка
        AddStructuredSnippets(campaign="X", header="Бренды", values=["A", "B", "C"])
    with pytest.raises(Exception):  # < 3 значений
        AddStructuredSnippets(campaign="X", header="Brands", values=["A", "B"])


def test_remove_asset_link_schema_requires_campaign_assets_rn():
    from agent.tools.schemas import RemoveAssetLink

    ok = RemoveAssetLink(link_resource_names=["customers/1/campaignAssets/5~SITELINK"])
    assert ok.link_resource_names
    with pytest.raises(Exception):
        RemoveAssetLink(link_resource_names=["customers/1/assets/9"])  # не campaign_asset


# ── apply_add_sitelinks: оба гейта ────────────────────────────────────────────────
_SLS = [{"link_text": "Купить", "final_url": "https://shop.ua"}]


async def test_apply_add_sitelinks_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, sitelinks):
        called.update(campaign_id=campaign_id, n=len(sitelinks))
        return {"applied": True, "count": len(sitelinks)}

    store = FakeConfirmStore(FakeProposal("add_sitelinks", "confirmed", user_initiated=False))
    with patched(ext, "_add_sitelinks_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_add_sitelinks(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            sitelinks=_SLS,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] and called["campaign_id"] == "23"
    assert store.finalized is True


async def test_apply_add_sitelinks_validate_before_claim():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeConfirmStore(FakeProposal("add_sitelinks", "confirmed", user_initiated=False))
    bad = [{"link_text": "А" * 26, "final_url": "https://shop.ua"}]  # >25 кириллицей
    with patched(ext, "_add_sitelinks_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(ValueError):
            await mut.apply_add_sitelinks(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                sitelinks=bad,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
    assert calls["n"] == 0 and store.finalized is False


async def test_apply_add_sitelinks_foreign_and_no_confirmation():
    store = FakeConfirmStore(FakeProposal("add_sitelinks", "confirmed", user_initiated=False))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):  # чужой аккаунт
            await mut.apply_add_sitelinks(
                customer_id="1234567890",
                campaign_id="23",
                sitelinks=_SLS,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
    assert store.finalized is False
    store2 = FakeConfirmStore(proposal=None)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):  # без подтверждения
            await mut.apply_add_sitelinks(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                sitelinks=_SLS,
                confirmation_id="bogus",
                confirm_store=store2,
                ads_client=object(),
            )
    assert store2.finalized is False


async def test_apply_add_sitelinks_replay_one_shot():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeConfirmStore(FakeProposal("add_sitelinks", "confirmed", user_initiated=False))
    with patched(ext, "_add_sitelinks_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        await mut.apply_add_sitelinks(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            sitelinks=_SLS,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
        with pytest.raises(PermissionError):  # повтор того же confirmation_id
            await mut.apply_add_sitelinks(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                sitelinks=_SLS,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
    assert calls["n"] == 1  # SDK вызван РОВНО один раз


# ── SDK-ветки ────────────────────────────────────────────────────────────────────────
class _FakeAssetSvc:
    def __init__(self, cap):
        self._cap = cap

    def mutate_assets(self, customer_id, operations):
        self._cap["asset_ops"] = list(operations)
        return SimpleNamespace(
            results=[
                SimpleNamespace(resource_name=f"customers/1/assets/{i}")
                for i in range(len(operations))
            ]
        )


class _FakeCampAssetSvc:
    def __init__(self, cap):
        self._cap = cap

    def mutate_campaign_assets(self, customer_id, operations):
        self._cap["link_ops"] = list(operations)
        return SimpleNamespace(
            results=[
                SimpleNamespace(resource_name=f"customers/1/campaignAssets/{i}")
                for i in range(len(operations))
            ]
        )


class _FakeCampSvc:
    def campaign_path(self, cid, campid):
        return f"customers/{cid}/campaigns/{campid}"


class _FakeClient:
    def __init__(self):
        self.cap = {}
        self.enums = SimpleNamespace(
            AssetFieldTypeEnum=SimpleNamespace(
                SITELINK="SITELINK",
                CALLOUT="CALLOUT",
                STRUCTURED_SNIPPET="STRUCTURED_SNIPPET",
                MARKETING_IMAGE="MARKETING_IMAGE",
                CALL="CALL",
                PROMOTION="PROMOTION",
                PRICE="PRICE",
            ),
            PriceExtensionTypeEnum=SimpleNamespace(SERVICES="SERVICES", BRANDS="BRANDS"),
            PriceExtensionPriceUnitEnum=SimpleNamespace(PER_MONTH="PER_MONTH", PER_DAY="PER_DAY"),
        )

    def get_service(self, name):
        if name == "AssetService":
            return _FakeAssetSvc(self.cap)
        if name == "CampaignAssetService":
            return _FakeCampAssetSvc(self.cap)
        if name == "CampaignService":
            return _FakeCampSvc()
        raise AssertionError(name)

    def get_type(self, name):
        if name == "AssetOperation":
            return SimpleNamespace(
                create=SimpleNamespace(
                    sitelink_asset=SimpleNamespace(
                        link_text=None, description1=None, description2=None
                    ),
                    callout_asset=SimpleNamespace(callout_text=None),
                    structured_snippet_asset=SimpleNamespace(header=None, values=[]),
                    call_asset=SimpleNamespace(country_code=None, phone_number=None),
                    promotion_asset=SimpleNamespace(
                        promotion_target=None,
                        percent_off=None,
                        money_amount_off=SimpleNamespace(amount_micros=None, currency_code=None),
                        promotion_code=None,
                    ),
                    price_asset=SimpleNamespace(type_=None, language_code=None, price_offerings=[]),
                    final_urls=[],
                )
            )
        if name == "CampaignAssetOperation":
            return SimpleNamespace(
                create=SimpleNamespace(campaign=None, asset=None, field_type=None), remove=None
            )
        if name == "PriceOffering":
            return SimpleNamespace(
                header=None,
                description=None,
                price=SimpleNamespace(amount_micros=None, currency_code=None),
                final_url=None,
                unit=None,
            )
        raise AssertionError(name)


def test_add_sitelinks_via_sdk_branches():
    client = _FakeClient()
    res = ext._add_sitelinks_via_sdk(
        client,
        DRAFT_ACCOUNT_ID,
        "23",
        [{"link_text": "Купить", "final_url": "https://shop.ua", "description1": "d1"}],
    )
    assert res["applied"] and res["count"] == 1
    aop = client.cap["asset_ops"][0]
    assert aop.create.sitelink_asset.link_text == "Купить"
    assert aop.create.sitelink_asset.description1 == "d1"
    assert list(aop.create.final_urls) == [
        "https://shop.ua"
    ]  # final_urls на .create (Asset), не на sitelink_asset
    lop = client.cap["link_ops"][0]
    assert lop.create.field_type == "SITELINK"
    assert lop.create.asset == "customers/1/assets/0"


def test_add_callouts_via_sdk_branches():
    client = _FakeClient()
    res = ext._add_callouts_via_sdk(client, DRAFT_ACCOUNT_ID, "23", ["Гарантия", "Доставка"])
    assert res["count"] == 2
    assert client.cap["asset_ops"][0].create.callout_asset.callout_text == "Гарантия"
    assert all(op.create.field_type == "CALLOUT" for op in client.cap["link_ops"])


def test_add_structured_snippets_via_sdk_branches():
    client = _FakeClient()
    res = ext._add_structured_snippets_via_sdk(
        client, DRAFT_ACCOUNT_ID, "23", "Brands", ["A", "B", "C"]
    )
    assert res["count"] == 1
    aop = client.cap["asset_ops"][0]
    assert aop.create.structured_snippet_asset.header == "Brands"
    assert list(aop.create.structured_snippet_asset.values) == ["A", "B", "C"]
    assert client.cap["link_ops"][0].create.field_type == "STRUCTURED_SNIPPET"


def test_remove_campaign_assets_via_sdk():
    client = _FakeClient()
    res = ext._remove_campaign_assets_via_sdk(
        client, DRAFT_ACCOUNT_ID, ["customers/1/campaignAssets/5"]
    )
    assert res["count"] == 1
    assert client.cap["link_ops"][0].remove == "customers/1/campaignAssets/5"


# ── list_campaign_assets ─────────────────────────────────────────────────────────────
def test_list_campaign_assets_labels_by_field_type():
    rows = [
        SimpleNamespace(
            campaign_asset=SimpleNamespace(
                resource_name="customers/1/campaignAssets/1",
                field_type=SimpleNamespace(name="SITELINK"),
                asset="customers/1/assets/1",
            ),
            asset=SimpleNamespace(
                type=SimpleNamespace(name="SITELINK"),
                name="",
                sitelink_asset=SimpleNamespace(link_text="Купить"),
                callout_asset=SimpleNamespace(callout_text=""),
                structured_snippet_asset=SimpleNamespace(header="", values=[]),
            ),
        ),
        SimpleNamespace(
            campaign_asset=SimpleNamespace(
                resource_name="customers/1/campaignAssets/2",
                field_type=SimpleNamespace(name="CALLOUT"),
                asset="customers/1/assets/2",
            ),
            asset=SimpleNamespace(
                type=SimpleNamespace(name="CALLOUT"),
                name="",
                sitelink_asset=SimpleNamespace(link_text=""),
                callout_asset=SimpleNamespace(callout_text="Гарантия"),
                structured_snippet_asset=SimpleNamespace(header="", values=[]),
            ),
        ),
    ]

    class _GA:
        def search(self, customer_id, query):
            return rows

    class _Client:
        def get_service(self, name):
            return _GA()

    with allowed_ids(DRAFT_ACCOUNT_ID):
        out = read.list_campaign_assets(_Client(), DRAFT_ACCOUNT_ID, "23")
    assert [r.field_type for r in out] == ["SITELINK", "CALLOUT"]
    assert out[0].label == "Купить" and out[1].label == "Гарантия"
    assert out[0].link_resource_name == "customers/1/campaignAssets/1"


def test_list_campaign_assets_rejects_foreign_account():
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            read.list_campaign_assets(object(), "1234567890", "23")


# ── allow-list синхронен ──────────────────────────────────────────────────────────
def test_asset_ops_in_supported_operations():
    from ads.service import SUPPORTED_OPERATIONS

    for op in (
        "add_sitelinks",
        "add_callouts",
        "add_structured_snippets",
        "attach_image_asset",
        "add_call_asset",
        "add_promotion",
        "add_price_asset",
        "remove_asset_link",
    ):
        assert op in SUPPORTED_OPERATIONS


# ── семейство 3: схемы ────────────────────────────────────────────────────────────
def test_call_asset_schema():
    from agent.tools.schemas import AddCallAsset

    ok = AddCallAsset(campaign="X", phone_number="+380 (44) 123-45-67", country_code="ua")
    assert ok.country_code == "UA"
    with pytest.raises(Exception):
        AddCallAsset(campaign="X", phone_number="abc", country_code="UA")  # буквы в телефоне
    with pytest.raises(Exception):
        AddCallAsset(campaign="X", phone_number="+380441234567", country_code="UKR")  # не 2 буквы


def test_promotion_schema_exactly_one_discount():
    from agent.tools.schemas import AddPromotion

    ok = AddPromotion(
        campaign="X", promotion_target="Лето", final_url="https://s.ua", percent_off=20
    )
    assert ok.percent_off == 20
    with pytest.raises(Exception):  # обе скидки
        AddPromotion(
            campaign="X",
            promotion_target="L",
            final_url="https://s.ua",
            percent_off=10,
            money_off_units=5,
            currency="USD",
        )
    with pytest.raises(Exception):  # ни одной
        AddPromotion(campaign="X", promotion_target="L", final_url="https://s.ua")
    with pytest.raises(Exception):  # money без currency
        AddPromotion(
            campaign="X", promotion_target="L", final_url="https://s.ua", money_off_units=5
        )
    with pytest.raises(Exception):  # percent вне (0,100]
        AddPromotion(campaign="X", promotion_target="L", final_url="https://s.ua", percent_off=150)


def test_price_schema_offering_bounds():
    from agent.tools.schemas import AddPriceAsset

    offs = [
        {
            "header": "Basic",
            "description": "Старт",
            "price_units": 9.99,
            "final_url": "https://s.ua/b",
        },
        {
            "header": "Pro",
            "description": "Рост",
            "price_units": 19.99,
            "final_url": "https://s.ua/p",
        },
        {
            "header": "Max",
            "description": "Всё",
            "price_units": 49.99,
            "final_url": "https://s.ua/m",
        },
    ]
    ok = AddPriceAsset(campaign="X", currency="USD", offerings=offs)
    assert len(ok.offerings) == 3 and ok.price_type == "services"
    with pytest.raises(Exception):  # < 3 оферов
        AddPriceAsset(campaign="X", currency="USD", offerings=offs[:2])
    with pytest.raises(Exception):  # header > 25
        AddPriceAsset(
            campaign="X",
            currency="USD",
            offerings=[{**offs[0], "header": "А" * 26}, offs[1], offs[2]],
        )


# ── семейство 3: SDK-ветки (включая КРИТИЧНОЕ масштабирование percent_off) ─────────────
def test_add_call_via_sdk():
    client = _FakeClient()
    res = ext._add_call_asset_via_sdk(client, DRAFT_ACCOUNT_ID, "23", "+380441234567", "UA")
    assert res["count"] == 1
    aop = client.cap["asset_ops"][0]
    assert aop.create.call_asset.country_code == "UA"
    assert aop.create.call_asset.phone_number == "+380441234567"
    assert client.cap["link_ops"][0].create.field_type == "CALL"


def test_add_promotion_via_sdk_percent_scaling():
    # КРИТИЧНО: 1_000_000 == 100% → 20% должно стать 200_000 (НЕ 20_000_000).
    client = _FakeClient()
    res = ext._add_promotion_via_sdk(
        client,
        DRAFT_ACCOUNT_ID,
        "23",
        promotion_target="Лето",
        final_url="https://shop.ua",
        percent_off=20,
    )
    assert res["count"] == 1
    aop = client.cap["asset_ops"][0]
    assert aop.create.promotion_asset.percent_off == 200_000  # 20 × 10_000
    assert list(aop.create.final_urls) == ["https://shop.ua"]  # на Asset, не на promotion_asset
    assert client.cap["link_ops"][0].create.field_type == "PROMOTION"


def test_add_promotion_via_sdk_money_off():
    client = _FakeClient()
    ext._add_promotion_via_sdk(
        client,
        DRAFT_ACCOUNT_ID,
        "23",
        promotion_target="Скидка",
        final_url="https://shop.ua",
        money_off_units=5,
        currency="USD",
    )
    mo = client.cap["asset_ops"][0].create.promotion_asset.money_amount_off
    assert (
        mo.amount_micros == 5_000_000 and mo.currency_code == "USD"
    )  # 1_000_000 micros = 1 единица


def test_add_price_via_sdk():
    client = _FakeClient()
    offs = [
        {
            "header": "Basic",
            "description": "Старт",
            "price_units": 9.99,
            "final_url": "https://s.ua/b",
            "unit": "per_month",
        },
        {
            "header": "Pro",
            "description": "Рост",
            "price_units": 19.99,
            "final_url": "https://s.ua/p",
        },
        {"header": "Max", "description": "Всё", "price_units": 49.0, "final_url": "https://s.ua/m"},
    ]
    res = ext._add_price_asset_via_sdk(
        client,
        DRAFT_ACCOUNT_ID,
        "23",
        price_type="services",
        currency="USD",
        language_code="uk",
        offerings=offs,
    )
    assert res["count"] == 1 and res["offerings"] == 3
    pa = client.cap["asset_ops"][0].create.price_asset
    assert pa.type_ == "SERVICES" and pa.language_code == "uk"
    assert pa.price_offerings[0].price.amount_micros == 9_990_000  # 9.99 × 1e6
    assert pa.price_offerings[0].unit == "PER_MONTH"
    assert client.cap["link_ops"][0].create.field_type == "PRICE"


# ── семейство 3: гейты apply_* (happy / validate-before-claim / no-confirmation) ──────
async def test_apply_add_call_asset_gates():
    calls = {"n": 0}

    def fake(client, customer_id, campaign_id, phone, cc):
        calls["n"] += 1
        return {"applied": True, "count": 1}

    store = FakeConfirmStore(FakeProposal("add_call_asset", "confirmed", user_initiated=False))
    with patched(ext, "_add_call_asset_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_add_call_asset(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            phone_number="+380441234567",
            country_code="UA",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] and calls["n"] == 1 and store.finalized is True
    # без подтверждения → PermissionError, SDK не зван
    store2 = FakeConfirmStore(proposal=None)
    with patched(ext, "_add_call_asset_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            await mut.apply_add_call_asset(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                phone_number="+380441234567",
                country_code="UA",
                confirmation_id="bogus",
                confirm_store=store2,
                ads_client=object(),
            )
    assert calls["n"] == 1  # не вырос


async def test_apply_add_promotion_validate_before_claim():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeConfirmStore(FakeProposal("add_promotion", "confirmed", user_initiated=False))
    with patched(ext, "_add_promotion_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(ValueError):  # ни одной скидки → отказ ДО claim
            await mut.apply_add_promotion(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                promotion_target="L",
                final_url="https://s.ua",
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
    assert calls["n"] == 0 and store.finalized is False


async def test_apply_add_price_foreign_account():
    store = FakeConfirmStore(FakeProposal("add_price_asset", "confirmed", user_initiated=False))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):  # чужой аккаунт → замок до всего
            await mut.apply_add_price_asset(
                customer_id="1234567890",
                campaign_id="23",
                price_type="services",
                currency="USD",
                language_code="uk",
                offerings=[
                    {
                        "header": "A",
                        "description": "a",
                        "price_units": 1,
                        "final_url": "https://s.ua",
                    }
                ]
                * 3,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
    assert store.finalized is False


# ── image-ассет (семейство 2): мутация + SDK-ветка ───────────────────────────────────
async def test_apply_attach_image_asset_happy_and_gates():
    called = {}

    def fake(client, customer_id, campaign_id, image_bytes, name):
        called.update(campaign_id=campaign_id, name=name, n=len(image_bytes))
        return {"applied": True, "count": 1}

    store = FakeConfirmStore(FakeProposal("attach_image_asset", "confirmed", user_initiated=False))
    with patched(ext, "_attach_image_asset_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_attach_image_asset(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            image_bytes=b"jpegdata",
            name="img1",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] and called["campaign_id"] == "23" and called["n"] == 8
    assert store.finalized is True


async def test_apply_attach_image_asset_empty_bytes_before_claim():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeConfirmStore(FakeProposal("attach_image_asset", "confirmed", user_initiated=False))
    with patched(ext, "_attach_image_asset_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(ValueError):
            await mut.apply_attach_image_asset(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                image_bytes=b"",  # пусто → ValueError ДО claim
                name="img1",
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
    assert calls["n"] == 0 and store.finalized is False


def test_attach_image_asset_via_sdk_uses_marketing_image(monkeypatch):
    client = _FakeClient()
    # upload_image_asset (ads.assets) → подменяем, чтобы не трогать Pillow/SDK
    import ads.assets as assets

    monkeypatch.setattr(assets, "upload_image_asset", lambda c, cid, b, n: "customers/1/assets/img")
    res = ext._attach_image_asset_via_sdk(client, DRAFT_ACCOUNT_ID, "23", b"jpeg", "img1")
    assert res["count"] == 1 and res["assets"] == ["customers/1/assets/img"]
    assert client.cap["link_ops"][0].create.field_type == "MARKETING_IMAGE"


# ── бот-визард расширений (парсеры + клавиатура + полный флоу → pending) ──────────────
import bot.main as bm  # noqa: E402
from bot.callbacks import CampCB, ExtCB  # noqa: E402
from bot.keyboards import ext_menu_kb  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from db.session import init_db  # noqa: E402


class _Msg:
    def __init__(self, chat_id=70_000, text=""):
        self.chat = type("C", (), {"id": chat_id})()
        self.text = text
        self.answers: list = []

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text="", **kw):
        return self


class _CQ:
    def __init__(self, message):
        self.message = message
        self.from_user = type("U", (), {"id": 1})()
        self.answers: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


class _State:
    def __init__(self):
        self._d, self._s = {}, None

    async def clear(self):
        self._d, self._s = {}, None

    async def set_state(self, s):
        self._s = s

    async def update_data(self, **kw):
        self._d.update(kw)

    async def get_data(self):
        return dict(self._d)

    async def get_state(self):
        return self._s


def test_parse_sitelinks():
    txt = "Купить | https://shop.ua | быстро\nплохая строка без url\nДоставка | https://d.ua | в день | бесплатно"
    out = bm._parse_sitelinks(txt)
    assert len(out) == 2
    assert out[0] == {
        "link_text": "Купить",
        "final_url": "https://shop.ua",
        "description1": "быстро",
    }
    assert out[1]["description2"] == "бесплатно"


def test_parse_csv_lines():
    assert bm._parse_csv_lines("a, b\n c ,,") == ["a", "b", "c"]


def test_ext_menu_kb_buttons():
    cbs = [b.callback_data for row in ext_menu_kb(2).inline_keyboard for b in row]
    exts = [ExtCB.unpack(c) for c in cbs if c.startswith("ext:")]
    assert {e.action for e in exts} == {"sitelink", "callout", "snippet", "image", "show"}
    backs = [CampCB.unpack(c) for c in cbs if c.startswith("camp:")]
    assert any(b.action == "menu" and b.idx == 2 for b in backs)


async def test_ext_sitelinks_flow_builds_pending():
    await init_db()
    chat = 70_010
    bm._CAMP_CACHE[chat] = [{"id": "111", "name": "Search Spring", "status": "ENABLED"}]
    state = _State()
    # тип выбран
    await bm.on_ext_type(_CQ(_Msg(chat)), ExtCB(action="sitelink", idx=0), state)
    assert await state.get_state() == bm.ExtWizard.awaiting_sitelinks
    # ввод текста → ТОЛЬКО черновик add_sitelinks (pending)
    await bm.ext_sitelinks(_Msg(chat, text="Купить | https://shop.ua | быстро"), state)
    cid = bm._LAST_PENDING.get(chat)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "add_sitelinks"
    assert snap.status == "pending"  # НЕ исполнено — ждёт ✅
    assert snap.params["campaign"] == "Search Spring"
    assert snap.params["sitelinks"][0]["link_text"] == "Купить"


async def test_ext_sitelinks_bad_input_keeps_nav():
    await init_db()
    chat = 70_011
    state = _State()
    await state.set_state(bm.ExtWizard.awaiting_sitelinks)
    await state.update_data(ext_campaign="X", ext_idx=0)
    msg = _Msg(chat, text="мусор без вертикальной черты")
    await bm.ext_sitelinks(msg, state)
    assert await state.get_state() == bm.ExtWizard.awaiting_sitelinks  # остаёмся
    cbs = [
        b.callback_data for row in msg.answers[-1][1]["reply_markup"].inline_keyboard for b in row
    ]
    assert any(c.startswith("nav:") for c in cbs)  # навигация на месте


async def test_ext_remove_flow_builds_pending():
    await init_db()
    chat = 70_012
    bm._LAST_PENDING.pop(chat, None)
    bm._EXT_CACHE[chat] = [
        SimpleNamespace(
            link_resource_name="customers/1/campaignAssets/9", field_type="SITELINK", label="Купить"
        )
    ]
    msg = _Msg(chat)
    await bm.on_ext_remove(_CQ(msg), ExtCB(action="remove", idx=0))
    cid = bm._LAST_PENDING.get(chat)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "remove_asset_link"
    assert snap.status == "pending"
    assert snap.params["link_resource_names"] == ["customers/1/campaignAssets/9"]
