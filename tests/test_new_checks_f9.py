"""Ф9: волна новых чеков — устройства, конфликты минусов, value, авто-применение, атрибуция, ротация.

Что пинуем (класс, не случай):
1. ВСЕ шесть чеков ctx-зависимы: без своего kwarg build_audit молчит — золотые матрицы прежних волн
   не сдвигаются, а «нет данных» никогда не читается как «проблема найдена» (GR8, fail-safe).
2. device_performance_gap: (а) 0-конверсионное устройство флажится ТОЛЬКО когда остальные устройства
   кампании конвертят — иначе это проблема кампании (spend_no_conv), не устройства; (б) CPA-ветка при
   живых конверсиях денег в at_risk не кладёт (переплата — в facts); (в) сегмент денег
   device::кампания::устройство — дедуп с кампанией не задваивает.
3. negative_keyword_conflicts: семантика минусов БЕЗ близких вариантов (EXACT — равенство, PHRASE —
   вхождение подряд, BROAD — подмножество токенов) + область действия (кампания / группа / shared по
   карте привязки). Усечённый инвентарь чек НЕ глушит: найденный конфликт — позитивный факт
   (неполнота инвентаря = недосчёт находок, не ложь — в отличие от harvest/ngram).
4. no_conversion_value: молчит без живых действий-конверсий (иначе кричал бы поверх critical-чека
   «трекинга нет») и на малом объёме конверсий (рано судить).
5. attribution_model_last_click: только primary ENABLED; стаб без поля attribution_model → молчание
   (getattr-гейт, обратная совместимость со старыми снапшотами/стабами).
6. auto_apply_recommendations_on: семья recommendations вне FAMILY_WEIGHT — наблюдение, не штраф.
7. ad_rotation_rotate_forever: агрегат (сколько групп + пример), OPTIMIZE не флажится.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reports.queries import (  # noqa: E402
    AdRotationRow,
    Breakdown,
    ConversionActionRow,
    DevicePerformanceRow,
    KeywordInventoryRow,
    Metrics,
    NegativeKeywordRow,
    NegativeKeywordsInfo,
    RecommendationSubscriptionRow,
)

from audit.engine import CHECK_REGISTRY, build_audit  # noqa: E402
from audit.render import finding_text  # noqa: E402
from audit.thresholds import FAMILY_WEIGHT  # noqa: E402

_F9_IDS = {
    "device_performance_gap",
    "negative_keyword_conflicts",
    "no_conversion_value",
    "auto_apply_recommendations_on",
    "attribution_model_last_click",
    "ad_rotation_rotate_forever",
}


def _report(campaigns: list[tuple[str, float, float]], *, conv_value: float = 0.0):
    """campaigns = [(имя, расход, конверсии)]."""
    rows = [
        (
            (name, "ENABLED"),
            Metrics(
                impressions=1000,
                clicks=100,
                cost_micros=int(cost * 1_000_000),
                conversions=conv,
            ),
        )
        for name, cost, conv in campaigns
    ]
    totals = Metrics(
        impressions=1000 * len(campaigns),
        clicks=100 * len(campaigns),
        cost_micros=int(sum(c[1] for c in campaigns) * 1_000_000),
        conversions=sum(c[2] for c in campaigns),
        conv_value=conv_value,
    )
    return SimpleNamespace(
        customer_id="123",
        totals=totals,
        breakdowns=[Breakdown("campaign", "Кампании", ["Кампания", "Статус"], rows)],
        currency="USD",
    )


def _dev(campaign: str, device: str, cost: float, conv: float, clicks: int = 50):
    return DevicePerformanceRow(
        campaign=campaign,
        device=device,
        metrics=Metrics(
            impressions=500, clicks=clicks, cost_micros=int(cost * 1_000_000), conversions=conv
        ),
    )


def _kw(campaign: str, ad_group: str, text: str, match_type: str = "EXACT"):
    return KeywordInventoryRow(
        campaign=campaign, ad_group=ad_group, keyword=text, match_type=match_type, metrics=Metrics()
    )


def _ca(
    name: str = "Lead",
    *,
    status: str = "ENABLED",
    primary: bool = True,
    attribution: str = "GOOGLE_ADS_DATA_DRIVEN",
):
    return ConversionActionRow(
        name=name,
        status=status,
        type="WEBPAGE",
        category="SUBMIT_LEAD_FORM",
        primary_for_goal=primary,
        attribution_model=attribution,
    )


def _ids(res) -> set[str]:
    return {f.check_id for f in res.findings}


def _one(res, check_id: str):
    return next(f for f in res.findings if f.check_id == check_id)


# ── 1. fail-safe: без ctx все шесть молчат (золотые матрицы прежних волн не двигаются) ──


def test_all_f9_checks_silent_without_ctx():
    res = build_audit(
        _report([("Camp", 300.0, 20.0)]),
        conversion_actions=None,  # и attribution, и value гейтятся на действиях
    )
    assert not (_ids(res) & _F9_IDS)


def test_registry_has_all_f9_checks():
    assert CHECK_REGISTRY["device_performance_gap"] == ("bidding", "warning")
    assert CHECK_REGISTRY["negative_keyword_conflicts"] == ("keywords", "warning")
    assert CHECK_REGISTRY["no_conversion_value"] == ("conversion_tracking", "info")
    assert CHECK_REGISTRY["auto_apply_recommendations_on"] == ("recommendations", "info")
    assert CHECK_REGISTRY["attribution_model_last_click"] == ("conversion_tracking", "info")
    assert CHECK_REGISTRY["ad_rotation_rotate_forever"] == ("rsa", "info")
    # Наблюдение, не штраф: recommendations вне вектора весов — авто-применение score не двигает.
    assert "recommendations" not in FAMILY_WEIGHT


# ── 2. device_performance_gap ──


def test_device_zero_conv_fires_only_when_rest_converts():
    res = build_audit(
        _report([("Camp", 150.0, 5.0)]),
        device_performance=[
            _dev("Camp", "MOBILE", 50.0, 0.0),  # жжёт без конверсий…
            _dev("Camp", "DESKTOP", 100.0, 5.0),  # …пока desktop конвертит
        ],
    )
    f = _one(res, "device_performance_gap")
    assert f.facts["reason"] == "zero_conv"
    assert f.at_risk == 50.0
    assert f.spend_segment == "device::Camp::MOBILE"  # ⊂ кампании — дедуп не задвоит
    assert f.suggested_operation is None  # корректировка ставок — деньги, не one-tap


def test_device_silent_when_whole_campaign_does_not_convert():
    # Обе платформы по нулям — это spend_no_conv (кампания), а не разрыв устройства.
    res = build_audit(
        _report([("Camp", 150.0, 0.0)]),
        device_performance=[
            _dev("Camp", "MOBILE", 50.0, 0.0),
            _dev("Camp", "DESKTOP", 100.0, 0.0),
        ],
    )
    assert "device_performance_gap" not in _ids(res)


def test_device_cpa_gap_keeps_money_out_of_at_risk():
    res = build_audit(
        _report([("Camp", 200.0, 11.0)]),
        device_performance=[
            _dev("Camp", "MOBILE", 100.0, 1.0),  # CPA 100
            _dev("Camp", "DESKTOP", 100.0, 10.0),  # CPA 10 → разрыв 10×
        ],
    )
    f = _one(res, "device_performance_gap")
    assert f.facts["reason"] == "cpa_gap"
    assert f.at_risk == 0.0  # конверсии есть — деньги работают
    assert f.facts["overpay"] == 90.0  # 100 − 1×10


def test_device_silent_below_floor_and_single_device():
    res = build_audit(
        _report([("Camp", 30.0, 5.0)]),
        device_performance=[
            _dev("Camp", "MOBILE", 10.0, 0.0),  # ниже device_min_spend=20
            _dev("Camp", "DESKTOP", 20.0, 5.0),
        ],
    )
    assert "device_performance_gap" not in _ids(res)
    res = build_audit(
        _report([("Camp", 100.0, 0.0)]),
        device_performance=[_dev("Camp", "MOBILE", 100.0, 0.0)],  # сравнивать не с чем
    )
    assert "device_performance_gap" not in _ids(res)


# ── 3. negative_keyword_conflicts ──


def _conflict_res(negs, kws, attachments=None, **kw):
    return build_audit(
        _report([("Camp", 100.0, 5.0)]),
        negative_keywords=NegativeKeywordsInfo(
            rows=negs, shared_attachments=dict(attachments or {})
        ),
        keyword_inventory=kws,
        **kw,
    )


def test_negative_exact_blocks_only_equal_text():
    negs = [NegativeKeywordRow("campaign", "Camp", "", "купить ноутбук", "EXACT")]
    res = _conflict_res(negs, [_kw("Camp", "G1", "Купить ноутбук"), _kw("Camp", "G1", "ноутбук")])
    f = _one(res, "negative_keyword_conflicts")
    assert f.facts["blocked_count"] == 1
    assert f.facts["examples"] == ["Купить ноутбук"]
    assert f.at_risk == 0.0  # заблокированный ключ не тратит — он ТЕРЯЕТ трафик


def test_negative_phrase_blocks_consecutive_only():
    negs = [NegativeKeywordRow("campaign", "Camp", "", "ремонт квартир", "PHRASE")]
    res = _conflict_res(
        negs,
        [
            _kw("Camp", "G1", "срочный ремонт квартир москва"),  # подряд — блок
            _kw("Camp", "G1", "ремонт ванных в квартирах"),  # токены врозь — жив
        ],
    )
    assert _one(res, "negative_keyword_conflicts").facts["blocked_count"] == 1


def test_negative_broad_blocks_any_order_subset():
    negs = [NegativeKeywordRow("campaign", "Camp", "", "квартир ремонт", "BROAD")]
    res = _conflict_res(negs, [_kw("Camp", "G1", "ремонт старых квартир")])
    assert _one(res, "negative_keyword_conflicts").facts["blocked_count"] == 1


def test_negative_scopes_respected():
    kws = [_kw("Camp", "G1", "ремонт квартир"), _kw("Other", "G9", "ремонт квартир")]
    # Минус группы G2 не трогает ключ из G1.
    res = _conflict_res([NegativeKeywordRow("ad_group", "Camp", "G2", "ремонт", "BROAD")], kws)
    assert "negative_keyword_conflicts" not in _ids(res)
    # Shared-список бьёт только по ПРИВЯЗАННЫМ кампаниям.
    shared = [NegativeKeywordRow("shared", "", "", "ремонт", "BROAD", list_name="Мусор")]
    res = _conflict_res(shared, kws, attachments={"Мусор": frozenset({"Other"})})
    f = _one(res, "negative_keyword_conflicts")
    assert f.facts["scope"] == "shared"
    assert f.facts["blocked_count"] == 1  # только Other, Camp не привязана
    # Без привязки список ни по кому не бьёт.
    res = _conflict_res(shared, kws, attachments={})
    assert "negative_keyword_conflicts" not in _ids(res)


def test_negative_conflicts_not_muted_by_truncated_inventory():
    # В отличие от harvest/ngram: конфликт — позитивный факт, полнота инвентаря его не отменяет.
    negs = [NegativeKeywordRow("campaign", "Camp", "", "ноутбук", "BROAD")]
    res = _conflict_res(
        negs, [_kw("Camp", "G1", "купить ноутбук")], keyword_inventory_truncated=True
    )
    assert "negative_keyword_conflicts" in _ids(res)


def test_negative_conflicts_silent_without_either_side():
    res = build_audit(
        _report([("Camp", 100.0, 5.0)]),
        negative_keywords=NegativeKeywordsInfo(
            rows=[NegativeKeywordRow("campaign", "Camp", "", "ноутбук", "BROAD")]
        ),
        keyword_inventory=None,  # инвентарь не прочитан — молчим, не гадаем
    )
    assert "negative_keyword_conflicts" not in _ids(res)


# ── 4. no_conversion_value ──


def test_no_conversion_value_fires_and_gates():
    fires = build_audit(
        _report([("Camp", 300.0, 15.0)], conv_value=0.0), conversion_actions=[_ca()]
    )
    assert "no_conversion_value" in _ids(fires)
    # value есть → молчим; мало конверсий → рано судить; нет ENABLED действий → кричит другой чек.
    ok = build_audit(_report([("Camp", 300.0, 15.0)], conv_value=900.0), conversion_actions=[_ca()])
    assert "no_conversion_value" not in _ids(ok)
    few = build_audit(_report([("Camp", 300.0, 3.0)], conv_value=0.0), conversion_actions=[_ca()])
    assert "no_conversion_value" not in _ids(few)
    dead = build_audit(
        _report([("Camp", 300.0, 15.0)], conv_value=0.0),
        conversion_actions=[_ca(status="REMOVED")],
    )
    assert "no_conversion_value" not in _ids(dead)


# ── 5. auto_apply_recommendations_on ──


def test_auto_apply_fires_on_enabled_subscriptions_only():
    res = build_audit(
        _report([("Camp", 100.0, 5.0)]),
        recommendation_subscriptions=[
            RecommendationSubscriptionRow("ENHANCED_CPC_OPT_IN", "ENABLED"),
            RecommendationSubscriptionRow("KEYWORD", "ENABLED"),
            RecommendationSubscriptionRow("TEXT_AD", "PAUSED"),  # выключена — не считаем
        ],
    )
    f = _one(res, "auto_apply_recommendations_on")
    assert f.facts["count"] == 2
    assert f.at_risk == 0.0
    silent = build_audit(
        _report([("Camp", 100.0, 5.0)]),
        recommendation_subscriptions=[RecommendationSubscriptionRow("KEYWORD", "PAUSED")],
    )
    assert "auto_apply_recommendations_on" not in _ids(silent)


# ── 6. attribution_model_last_click ──


def test_attribution_last_click_only_primary_enabled():
    res = build_audit(
        _report([("Camp", 100.0, 5.0)]),
        conversion_actions=[
            _ca("Лид", attribution="GOOGLE_ADS_LAST_CLICK"),
            _ca("Звонок", attribution="GOOGLE_ADS_LAST_CLICK", primary=False),  # вспомогательное
            _ca("Продажа"),  # data-driven — норма
            _ca("Старый", attribution="GOOGLE_ADS_LAST_CLICK", status="REMOVED"),
        ],
    )
    f = _one(res, "attribution_model_last_click")
    assert f.facts["count"] == 1
    assert f.facts["names"] == ["Лид"]


def test_attribution_silent_on_legacy_stub_without_field():
    # Стаб без attribution_model (старые тесты/снапшоты) → getattr-гейт молчит, не гадает.
    legacy = SimpleNamespace(name="Лид", status="ENABLED", primary_for_goal=True)
    res = build_audit(_report([("Camp", 100.0, 5.0)]), conversion_actions=[legacy])
    assert "attribution_model_last_click" not in _ids(res)


# ── 7. ad_rotation_rotate_forever ──


def test_ad_rotation_aggregates_and_ignores_optimize():
    res = build_audit(
        _report([("Camp", 100.0, 5.0)]),
        ad_rotation=[
            AdRotationRow("Camp", "G1", "ROTATE_FOREVER"),
            AdRotationRow("Camp", "G2", "ROTATE_FOREVER"),
            AdRotationRow("Camp", "G3", "OPTIMIZE"),
        ],
    )
    f = _one(res, "ad_rotation_rotate_forever")
    assert f.facts["count"] == 2
    assert f.facts["ad_group"] == "G1"
    silent = build_audit(
        _report([("Camp", 100.0, 5.0)]), ad_rotation=[AdRotationRow("Camp", "G1", "OPTIMIZE")]
    )
    assert "ad_rotation_rotate_forever" not in _ids(silent)


# ── 8. проза: RU/EN есть у каждого нового чека (нет отвала в сырой check_id) ──


def test_f9_findings_have_prose_in_both_languages():
    res = build_audit(
        _report([("Camp", 350.0, 16.0)], conv_value=0.0),
        conversion_actions=[_ca("Лид", attribution="GOOGLE_ADS_LAST_CLICK")],
        device_performance=[
            _dev("Camp", "MOBILE", 50.0, 0.0),
            _dev("Camp", "DESKTOP", 100.0, 5.0),
        ],
        negative_keywords=NegativeKeywordsInfo(
            rows=[NegativeKeywordRow("campaign", "Camp", "", "ноутбук", "BROAD")]
        ),
        keyword_inventory=[_kw("Camp", "G1", "купить ноутбук")],
        recommendation_subscriptions=[RecommendationSubscriptionRow("KEYWORD", "ENABLED")],
        ad_rotation=[AdRotationRow("Camp", "G1", "ROTATE_FOREVER")],
    )
    got = _ids(res) & _F9_IDS
    assert got == _F9_IDS  # все шесть сработали на одном отчёте
    for cid in sorted(_F9_IDS):
        f = _one(res, cid)
        ru, en = finding_text(f, "ru", "USD"), finding_text(f, "en", "USD")
        # Проза есть (не отвал в `camp or check_id`) и языки не перепутаны.
        assert ru not in (cid, "Camp") and en not in (cid, "Camp"), cid
        assert ru != en, cid
