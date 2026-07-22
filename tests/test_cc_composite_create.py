"""§19 Этап 7: расширение composite-создания (gео/язык/стратегия/display path/URL-опции/ассеты).

Без живого Google Ads: проверяем чистые/полу-чистые хелперы и проводку async-обёртки (ассеты/
изображения добавляются ПОСЛЕ кампании, сбой одного не роняет $0/PAUSED кампанию).
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
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from conftest import FakeConfirmStore, FakeProposal  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


# Дублёр стора — общий (`tests/conftest.py`), локальных копий здесь нет: контракт
# `confirm.store.ConfirmStore` зеркалится в одном месте. Черновик — `user_initiated=True`
# (создание кампании требует прямой команды, `_require_user_command`); `origin_human_turn`
# зеркалит его по умолчанию. Свежесть не задаём: `create_search_campaign` — Tier.NO_DIFF
# (создание с нуля, прежнего состояния нет), гейт A до стора не доходит.
def _store() -> FakeConfirmStore:
    return FakeConfirmStore(
        FakeProposal("create_search_campaign", "confirmed", user_initiated=True)
    )


# ── _resolve_language_ids: имя/код → languageConstant id, неизвестное пропускаем ──
def test_resolve_language_ids():
    assert mut._resolve_language_ids(["English", "ru", "Swahili"]) == [1000, 1031]
    assert mut._resolve_language_ids([]) == []


def test_resolve_language_ids_covers_full_google_table():
    """B1: резолв идёт через geo.lang_iso + keyword_plan.LANGUAGE_IDS (полная таблица Google), а не
    через собственную табличку на ru/uk/en — иначе кампания на Германию/Польшу молча оставалась без
    языкового критерия (показы на всех языках)."""
    assert mut._resolve_language_ids(["Немецкий"]) == [1001]
    assert mut._resolve_language_ids(["de"]) == [1001]
    assert mut._resolve_language_ids(["th", "Polish"]) == [1044, 1030]


def test_split_language_ids_reports_unresolved():
    """B1: нераспознанный язык возвращается ОТДЕЛЬНО (не проглатывается) — на нём строится warning."""
    ids, unresolved = mut._split_language_ids(["English", "Swahili", " "])
    assert ids == [1000]
    assert unresolved == ["Swahili"]
    # Дубли (код + имя одного языка) НЕ дают ложного «потеряли язык».
    ids, unresolved = mut._split_language_ids(["en", "English"])
    assert (ids, unresolved) == ([1000], [])


class _CritClient:
    """Мини-фейк SDK: все mutate_* возвращают по результату на операцию (успех)."""

    def __init__(self):
        self.enums = SimpleNamespace(
            AdvertisingChannelTypeEnum=SimpleNamespace(SEARCH="SEARCH"),
            CampaignStatusEnum=SimpleNamespace(PAUSED="PAUSED"),
            AdGroupStatusEnum=SimpleNamespace(PAUSED="PAUSED"),
            AdGroupAdStatusEnum=SimpleNamespace(PAUSED="PAUSED"),
            AdGroupTypeEnum=SimpleNamespace(SEARCH_STANDARD="SEARCH_STANDARD"),
            BudgetDeliveryMethodEnum=SimpleNamespace(STANDARD="STANDARD"),
            KeywordMatchTypeEnum=SimpleNamespace(PHRASE="PHRASE", EXACT="EXACT", BROAD="BROAD"),
            DayOfWeekEnum=SimpleNamespace(MONDAY="MONDAY"),
            MinuteOfHourEnum=SimpleNamespace(ZERO="ZERO"),
        )

    def get_type(self, name):
        from unittest.mock import MagicMock

        return MagicMock()

    def get_service(self, name):
        return _CritService()


class _CritService:
    def _ok(self, operations):
        return SimpleNamespace(
            results=[SimpleNamespace(resource_name=f"rn/{i}") for i in range(len(operations))],
            partial_failure_error=None,
        )

    def __getattr__(self, method):
        def _call(*, customer_id, operations, **kw):
            return self._ok(operations)

        return _call


def test_create_warns_when_language_not_resolved():
    """B1 (деньги/показы): язык, который Google не таргетит, обязан попасть в warnings. Раньше
    «запрошено» считалось от ПОСТ-резолвового списка → карточка обещала «English, Swahili», кампания
    создавалась только с English, и менеджер не видел НИ строчки об этом."""
    with (
        patched(mut, "_downgrade_bidding_if_no_conversions", lambda c, cid, b: (b, None)),
        patched(mut, "_apply_bidding_on_create", lambda c, camp, b: None),
    ):
        res = mut._create_search_campaign_via_sdk(
            _CritClient(),
            DRAFT_ACCOUNT_ID,
            campaign_name="T",
            final_url="https://x/",
            headlines=["a", "b", "c"],
            descriptions=["d", "e"],
            budget_micros=40_000_000,
            keywords=[],
            match_type="phrase",
            cpc_bid_micros=120_000,
            languages=["English", "Swahili"],
        )
    assert res["languages"] == 1
    assert {"part": "languages", "requested": 2, "applied": 1} in res["warnings"]


def test_create_no_language_warning_when_all_resolved():
    """Обратная сторона: полностью резолвнутый список НЕ порождает ложный warning."""
    with (
        patched(mut, "_downgrade_bidding_if_no_conversions", lambda c, cid, b: (b, None)),
        patched(mut, "_apply_bidding_on_create", lambda c, camp, b: None),
    ):
        res = mut._create_search_campaign_via_sdk(
            _CritClient(),
            DRAFT_ACCOUNT_ID,
            campaign_name="T",
            final_url="https://x/",
            headlines=["a", "b", "c"],
            descriptions=["d", "e"],
            budget_micros=40_000_000,
            keywords=[],
            match_type="phrase",
            cpc_bid_micros=120_000,
            languages=["Немецкий", "English"],
        )
    assert res["languages"] == 2
    assert not [w for w in res.get("warnings", []) if w["part"] == "languages"]


# ── _validate_url_options: §19.8 ─────────────────────────────────────────────────
def test_url_options_validation():
    mut._validate_url_options(None)  # ok
    mut._validate_url_options({"tracking_url_template": "{lpurl}?src=ads"})  # ok
    mut._validate_url_options({"custom_parameters": {"src": "tg"}})  # ok
    with pytest.raises(ValueError):
        mut._validate_url_options({"tracking_url_template": "no-lpurl-no-http"})
    with pytest.raises(ValueError):
        mut._validate_url_options({"final_url_suffix": "?bad=1"})  # ведущий '?'
    with pytest.raises(ValueError):
        mut._validate_url_options({"custom_parameters": {"bad key": "x"}})  # пробел в ключе
    with pytest.raises(ValueError):
        mut._validate_url_options({"custom_parameters": {f"k{i}": "v" for i in range(9)}})  # >8
    # ValueTrack {escapedlpurl}/{unescapedlpurl} — валидны (не содержат подстроку "{lpurl}")
    mut._validate_url_options({"tracking_url_template": "{unescapedlpurl}&utm_source=x"})
    mut._validate_url_options({"tracking_url_template": "{escapedlpurl}?a=b"})


# ── _apply_url_options_on_create: проставляет поля на CampaignOperation.create ────
def test_apply_url_options_on_create():
    class _CP:
        key = ""
        value = ""

    client = SimpleNamespace(get_type=lambda name: _CP())
    c = SimpleNamespace(url_custom_parameters=[])
    mut._apply_url_options_on_create(
        client,
        c,
        {
            "tracking_url_template": "{lpurl}?x=1",
            "final_url_suffix": "a=b",
            "custom_parameters": {"src": "tg"},
        },
    )
    assert c.tracking_url_template == "{lpurl}?x=1"
    assert c.final_url_suffix == "a=b"
    assert len(c.url_custom_parameters) == 1
    assert c.url_custom_parameters[0].key == "src"


# ── extensions.apply_asset_spec_via_sdk: дисп. + config-gated → NotImplementedError ─
def test_asset_dispatch_and_gated():
    calls = {}

    def fake_callouts(client, cid, campaign_id, callouts):
        calls["callouts"] = callouts
        return {"applied": True}

    with patched(ext, "_add_callouts_via_sdk", fake_callouts):
        ext.apply_asset_spec_via_sdk(
            object(), "1", "2", {"family": "callouts", "params": {"callouts": ["Гарантия"]}}
        )
    assert calls["callouts"] == ["Гарантия"]
    # location/affiliate_location — недоступны в v24 (config-gated → NotImplementedError). lead_form
    # РЕАЛИЗОВАН (см. test_lead_form_asset_*), в этот набор больше не входит.
    for gated in ("location", "affiliate_location"):
        with pytest.raises(NotImplementedError):
            ext.apply_asset_spec_via_sdk(object(), "1", "2", {"family": gated, "params": {}})
    with pytest.raises(ValueError):
        ext.apply_asset_spec_via_sdk(object(), "1", "2", {"family": "unknown", "params": {}})


# ── _attach_asset_specs_via_sdk: добавленные vs пропущенные (config-gated/сбой) ────
def test_attach_asset_specs_collects_added_and_skipped():
    def fake_dispatch(client, cid, campaign_id, spec):
        fam = spec["family"]
        if fam == "callouts":
            return {"applied": True}
        if fam == "location":
            raise NotImplementedError("недоступно в v24")
        raise RuntimeError("boom")

    with patched(ext, "apply_asset_spec_via_sdk", fake_dispatch):
        added, skipped = mut._attach_asset_specs_via_sdk(
            object(),
            DRAFT_ACCOUNT_ID,
            "123",
            [
                {"family": "callouts", "params": {}},
                {"family": "location", "params": {}},
                {"family": "price", "params": {}},
            ],
        )
    assert added == ["callouts"]
    assert {s["family"] for s in skipped} == {"location", "price"}


# ── §19.7.1: реальная сборка lead_form_asset (v24) — поля/CTA/link по именам enum ──
def test_lead_form_asset_builds_correct_op():
    """Стандартная пара create-asset → CampaignAsset-link: проверяем, что lead_form_asset заполнен
    (business_name/headline/description/privacy_url/CTA + поля формы) и линкуется field_type=LEAD_FORM."""
    captured: dict = {}

    class _LF:
        def __init__(self):
            self.fields = []

    class _AssetOp:
        def __init__(self):
            self.create = SimpleNamespace(lead_form_asset=_LF())

    class _AssetSvc:
        def mutate_assets(self, customer_id, operations):
            captured["op"] = operations[0]
            return SimpleNamespace(results=[SimpleNamespace(resource_name="customers/x/assets/9")])

    class _CampSvc:
        def campaign_path(self, cid, camp):
            return f"customers/{cid}/campaigns/{camp}"

    class _CampAssetSvc:
        def mutate_campaign_assets(self, customer_id, operations):
            captured["link_field"] = operations[0].create.field_type
            return SimpleNamespace(
                results=[SimpleNamespace(resource_name="customers/x/campaignAssets/1")]
            )

    class _Enums:
        class LeadFormCallToActionTypeEnum:
            GET_QUOTE = "GET_QUOTE"

        class LeadFormFieldUserInputTypeEnum:
            FULL_NAME = "FULL_NAME"
            EMAIL = "EMAIL"
            PHONE_NUMBER = "PHONE_NUMBER"

        class AssetFieldTypeEnum:
            LEAD_FORM = "LEAD_FORM"

    services = {
        "AssetService": _AssetSvc(),
        "CampaignService": _CampSvc(),
        "CampaignAssetService": _CampAssetSvc(),
    }

    class _Client:
        enums = _Enums()

        def get_service(self, name):
            return services[name]

        def get_type(self, name):
            if name == "AssetOperation":
                return _AssetOp()
            if name == "LeadFormField":
                return SimpleNamespace()
            return SimpleNamespace(create=SimpleNamespace())  # CampaignAssetOperation

    res = ext._add_lead_form_via_sdk(
        _Client(),
        "775",
        "55",
        business_name="Kasi Motors",
        headline="Оставьте заявку",
        description="Свяжемся с вами",
        call_to_action_description="Заявка",
        privacy_policy_url="https://kasi.example/privacy",
    )
    lf = captured["op"].create.lead_form_asset
    assert lf.business_name == "Kasi Motors"
    assert lf.privacy_policy_url == "https://kasi.example/privacy"
    assert lf.call_to_action_type == "GET_QUOTE"
    assert [f.input_type for f in lf.fields] == ["FULL_NAME", "EMAIL", "PHONE_NUMBER"]
    assert captured["link_field"] == "LEAD_FORM"  # линк с правильным field_type
    assert res["applied"] and res["kind"] == "lead_form" and res["count"] == 1


# ── §19.3: конверс-стратегия без отслеживания конверсий → понижение до Maximize Clicks ──
class _FakeEnums:
    class ConversionTrackingStatusEnum:
        UNSPECIFIED = 0
        UNKNOWN = 1
        NOT_CONVERSION_TRACKED = 2
        CONVERSION_TRACKING_MANAGED_BY_SELF = 3


def _fake_client_with_tracking(status: int):
    row = SimpleNamespace(
        customer=SimpleNamespace(
            conversion_tracking_setting=SimpleNamespace(conversion_tracking_status=status)
        )
    )
    ga = SimpleNamespace(search=lambda **kw: [row])
    return SimpleNamespace(get_service=lambda name: ga, enums=_FakeEnums())


def test_conversion_tracking_enabled_reads_status():
    on = _fake_client_with_tracking(
        _FakeEnums.ConversionTrackingStatusEnum.CONVERSION_TRACKING_MANAGED_BY_SELF
    )
    off = _fake_client_with_tracking(_FakeEnums.ConversionTrackingStatusEnum.NOT_CONVERSION_TRACKED)
    assert mut._conversion_tracking_enabled(on, "123") is True
    assert mut._conversion_tracking_enabled(off, "123") is False


def test_conversion_tracking_read_failure_is_failsafe_off():
    def _boom(**kw):
        raise RuntimeError("no access")

    client = SimpleNamespace(
        get_service=lambda n: SimpleNamespace(search=_boom), enums=_FakeEnums()
    )
    assert mut._conversion_tracking_enabled(client, "123") is False  # сбой → выключено (fail-safe)


def test_downgrade_bidding_when_no_conversion_tracking():
    off = _fake_client_with_tracking(_FakeEnums.ConversionTrackingStatusEnum.NOT_CONVERSION_TRACKED)
    bidding = {"strategy": "maximize_conversions", "target_cpa_micros": 180_000}
    out, note = mut._downgrade_bidding_if_no_conversions(off, "123", bidding)
    assert out["strategy"] == "target_spend"  # Maximize Clicks — конверсии не нужны
    assert "target_cpa_micros" not in out  # неприменимый target убран
    assert note and "maximize_clicks" in note


def test_no_downgrade_when_tracking_on_or_non_conversion_strategy():
    on = _fake_client_with_tracking(
        _FakeEnums.ConversionTrackingStatusEnum.CONVERSION_TRACKING_MANAGED_BY_SELF
    )
    out, note = mut._downgrade_bidding_if_no_conversions(
        on, "123", {"strategy": "maximize_conversions"}
    )
    assert out["strategy"] == "maximize_conversions" and note is None  # трекинг есть → не трогаем
    off = _fake_client_with_tracking(_FakeEnums.ConversionTrackingStatusEnum.NOT_CONVERSION_TRACKED)
    out2, note2 = mut._downgrade_bidding_if_no_conversions(off, "123", {"strategy": "manual_cpc"})
    assert out2["strategy"] == "manual_cpc" and note2 is None  # manual_cpc конверсий не требует


# ── async-обёртка: ассеты/изображения добавляются ПОСЛЕ кампании, сбой не роняет ──
@pytest.mark.asyncio
async def test_apply_create_attaches_assets_after_campaign():
    def fake_sdk(client, customer_id, **kw):
        return {"applied": True, "status": "PAUSED", "campaign": "customers/x/campaigns/77"}

    captured = {}

    def fake_attach(client, customer_id, campaign_id, specs):
        captured["campaign_id"] = campaign_id
        captured["specs"] = specs
        return (["callouts"], [{"family": "lead_form", "reason": "config"}])

    store = _store()
    with (
        patched(mut, "_create_search_campaign_via_sdk", fake_sdk),
        patched(mut, "_attach_asset_specs_via_sdk", fake_attach),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        res = await mut.apply_create_search_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_name="Кения Авто",
            final_url="https://shop.example/",
            headlines=["Поддержанные авто", "Проверенные б/у", "Авто с гарантией"],
            descriptions=["Большой выбор авто с пробегом.", "Гарантия и проверка."],
            budget_daily_micros=40_000_000,
            asset_specs=[{"family": "callouts", "params": {"callouts": ["Гарантия"]}}],
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert captured["campaign_id"] == "77"  # извлечён из resource_name кампании
    assert res["assets_added"] == ["callouts"]
    assert res["assets_skipped"][0]["family"] == "lead_form"
    assert store.finalized is True


@pytest.mark.asyncio
async def test_apply_create_survives_asset_step_failure():
    """B4: кампания УЖЕ создана — сбой пост-шага ассетов (таймаут/квота/гейт самого вызова, а не
    одного ассета) не должен ронять операцию: иначе confirm.store.record_failure пишет в audit_log
    `status='failed'` на реально созданной кампании, а повтор упирается в DUPLICATE_CAMPAIGN_NAME.
    Потеря показывается честно — в assets_skipped/assets_reused."""

    def fake_sdk(client, customer_id, **kw):
        return {"applied": True, "status": "PAUSED", "campaign": "customers/x/campaigns/77"}

    def boom(*a, **k):
        raise TimeoutError("ads call timed out")

    store = _store()
    with (
        patched(mut, "_create_search_campaign_via_sdk", fake_sdk),
        patched(mut, "_attach_asset_specs_via_sdk", boom),
        patched(mut, "_link_existing_assets_via_sdk", boom),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        res = await mut.apply_create_search_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_name="Кения Авто",
            final_url="https://shop.example/",
            headlines=["Поддержанные авто", "Проверенные б/у", "Авто с гарантией"],
            descriptions=["Большой выбор авто с пробегом.", "Гарантия и проверка."],
            budget_daily_micros=40_000_000,
            asset_specs=[{"family": "callouts", "params": {"callouts": ["Гарантия"]}}],
            existing_asset_links=[{"asset_resource_name": "customers/x/assets/1"}],
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True  # кампания создана — операция НЕ провалена
    assert store.finalized is True  # audit_log: applied, а не failed
    assert res["assets_added"] == []
    assert res["assets_skipped"] == [{"family": "callouts", "reason": "TimeoutError"}]
    assert res["assets_reused"] == 0
