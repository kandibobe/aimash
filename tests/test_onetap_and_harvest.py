"""Ревизия волны (2026-07-14): «быстрые победы» стали настоящей одной кнопкой, а «сбор урожая» —
обратным ходом к «🚫 в минус».

Что тут стережём (это денежный периметр, а не UI-сахар):
- ONE_TAP_OPS никогда не пересекается с MONEY_OPS (golden rule #3: бюджет/ставка — только прямой
  командой человека). Добавили в one-tap две новые операции — тест обязан ловить любую денежную;
- направление one-tap ЗАШИТО в код (КМС выключаем, гео сужаем): кнопка «применить совет» не должна
  уметь РАСШИРИТЬ показы — иначе она станет способом потратить чужие деньги в один тап;
- add_keywords, сужённый до группы: имя группы не совпало ⇒ ОТКАЗ, а не тихий веер по всем группам
  кампании (это была бы та самая каннибализация, которую флажит сам аудит);
- сбор урожая молчит на усечённом/непрочитанном инвентаре ключей (GR8: «нет данных» ≠ «ноль»).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from ads.resolve import MONEY_OPS  # noqa: E402
from ads.service import SUPPORTED_OPERATIONS  # noqa: E402
from audit.engine import ONE_TAP_OPS  # noqa: E402
from bot.keyboards import ADVISE_APPLY_OPS  # noqa: E402


# ── One-tap: инварианты денег ────────────────────────────────────────────────────
def test_one_tap_ops_never_touch_money():
    """Golden rule #3. Любая денежная операция в one-tap = способ потратить чужие деньги кликом."""
    assert not (ONE_TAP_OPS & set(MONEY_OPS)), (
        f"денежные операции в ONE_TAP_OPS: {sorted(ONE_TAP_OPS & set(MONEY_OPS))} — "
        "бюджет/ставка меняются ТОЛЬКО прямой командой пользователя"
    )
    assert ONE_TAP_OPS <= set(
        SUPPORTED_OPERATIONS
    )  # кнопка на неподдержанную op = падение ПОСЛЕ ✅
    # Движок и бот обязаны считать one-tap одинаково: разъезд = кнопка, которая ничего не делает,
    # либо находка, которая молча теряет кнопку.
    assert ONE_TAP_OPS == set(ADVISE_APPLY_OPS)


def test_one_tap_params_are_pinned_to_the_safe_direction():
    """Направление чинки — КОНСТАНТА в коде, а не поле находки. Даже если рекомендация в БД будет
    испорчена (или подсунута), кнопка может только ВЫКЛЮЧИТЬ КМС и СУЗИТЬ гео до PRESENCE."""
    rec = SimpleNamespace(
        target_campaign="Бренд",
        suggested_operation="set_campaign_display_network",
        evidence={"display_network": True, "geo_target_type": "PRESENCE_OR_INTEREST"},
    )
    assert bm._advise_apply_params(rec) == {"campaign": "Бренд", "display_network": False}

    rec.suggested_operation = "set_campaign_geo_target_type"
    assert bm._advise_apply_params(rec) == {"campaign": "Бренд", "geo_target_type": "PRESENCE"}

    # Нет кампании → нечего адресовать: черновик не минтим (не гадаем).
    rec.target_campaign = None
    assert bm._advise_apply_params(rec) is None


def test_geo_and_display_checks_carry_a_one_tap_operation():
    """Ф6.2 нашла G11/G12, но чинить их приходилось руками через карточку кампании. Прогоняем оба
    чека на синтетике, где условие срабатывания выполнено, и проверяем: находка несёт
    suggested_operation ∈ ONE_TAP_OPS — иначе блок «⚡ быстрые победы» снова пуст."""
    from audit.engine import _Ctx, check_display_on_search_campaign, check_geo_interest_waste

    report = SimpleNamespace(currency="USD", totals=SimpleNamespace(cost=1000.0))
    # КМС на поиске: расход по CONTENT есть, конверсий с него нет.
    disp_row = SimpleNamespace(
        campaign="К",
        channel_type="SEARCH",
        content_network=True,
        content_cost=80.0,
        content_conversions=0.0,
    )
    # Гео «присутствие ИЛИ интерес»: расход вне таргета есть, конверсий вне таргета нет.
    geo_row = SimpleNamespace(
        campaign="К",
        geo_target_type="PRESENCE_OR_INTEREST",
        outside_cost=120.0,
        outside_conversions=0.0,
    )
    ctx = _Ctx(campaign_settings=[disp_row])
    disp = check_display_on_search_campaign(report, {"content_on_search_min_spend": 5.0}, ctx)
    assert disp and disp[0].suggested_operation == "set_campaign_display_network"
    assert disp[0].one_tap

    ctx = _Ctx(campaign_settings=[geo_row])
    geo = check_geo_interest_waste(report, {"geo_interest_min_spend": 20.0}, ctx)
    assert geo and geo[0].suggested_operation == "set_campaign_geo_target_type"
    assert geo[0].one_tap


