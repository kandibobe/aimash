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


class FakeProposal:
    def __init__(self):
        self.operation = "create_search_campaign"
        self.status = "confirmed"
        self.user_initiated = True


class FakeStore:
    def __init__(self):
        self.finalized = False

    async def claim(self, cid, *, operation):
        return FakeProposal()

    async def finalize(self, cid, *, result):
        self.finalized = True
        self.result = result


# ── _resolve_language_ids: имя/код → languageConstant id, неизвестное пропускаем ──
def test_resolve_language_ids():
    assert mut._resolve_language_ids(["English", "ru", "Swahili"]) == [1000, 1031]
    assert mut._resolve_language_ids([]) == []


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
    for gated in ("location", "affiliate_location", "lead_form"):
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
        if fam == "lead_form":
            raise NotImplementedError("требует privacy policy")
        raise RuntimeError("boom")

    with patched(ext, "apply_asset_spec_via_sdk", fake_dispatch):
        added, skipped = mut._attach_asset_specs_via_sdk(
            object(),
            DRAFT_ACCOUNT_ID,
            "123",
            [
                {"family": "callouts", "params": {}},
                {"family": "lead_form", "params": {}},
                {"family": "price", "params": {}},
            ],
        )
    assert added == ["callouts"]
    assert {s["family"] for s in skipped} == {"lead_form", "price"}


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

    store = FakeStore()
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