# ── add_keywords, сужённый до одной группы ───────────────────────────────────────
async def test_add_keywords_narrows_to_the_named_ad_group():
    """Ф4: собранный запрос кладём в ТУ группу, где он крутился. Без сужения ключ уходил бы во ВСЕ
    группы кампании — сами бы и сделали каннибализацию, которую флажит check_keyword_cannibalization."""
    import ads.service as svc

    seen: dict = {}

    async def _fake_apply(**kw):
        seen.update(kw)
        return {"applied": True, "count": 1}

    groups = [
        SimpleNamespace(id="11", name="Бренд"),
        SimpleNamespace(id="22", name="Общие"),
        SimpleNamespace(id="33", name="Гео"),
    ]
    p = SimpleNamespace(
        operation="add_keywords",
        status="confirmed",
        customer_id="7753643025",
        params={
            "campaign": "К",
            "ad_group": "Общие",
            "keywords": ["купить окна"],
            "match_type": "exact",
        },
    )

    class _S:
        async def get_confirmed(self, cid):
            return p

    import pytest

    from core.config import settings

    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = "7753643025"
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(svc, "build_client_async", lambda cid: _async(SimpleNamespace()))
        monkey.setattr(svc.resolve, "find_ad_groups", lambda *a, **k: groups)
        monkey.setattr(svc.mutations, "apply_add_keywords", _fake_apply)
        await svc.execute_confirmed(_S(), "cid")
        assert seen["ad_group_ids"] == ["22"], "ключ ушёл не в ту группу (или во все сразу)"

        # Имя группы не найдено → ОТКАЗ (fail-closed), а не веер по всем группам.
        p.params["ad_group"] = "Такой группы нет"
        try:
            await svc.execute_confirmed(_S(), "cid")
            raise AssertionError("ожидался ValueError: группы нет в кампании")
        except ValueError as e:
            assert "нет группы" in str(e)
    finally:
        monkey.undo()
        settings.google_ads_allowed_customer_ids = prev


async def _async(v):
    return v


def test_add_keywords_without_ad_group_still_covers_whole_campaign():
    """Совместимость: старые черновики (и агент, не указавший группу) по-прежнему кладут ключи во
    все группы кампании — сужение ОПЦИОНАЛЬНО, а не обязательно."""
    from agent.tools.schemas import SCHEMAS

    params = SCHEMAS["add_keywords"](
        campaign="К", keywords=["окна"], match_type="exact"
    ).model_dump()
    assert params["ad_group"] is None


# ── Сбор урожая: молчит на неполных данных (GR8) ─────────────────────────────────
def _term(text, camp, ag, conv, cost=10.0, clicks=5):
    return SimpleNamespace(
        search_term=text,
        campaign=camp,
        ad_group=ag,
        metrics=SimpleNamespace(conversions=conv, cost=cost, clicks=clicks),
    )


async def _harvest(monkeypatch, *, inventory, rows=None):
    """Прогнать bm._searchterms_harvest с подставленным инвентарём. inventory=None ⇒ чтение упало."""
    rows = rows if rows is not None else [_term("купить окна цена", "К", "Общие", 2.0)]

    async def _read(fn, client, acct, period, label=""):
        if inventory is None:
            raise RuntimeError("read failed")
        return inventory

    monkeypatch.setattr(bm, "run_ads_read_call", _read)
    return await bm._searchterms_harvest(object(), "7753643025", None, rows)


async def test_harvest_offers_converting_term_without_a_keyword(monkeypatch):
    inv = [SimpleNamespace(keyword="окна")]  # «купить окна цена» — своего ключа НЕТ
    out = await _harvest(monkeypatch, inventory=inv)
    assert [(i["term"], i["campaign"], i["ad_group"]) for i in out] == [
        ("купить окна цена", "К", "Общие")
    ]
    assert out[0]["conversions"] == 2.0


async def test_harvest_silent_when_the_term_is_already_a_keyword(monkeypatch):
    inv = [SimpleNamespace(keyword="купить окна цена")]
    assert await _harvest(monkeypatch, inventory=inv) == []


async def test_harvest_silent_when_inventory_is_truncated(monkeypatch):
    """Инвентарь тут — ОТРИЦАТЕЛЬНЫЙ фильтр («чего у меня нет»). Обрезали список ⇒ предложим собрать
    уже собранное. Нет данных ≠ ноль (GR8) — молчим."""
    from reports.queries import KEYWORD_INVENTORY_LIMIT

    inv = [SimpleNamespace(keyword=f"ключ {i}") for i in range(KEYWORD_INVENTORY_LIMIT)]
    assert await _harvest(monkeypatch, inventory=inv) == []


async def test_harvest_silent_when_inventory_read_fails(monkeypatch):
    """Сбой чтения инвентаря не должен превращаться в «у вас нет таких ключей» (fail-closed)."""
    assert await _harvest(monkeypatch, inventory=None) == []
