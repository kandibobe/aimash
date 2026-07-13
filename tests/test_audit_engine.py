"""Юнит-тесты движка аудита — чистые, без SDK/сети (образец tests/test_advisor.py).

Пинуют health-score константами (крит.C1): правка весов/порогов ломает golden-fixture, а не молча
сдвигает всем grade. Проверяют дедуп денег-под-риском (крит.C2), пустой аккаунт («—», не 100),
границы буквы и инвариант «audit/ не импортирует ads.mutations/ads.service».
"""

from __future__ import annotations

import ast
import pathlib
import re
from types import SimpleNamespace

import pytest

from reports.queries import Breakdown, Metrics

from audit.engine import (
    CHECK_IDS,
    CHECK_REGISTRY,
    SCORE_MODEL_VERSION,
    Finding,
    _dedup_at_risk,
    build_audit,
    compute_score_model_version,
)
from audit.thresholds import (
    DEFAULT_AUDIT_THRESHOLDS,
    FAMILY_WEIGHT,
    GRADE_BANDS,
    NONMONEY_INTENSITY,
    SEVERITY_MULT,
    grade_for,
)


def _report(totals: Metrics, campaign_rows=(), keyword_rows=(), ad_rows=(), currency="USD"):
    """ReportData-подобный объект (duck-typed, как в test_advisor)."""
    bds = [Breakdown("campaign", "Кампании", ["Кампания", "Статус"], list(campaign_rows))]
    if keyword_rows:
        bds.append(
            Breakdown("keyword", "Ключи", ["Кампания", "Группа", "Ключ", "Тип"], list(keyword_rows))
        )
    if ad_rows:
        bds.append(
            Breakdown("ad", "Объявления", ["Кампания", "Группа", "ID", "Тип"], list(ad_rows))
        )
    return SimpleNamespace(customer_id="123", totals=totals, breakdowns=bds, currency=currency)


def test_golden_score_and_grade():
    """Один waste-финдинг на 50% расхода → штраф 30×0.5=15 → score 85 (B), at_risk 500.
    ЖЁСТКО пинует модель score: смена веса waste (30) или SEVERITY_MULT сразу ломает тест (C1)."""
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [
        (
            ("Brand", "ENABLED"),
            Metrics(impressions=1000, clicks=100, cost_micros=500_000_000, conversions=0),
        ),
        (
            ("Generic", "ENABLED"),
            Metrics(impressions=2000, clicks=200, cost_micros=500_000_000, conversions=5),
        ),
    ]
    res = build_audit(_report(totals, rows))
    assert res.score == 85
    assert res.grade == "B"
    assert res.at_risk == 500.0
    assert res.total_spend == 1000.0
    assert res.has_activity is True
    # ровно одна находка waste (Brand); Generic cpa 100 < 2×200 → без high_cpa
    kinds = [f.check_id for f in res.findings]
    assert kinds == ["spend_no_conv"]
    assert res.findings[0].one_tap is True  # pause_campaign применим в один тап


def test_empty_account_scores_dash_not_100():
    res = build_audit(_report(Metrics(), []))
    assert res.score is None
    assert res.grade == "—"
    assert res.at_risk == 0.0
    assert res.has_activity is False
    assert res.findings == []


def test_at_risk_dedup_takes_max_per_segment_not_sum():
    """Две находки на одном (кампания, сегмент) → MAX, не сумма (крит.C2)."""
    fs = [
        Finding(
            "a", "waste", "warning", at_risk=500.0, spend_segment="Brand", target_campaign="Brand"
        ),
        Finding(
            "b", "waste", "warning", at_risk=300.0, spend_segment="Brand", target_campaign="Brand"
        ),
    ]
    assert _dedup_at_risk(fs, total_spend=1000.0) == 500.0


def test_at_risk_capped_by_spend():
    """Сумма денег-под-риском НИКОГДА не превышает расход (гарантирует КОД, не утверждение)."""
    fs = [
        Finding("a", "waste", "warning", at_risk=800.0, spend_segment="X", target_campaign="A"),
        Finding("b", "keywords", "warning", at_risk=800.0, spend_segment="Y", target_campaign="B"),
    ]
    assert _dedup_at_risk(fs, total_spend=1000.0) == 1000.0


def test_kill_rule_noop_without_target_but_fires_with_target():
    """«3× Kill» молчит без цели (не фабрикует «×цель») и срабатывает при заданной target_cpa."""
    totals = Metrics(
        impressions=1000, clicks=100, cost_micros=1_000_000_000, conversions=2
    )  # acct_cpa 500
    rows = [
        (
            ("Expensive", "ENABLED"),
            Metrics(impressions=1000, clicks=100, cost_micros=1_000_000_000, conversions=2),
        ),
    ]  # cpa = 500
    # без цели: kill_rule не запускается (high_cpa тоже молчит — acct_cpa == cpa, не ≥2×)
    res0 = build_audit(_report(totals, rows))
    assert "kill_rule" not in [f.check_id for f in res0.findings]
    # с целью 100: cpa 500 ≥ 3×100 → kill_rule, one-tap pause
    res1 = build_audit(_report(totals, rows), target_cpa=100.0)
    kill = [f for f in res1.findings if f.check_id == "kill_rule"]
    assert len(kill) == 1
    assert kill[0].one_tap is True
    assert kill[0].at_risk == 800.0  # cost 1000 − conv 2 × target 100 = 800


def test_no_conversion_tracking_flags_whole_spend():
    """Расход есть, клики есть, 0 конверсий по аккаунту → красная семья, весь расход под риском."""
    totals = Metrics(
        impressions=5000, clicks=400, cost_micros=200_000_000, conversions=0
    )  # $200, 0 conv
    res = build_audit(_report(totals, []))
    fams = res.families
    assert "conversion_tracking" in fams
    # весь расход под риском (capped by spend)
    assert res.at_risk == 200.0


def test_grade_bands():
    assert grade_for(100) == "A"
    assert grade_for(90) == "A"
    assert grade_for(89.9) == "B"
    assert grade_for(65) == "C"
    assert grade_for(50) == "D"
    assert grade_for(49.9) == "F"
    assert grade_for(0) == "F"
    assert grade_for(None) == "—"


def _is(name, search, budget, rank, channel="SEARCH"):
    return SimpleNamespace(
        campaign_name=name,
        channel_type=channel,
        search_is=search,
        budget_lost_is=budget,
        rank_lost_is=rank,
    )


def test_impression_share_budget_vs_rank_and_nodata():
    """IS: budget-lost>rank → budget_constrained (budget); rank>budget → rank_constrained (rsa);
    Σ долей ≠ 1.0 (proto3-zero) → молчим (нет данных)."""
    totals = Metrics(impressions=1000, clicks=100, cost_micros=100_000000, conversions=1)
    rows = [
        (
            ("A", "ENABLED"),
            Metrics(impressions=1000, clicks=100, cost_micros=100_000000, conversions=1),
        )
    ]
    is_rows = [
        _is("Budget-Camp", 0.5, 0.4, 0.1),  # budget-constrained
        _is("Rank-Camp", 0.55, 0.05, 0.4),  # rank-constrained
        _is("NoData-Camp", 0.0, 0.0, 0.0),  # Σ=0 → нет данных, молчим
    ]
    res = build_audit(_report(totals, rows), is_rows=is_rows)
    ids = {f.check_id: f for f in res.findings}
    assert ids["is_budget_constrained"].target_campaign == "Budget-Camp"
    assert ids["is_budget_constrained"].family == "budget"
    assert ids["is_rank_constrained"].target_campaign == "Rank-Camp"
    assert ids["is_rank_constrained"].family == "rsa"
    assert not any(f.target_campaign == "NoData-Camp" for f in res.findings)


def test_is_lost_revenue_estimate_scores_via_intensity_not_at_risk():
    """C: кампания С конверсиями упирается в бюджет → ОЦЕНКА упущенной выручки. Вклад в score через
    score_intensity (не at_risk — инвариант at_risk ≤ расход и headline не задваиваются). lost_opportunity
    показывается отдельным числом. Без конверсий / ненадёжного search_is → info is_budget_constrained."""
    totals = Metrics(
        impressions=2000, clicks=200, cost_micros=1_000_000_000, conversions=10
    )  # acct_cpa 100
    rows = [
        _campaign("Constrained", 500, 100, 5),  # конвертит → есть способность
        _campaign("Fine", 500, 100, 5),
    ]
    # search_is 0.5, budget-lost 0.4 → lost_conv = 5×0.4/0.5 = 4; value=acct_cpa 100 → lost_rev=400
    res = build_audit(_report(totals, rows), is_rows=[_is("Constrained", 0.5, 0.4, 0.1)])
    f = next(f for f in res.findings if f.check_id == "is_lost_revenue")
    assert f.facts["lost_conv"] == 4.0 and f.facts["lost_revenue"] == 400.0
    assert f.at_risk == 0.0  # инвариант: упущенное НЕ в at_risk
    assert res.at_risk == 0.0 and res.lost_opportunity == 400.0
    assert f.severity == "warning" and f.one_tap is False
    # score среагировал (budget вес 10, intensity 400/1000=0.4 → штраф 4 → 96), но БЕЗ is_rows — 100
    assert res.score == 96
    assert build_audit(_report(totals, rows)).score == 100
    # search_is=0 (Σ≠1) → нет данных, молчим; кампания без конверсий → info, не оценка
    quiet = build_audit(_report(totals, rows), is_rows=[_is("Constrained", 0.0, 0.0, 0.0)])
    assert not any(f.check_id == "is_lost_revenue" for f in quiet.findings)
    solo = [_campaign("NoConv", 1000, 100, 0)]
    noconv = build_audit(
        _report(
            Metrics(impressions=2000, clicks=100, cost_micros=1_000_000_000, conversions=0), solo
        ),
        is_rows=[_is("NoConv", 0.5, 0.4, 0.1)],
    )
    assert any(f.check_id == "is_budget_constrained" for f in noconv.findings)
    assert not any(f.check_id == "is_lost_revenue" for f in noconv.findings)


def test_structure_checks_bloat_thin_and_no_negatives():
    """D: adgroup_bloat (>20 ключей), rsa_thin (<2 RSA), no_negative_list (0 списков при трафике).
    Все info/неденежные/не one-tap; нет данных структуры/списков → молчим (fail-safe)."""
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [_campaign("Main", 1000, 300, 5)]  # конвертит → без spend_no_conv, но трафик есть
    ag = [
        SimpleNamespace(campaign="Main", ad_group="Bloated", kw_count=35, rsa_count=1),
        SimpleNamespace(campaign="Main", ad_group="Ok", kw_count=8, rsa_count=3),
    ]
    res = build_audit(
        _report(totals, rows),
        adgroup_structure=ag,
        negative_lists=SimpleNamespace(count=0, attached_campaigns=0),
    )
    ids = {f.check_id: f for f in res.findings}
    assert (
        ids["adgroup_bloat"].facts["worst_kw"] == 35 and ids["adgroup_bloat"].family == "structure"
    )
    assert ids["rsa_thin"].facts["worst_rsa"] == 1 and ids["rsa_thin"].family == "rsa"
    assert ids["no_negative_list"].family == "keywords"
    for cid in ("adgroup_bloat", "rsa_thin", "no_negative_list"):
        assert ids[cid].severity == "info" and ids[cid].at_risk == 0.0 and ids[cid].one_tap is False
    # нет данных структуры/списков → молчим (fail-safe)
    quiet = build_audit(_report(totals, rows))
    assert not any(
        f.check_id in ("adgroup_bloat", "rsa_thin", "no_negative_list") for f in quiet.findings
    )
    # список минус-слов есть → no_negative_list молчит
    ok = build_audit(
        _report(totals, rows), negative_lists=SimpleNamespace(count=2, attached_campaigns=1)
    )
    assert not any(f.check_id == "no_negative_list" for f in ok.findings)


def _kq(kw, qs, ctr, rel, lp, cost=100.0, campaign="Main"):
    return SimpleNamespace(
        campaign=campaign,
        ad_group="grp",
        keyword=kw,
        quality_score=qs,
        expected_ctr=ctr,
        ad_relevance=rel,
        landing_page=lp,
        metrics=Metrics(
            impressions=500, clicks=50, cost_micros=int(cost * 1_000_000), conversions=1
        ),
    )


def test_quality_score_low_and_component_decomposition():
    """B: qs_low (QS ≤ 4 при расходе); компоненты qs_ctr/relevance/landing_below (доля «ниже среднего»
    ≥ 35%). proto3-zero QS не учитывается; мелкий расход отсекается; нет данных → молчим. Всё info/rsa."""
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [_campaign("Main", 1000, 300, 5)]
    kq = [
        _kq("cheap junk", 2, "BELOW_AVERAGE", "BELOW_AVERAGE", "AVERAGE"),
        _kq("weak", 4, "BELOW_AVERAGE", "AVERAGE", "AVERAGE"),
        _kq("good", 8, "ABOVE_AVERAGE", "ABOVE_AVERAGE", "ABOVE_AVERAGE"),
        _kq("nodata", 0, "AVERAGE", "AVERAGE", "AVERAGE"),  # proto3-zero → не учитываем
        _kq(
            "tiny", 1, "BELOW_AVERAGE", "BELOW_AVERAGE", "BELOW_AVERAGE", cost=0.5
        ),  # мелочь → отсекаем
    ]
    res = build_audit(_report(totals, rows), keyword_quality=kq)
    ids = {f.check_id: f for f in res.findings}
    # учтено 3 ключа (QS>0 и cost≥3): cheap(2), weak(4), good(8)
    assert ids["qs_low"].facts["count"] == 2 and ids["qs_low"].facts["worst_qs"] == 2
    # CTR below: cheap+weak = 2/3 = 67% ≥ 35% → флаг
    assert "qs_ctr_below" in ids and ids["qs_ctr_below"].facts["share"] == 67
    # relevance below: только cheap = 1/3 = 33% < 35% → нет флага
    assert "qs_relevance_below" not in ids
    for cid in ("qs_low", "qs_ctr_below"):
        assert ids[cid].family == "rsa" and ids[cid].severity == "info" and ids[cid].at_risk == 0.0
    # нет данных QS → молчим (fail-safe)
    quiet = build_audit(_report(totals, rows))
    assert not any(f.check_id.startswith("qs_") for f in quiet.findings)


def test_manual_bid_high_vol_flags_manual_at_scale():
    """Bidding (G40): ENABLED-кампания на ручной стратегии с >30 конв → совет Smart Bidding. Smart-
    стратегия / мало конверсий / нет данных о стратегии → молчим. info/неденежное/не one-tap."""
    totals = Metrics(impressions=5000, clicks=500, cost_micros=1_000_000_000, conversions=50)
    rows = [
        _campaign("ManualBig", 600, 300, 40),  # ручная, 40 конв → флаг
        _campaign("ManualSmall", 200, 100, 10),  # ручная, 10 конв → нет (≤30)
        _campaign("Smart", 200, 100, 40),  # Smart → нет, несмотря на объём
    ]
    bidding = [
        SimpleNamespace(name="ManualBig", strategy_type="MANUAL_CPC", target_cpa=None),
        SimpleNamespace(name="ManualSmall", strategy_type="MANUAL_CPC", target_cpa=None),
        SimpleNamespace(name="Smart", strategy_type="MAXIMIZE_CONVERSIONS", target_cpa=None),
    ]
    res = build_audit(_report(totals, rows), bidding=bidding)
    mb = [f for f in res.findings if f.check_id == "manual_bid_high_vol"]
    assert len(mb) == 1 and mb[0].target_campaign == "ManualBig"
    assert mb[0].family == "bidding" and mb[0].severity == "info"
    assert mb[0].at_risk == 0.0 and mb[0].one_tap is False
    # нет данных о стратегии → молчим (fail-safe)
    assert not any(
        f.check_id == "manual_bid_high_vol" for f in build_audit(_report(totals, rows)).findings
    )


def _geo(campaign, region, cost, clicks, conv):
    return SimpleNamespace(
        campaign=campaign,
        region=region,
        metrics=Metrics(
            impressions=1000, clicks=clicks, cost_micros=int(cost * 1_000_000), conversions=conv
        ),
    )


def _cell(campaign, day, hour, cost, clicks, conv):
    return SimpleNamespace(
        campaign=campaign,
        day_of_week=day,
        hour=hour,
        metrics=Metrics(
            impressions=500, clicks=clicks, cost_micros=int(cost * 1_000_000), conversions=conv
        ),
    )


def test_geo_no_conv_money_dedup_and_schedule_waste():
    """A: geo_no_conv — денежная (at_risk=cost, семья geo), дедуп cost региона ⊂ cost кампании (не
    задваивает spend_no_conv). schedule_waste — info/at_risk=0. Нет данных → молчим (fail-safe)."""
    totals = Metrics(impressions=4000, clicks=400, cost_micros=1_000_000_000, conversions=8)
    rows = [
        _campaign("Search", 500, 200, 8),
        _campaign("Other", 500, 200, 0),
    ]  # Other → spend_no_conv 500
    geo = [
        _geo("Search", "Kyiv", 60, 100, 0),  # $60, 0 конв → флаг
        _geo("Search", "Lviv", 10, 50, 0),  # $10 < 20 → нет
        _geo("Search", "Odesa", 40, 80, 3),  # конвертит → нет
    ]
    res = build_audit(_report(totals, rows), geo_waste=geo)
    g = [f for f in res.findings if f.check_id == "geo_no_conv"]
    assert len(g) == 1 and g[0].facts["region"] == "Kyiv" and g[0].at_risk == 60.0
    assert g[0].family == "geo" and g[0].severity == "warning" and g[0].one_tap is False
    assert g[0].spend_segment == "geo::Search::Kyiv"
    # дедуп: geo Kyiv ($60) ⊂ Search ($500, конвертит) + Other spend_no_conv ($500) = 560 ≤ total 1000
    assert res.at_risk == 560.0
    assert not any(f.check_id == "geo_no_conv" for f in build_audit(_report(totals, rows)).findings)
    # schedule_waste
    sched = [
        _cell("Search", "MONDAY", 3, 25, 30, 0),  # $25, 0 конв → флаг
        _cell("Search", "TUESDAY", 14, 40, 50, 2),  # конвертит → нет
    ]
    res2 = build_audit(_report(totals, rows), schedule=sched)
    sw = [f for f in res2.findings if f.check_id == "schedule_waste"]
    assert len(sw) == 1 and sw[0].at_risk == 0.0
    assert sw[0].family == "geo" and sw[0].severity == "info"
    assert sw[0].facts["worst_hour"] == 3 and sw[0].facts["count"] == 1


def test_conversion_tracking_with_live_actions():
    """С живыми conversion_action: нет активных → no_conversion_tracking; есть, но 0 конв → zero_conversions;
    есть и конверсии есть → тихо."""
    totals0 = Metrics(impressions=5000, clicks=400, cost_micros=200_000000, conversions=0)
    # нет активных действий-конверсий
    res_none = build_audit(_report(totals0, []), conversion_actions=[])
    assert any(f.check_id == "no_conversion_tracking" for f in res_none.findings)
    # есть активное действие, но 0 конверсий при кликах → zero_conversions
    action = SimpleNamespace(status="ENABLED", primary_for_goal=True, name="Purchase")
    res_zero = build_audit(_report(totals0, []), conversion_actions=[action])
    assert any(f.check_id == "zero_conversions" for f in res_zero.findings)
    assert not any(f.check_id == "no_conversion_tracking" for f in res_zero.findings)
    # есть действие и есть конверсии → семьи conversion_tracking нет
    totals_ok = Metrics(impressions=5000, clicks=400, cost_micros=200_000000, conversions=8)
    res_ok = build_audit(_report(totals_ok, []), conversion_actions=[action])
    assert "conversion_tracking" not in res_ok.families


def test_kill_rule_from_per_campaign_bidding_target():
    """Цель CPA из пер-кампанийной стратегии (ctx.bidding) запускает 3×-Kill даже без глобального /target."""
    totals = Metrics(
        impressions=1000, clicks=100, cost_micros=1_000_000000, conversions=2
    )  # acct_cpa 500
    rows = [
        (
            ("Expensive", "ENABLED"),
            Metrics(impressions=1000, clicks=100, cost_micros=1_000_000000, conversions=2),
        )
    ]
    bidding = [SimpleNamespace(name="Expensive", target_cpa=100.0)]  # cpa 500 ≥ 3×100
    res = build_audit(_report(totals, rows), bidding=bidding)
    kill = [f for f in res.findings if f.check_id == "kill_rule"]
    assert len(kill) == 1
    assert kill[0].one_tap is True
    assert kill[0].at_risk == 800.0


def test_optimization_score_is_second_opinion_not_our_score():
    """Google optimization_score прокидывается (0..100) отдельно и НЕ влияет на наш score."""
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000000, conversions=5)
    rows = [
        (
            ("Brand", "ENABLED"),
            Metrics(impressions=1000, clicks=100, cost_micros=500_000000, conversions=0),
        )
    ]
    opt = SimpleNamespace(score=0.62, uplift=0.15)
    res_no = build_audit(_report(totals, rows))
    res_opt = build_audit(_report(totals, rows), optimization_score=opt)
    assert res_no.optimization_score is None
    assert res_opt.optimization_score == 62
    assert res_opt.optimization_uplift == 15
    assert res_opt.score == res_no.score  # Google-балл НЕ влияет на наш score


def test_at_risk_containment_caps_keyword_inside_campaign():
    """Ревью 2026-07-08: слив кампании (⊃) + дорогой ключ ТОЙ ЖЕ кампании не плюсуются — headline
    ≤ расход кампании (расход ключа — часть расхода кампании)."""
    totals = Metrics(impressions=3000, clicks=230, cost_micros=500_000000, conversions=10)
    camp_rows = [
        (
            ("Brand", "ENABLED"),
            Metrics(impressions=1000, clicks=100, cost_micros=200_000000, conversions=0),
        ),
        (
            ("Generic", "ENABLED"),
            Metrics(impressions=2000, clicks=130, cost_micros=300_000000, conversions=10),
        ),
    ]
    kw_rows = [
        (
            ("Brand", "grp", "дёшево", "BROAD"),
            Metrics(impressions=500, clicks=80, cost_micros=150_000000, conversions=0),
        ),
    ]
    res = build_audit(_report(totals, camp_rows, keyword_rows=kw_rows))
    assert res.at_risk == 200.0  # 200 (slив Brand) ⊇ 150 (ключ Brand) → 200, НЕ 350
    # прямой дедуп: с capом кампании = 200; без capа (старое) = 350
    fs = [
        Finding(
            "spend_no_conv",
            "waste",
            "warning",
            at_risk=200.0,
            spend_segment="Brand",
            target_campaign="Brand",
        ),
        Finding(
            "wasteful_keyword",
            "keywords",
            "warning",
            at_risk=150.0,
            spend_segment="kw::Brand::x",
            target_campaign="Brand",
        ),
    ]
    assert _dedup_at_risk(fs, 500.0, {"Brand": 200.0}) == 200.0
    assert _dedup_at_risk(fs, 500.0) == 350.0


def test_independent_keywords_still_sum_within_campaign_cost():
    """Два НЕзависимых дорогих ключа одной кампании суммируются (в пределах расхода кампании)."""
    fs = [
        Finding(
            "wasteful_keyword",
            "keywords",
            "warning",
            at_risk=100.0,
            spend_segment="kw::C::a",
            target_campaign="C",
        ),
        Finding(
            "wasteful_keyword",
            "keywords",
            "warning",
            at_risk=120.0,
            spend_segment="kw::C::b",
            target_campaign="C",
        ),
    ]
    assert _dedup_at_risk(fs, 1000.0, {"C": 500.0}) == 220.0  # сумма, кап 500 не режет
    assert _dedup_at_risk(fs, 1000.0, {"C": 150.0}) == 150.0  # кап расходом кампании


def test_google_recommendations_counted_and_dismissed_excluded():
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000000, conversions=5)
    rows = [
        (
            ("Brand", "ENABLED"),
            Metrics(impressions=1000, clicks=100, cost_micros=500_000000, conversions=0),
        )
    ]
    recs = [
        SimpleNamespace(type="KEYWORD", dismissed=False),
        SimpleNamespace(type="KEYWORD", dismissed=False),
        SimpleNamespace(type="TARGET_CPA_OPT_IN", dismissed=False),
        SimpleNamespace(type="CALLOUT_EXTENSION", dismissed=True),  # dismissed → исключён
    ]
    res = build_audit(_report(totals, rows), recommendations=recs)
    assert res.google_recommendations == {"KEYWORD": 2, "TARGET_CPA_OPT_IN": 1}


def test_audit_headline_empty_and_active():
    """audit_headline: пустой аккаунт → '' (звать /audit не на чем); активный → score/grade + /audit."""
    from audit.render import audit_headline

    assert audit_headline(build_audit(_report(Metrics(), []))) == ""
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000000, conversions=5)
    rows = [
        (
            ("Brand", "ENABLED"),
            Metrics(impressions=1000, clicks=100, cost_micros=500_000000, conversions=0),
        ),
        (
            ("Generic", "ENABLED"),
            Metrics(impressions=2000, clicks=200, cost_micros=500_000000, conversions=5),
        ),
    ]
    hl = audit_headline(build_audit(_report(totals, rows)), "ru")
    assert "85/100" in hl and "· B" in hl and "/audit" in hl and "под риском" in hl


def test_wasteful_search_term_mining():
    """Дорогой поисковый запрос без конверсий → находка wasteful_search_term (one-tap минус, EXACT).
    Отдельный kind от wasteful_keyword; минус — точное соответствие (режет только этот запрос)."""
    totals = Metrics(impressions=2000, clicks=200, cost_micros=500_000000, conversions=5)
    rows = [
        (
            ("Search", "ENABLED"),
            Metrics(impressions=2000, clicks=200, cost_micros=500_000000, conversions=5),
        )
    ]
    st = SimpleNamespace(
        search_term="бесплатно скачать",
        campaign="Search",
        ad_group="grp",
        keyword="скачать",
        match_type="BROAD",
        metrics=Metrics(impressions=300, clicks=120, cost_micros=90_000000, conversions=0),
    )
    res = build_audit(_report(totals, rows), search_terms=[st])
    f = next(f for f in res.findings if f.check_id == "wasteful_search_term")
    assert f.suggested_operation == "add_negative_keywords"
    assert f.one_tap is True
    assert f.evidence["keyword"] == "бесплатно скачать"
    assert f.evidence["match_type"] == "exact"
    assert f.at_risk == 90.0


def test_search_term_inherits_parent_keyword_segment_no_double_count():
    """D5: расход поискового запроса ⊂ расход его ключа. Раньше сегменты были разные
    («kw::…» и «st::…») ⇒ _dedup_at_risk СКЛАДЫВАЛ их и «Под риском» задваивалось
    (200 ключа + 90 его запроса = 290 при реальных 200). Теперь запрос наследует сегмент ключа."""
    totals = Metrics(impressions=2000, clicks=200, cost_micros=500_000000, conversions=5)
    camp_rows = [
        (
            ("Search", "ENABLED"),
            Metrics(impressions=2000, clicks=200, cost_micros=500_000000, conversions=5),
        )
    ]
    kw_rows = [
        (
            ("Search", "grp", "скачать", "BROAD"),
            Metrics(impressions=1000, clicks=150, cost_micros=200_000000, conversions=0),
        )
    ]
    st = SimpleNamespace(
        search_term="бесплатно скачать",
        campaign="Search",
        ad_group="grp",
        keyword="Скачать",  # регистр из другого GAQL-ресурса — сегмент нормализуется
        match_type="BROAD",
        metrics=Metrics(impressions=300, clicks=120, cost_micros=90_000000, conversions=0),
    )
    res = build_audit(_report(totals, camp_rows, keyword_rows=kw_rows), search_terms=[st])
    kw_f = next(f for f in res.findings if f.check_id == "wasteful_keyword")
    st_f = next(f for f in res.findings if f.check_id == "wasteful_search_term")
    assert kw_f.spend_segment == st_f.spend_segment  # один сегмент денег
    assert res.at_risk == 200.0  # МАКСИМУМ по сегменту (расход ключа), а не 200 + 90


def test_search_term_without_parent_keyword_keeps_own_segment():
    """Ключ неизвестен (DSA/PMax — segments.keyword.info.text пуст) → прежний сегмент «st::…»:
    родителя нет, поглощать нечего, деньги запроса считаются самостоятельно."""
    totals = Metrics(impressions=2000, clicks=200, cost_micros=500_000000, conversions=5)
    camp_rows = [
        (
            ("Search", "ENABLED"),
            Metrics(impressions=2000, clicks=200, cost_micros=500_000000, conversions=5),
        )
    ]
    st = SimpleNamespace(
        search_term="что-то",
        campaign="Search",
        ad_group="grp",
        keyword="",
        match_type="",
        metrics=Metrics(impressions=300, clicks=120, cost_micros=90_000000, conversions=0),
    )
    res = build_audit(_report(totals, camp_rows), search_terms=[st])
    f = next(f for f in res.findings if f.check_id == "wasteful_search_term")
    assert f.spend_segment == "st::Search::что-то"
    assert f.at_risk == 90.0


def test_money_findings_never_one_tap():
    """Денежные находки (high_cpa / budget_imbalance / IS) — НЕ one-tap: apply-кнопка им не положена
    (golden rule #3, бюджет/ставка только прямой командой). Только pause/минус-слова — one-tap."""
    totals = Metrics(
        impressions=2000, clicks=200, cost_micros=1_000_000000, conversions=10
    )  # acct_cpa 100
    rows = [
        (
            ("Expensive", "ENABLED"),
            Metrics(impressions=1000, clicks=50, cost_micros=600_000000, conversions=1),
        ),  # cpa 600, 60% spend
        (
            ("Cheap", "ENABLED"),
            Metrics(impressions=1000, clicks=150, cost_micros=400_000000, conversions=9),
        ),
    ]
    is_rows = [
        _is("Expensive", 0.5, 0.4, 0.1)
    ]  # budget-lost + у Expensive есть конверсии → is_lost_revenue
    res = build_audit(_report(totals, rows), is_rows=is_rows)
    money_kinds = {
        "high_cpa",
        "budget_imbalance",
        "is_budget_constrained",
        "is_lost_revenue",
        "is_rank_constrained",
    }
    for f in res.findings:
        if f.check_id in money_kinds:
            assert f.one_tap is False
            assert f.suggested_operation is None
    assert any(f.check_id == "high_cpa" for f in res.findings)
    # Expensive конвертит (1 конв) и упирается в бюджет → оценка упущенной выручки (не is_budget_constrained)
    assert any(f.check_id == "is_lost_revenue" for f in res.findings)


def test_advice_operation_never_one_tap():
    """advice_operation — МЕТКА для замера эффекта (advisor.outcome), а НЕ путь исполнения.
    Гард против тихого расширения денежного пути: положил бы кто-то сюда 'pause_campaign', и
    bot._audit_run (f.suggested_operation ∈ _ADVISE_APPLY_OPS) остался бы чист, а вот маппер в
    Recommendation.suggested_operation — уже нет, и у ставки/бюджета появилась бы кнопка (rule #3).

    Два инварианта: (1) ни одна метка не пересекается с ONE_TAP_OPS; (2) одна находка не несёт
    обе операции сразу — иначе непонятно, что исполняется."""
    from audit.engine import ONE_TAP_OPS

    # Богатый отчёт: dead-кампания (waste), дорогая (high_cpa + перекос бюджета), слабый RSA, ключ-мот.
    totals = Metrics(impressions=5000, clicks=300, cost_micros=1_000_000000, conversions=10)
    rows = [
        (
            ("Expensive", "ENABLED"),
            Metrics(impressions=3000, clicks=100, cost_micros=700_000000, conversions=1),
        ),
        (
            ("Dead", "ENABLED"),
            Metrics(impressions=2000, clicks=200, cost_micros=300_000000, conversions=0),
        ),
    ]
    kw_rows = [
        (
            ("Dead", "AG", "мот", "BROAD"),
            Metrics(impressions=500, clicks=50, cost_micros=80_000000, conversions=0),
        )
    ]
    ad_rows = [
        (
            ("Expensive", "AG", "1", "RSA"),
            Metrics(impressions=2000, clicks=5, cost_micros=100_000000, conversions=0),
        )
    ]
    findings = build_audit(_report(totals, rows, kw_rows, ad_rows)).findings
    got = {f.check_id for f in findings}
    assert {"high_cpa", "budget_imbalance", "low_ctr_ad", "wasteful_keyword"} <= got, (
        f"сценарий выродился — не сработали чеки с advice_operation, получено {sorted(got)}"
    )
    for f in findings:
        assert f.advice_operation not in ONE_TAP_OPS, (
            f"{f.check_id}: advice_operation={f.advice_operation!r} попал в ONE_TAP_OPS — "
            "это метка для outcome, а не кнопка «применить»"
        )
        assert not (f.suggested_operation and f.advice_operation), (
            f"{f.check_id}: обе операции сразу (suggested={f.suggested_operation!r}, "
            f"advice={f.advice_operation!r}) — непонятно, что исполнять"
        )


def test_evidence_match_type_matches_mutation_schema():
    """evidence['match_type'] уходит в bot._advise_apply_params → AddNegativeKeywords(**params).
    GAQL отдаёт ENUM-имя ('BROAD'), схема ждёт Literal['broad','phrase','exact'] — сырое значение
    уронило бы валидацию, и one-tap «➖ В минус-слова» молча превращался бы в «совет устарел».
    Гард на дрейф: _MATCH_TYPES в движке обязан совпадать со схемой мутации."""
    from typing import get_args

    from agent.tools.schemas import MatchType
    from audit.engine import _MATCH_TYPES, _norm_match_type

    assert _MATCH_TYPES == set(get_args(MatchType))
    assert _norm_match_type("BROAD") == "broad"  # ← ровно то, что приходит из keyword_view
    assert _norm_match_type("Exact") == "exact"
    assert _norm_match_type("BROAD_MATCH_MODIFIER") is None  # незнакомое → не тащим в мутацию
    assert _norm_match_type("") is None and _norm_match_type(None) is None

    totals = Metrics(impressions=1000, clicks=100, cost_micros=100_000000, conversions=5)
    rep = _report(
        totals,
        [
            (
                ("C", "ENABLED"),
                Metrics(impressions=1000, clicks=100, cost_micros=100_000000, conversions=5),
            )
        ],
        keyword_rows=[
            (
                ("C", "AG", "дорогой ключ", "EXACT"),
                Metrics(impressions=200, clicks=20, cost_micros=50_000000, conversions=0),
            )
        ],
    )
    kw = [f for f in build_audit(rep).findings if f.check_id == "wasteful_keyword"]
    assert kw and kw[0].evidence["match_type"] == "exact", (
        "минус обязан зеркалить тип соответствия ключа (exact-ключ → exact-минус), "
        "а не резать шире дефолтным broad"
    )


def test_finding_text_parity_with_legacy_advisor_lines():
    """audit.render.finding_text — ЕДИНСТВЕННЫЙ текст рекомендации (advisor.render.render_recommendation
    удалён вместе с детекторами). Гард: смысловые куски старых advise_rec_* не потерялись при переезде.

    budget_imbalance — не косметика, а правило #3: строка зовёт тронуть бюджет ⇒ бот обязан тут же
    сказать, что САМ его не тронет (раньше это жило только в advise_rec_budget_imbalance)."""
    from audit.render import finding_text

    def line(check_id, facts, lang="ru"):
        return finding_text(Finding(check_id, "x", "info", facts=facts), lang, "USD")

    budget = line("budget_imbalance", {"campaign": "Whale", "share": 75, "cpa": 100})
    assert "Whale" in budget and "75" in budget
    assert "команд" in budget.lower(), "оговорка правила #3 («только по твоей команде») потерялась"
    assert "100" in budget, "CPA в строке перекоса бюджета потерялся — совет без числа неубедителен"
    assert "command" in line("budget_imbalance", {"campaign": "W", "share": 75, "cpa": 100}, "en")

    kw = line(
        "wasteful_keyword", {"campaign": "C", "keyword": "мот", "match_type": "EXACT", "cost": 9}
    )
    assert "мот" in kw and "exact" in kw, "тип соответствия ключа потерялся"

    ad = line("low_ctr_ad", {"campaign": "C", "ad_group": "AG", "ctr": 0.4, "acct_ctr": 2.0})
    assert "AG" in ad, "группа потерялась — RSA чинится на уровне ГРУППЫ, кампании мало"

    one = line("single_campaign", {"campaign": "Only", "cost": 500})
    assert "Only" in one and "500" in one


# ── N1.0b: golden-МАТРИЦА по всем grade-бэндам + снапшот полного вектора весов ────────
def _campaign(name: str, cost: float, clicks: int, conv: float, imps: int = 1000):
    return (
        (name, "ENABLED"),
        Metrics(
            impressions=imps, clicks=clicks, cost_micros=int(cost * 1_000_000), conversions=conv
        ),
    )


_ACTION = SimpleNamespace(status="ENABLED", primary_for_goal=True, name="Purchase")

# Сценарии пинуют ТОЧНЫЙ score в каждом бэнде (не только B, крит. N1.0b): любая правка весов/
# порогов/набора проверок сдвигает матрицу и ловится здесь, а не молча у клиента.
_GOLDEN_MATRIX = [
    # (метка, totals, campaign_rows, kwargs, score, grade, at_risk)
    (
        "A",  # один мелкий слив 20% расхода → waste 30×0.2=6 → 94
        Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5),
        [_campaign("Small-Waste", 200, 40, 0), _campaign("Main", 800, 260, 5, imps=2000)],
        {},
        94,
        "A",
        200.0,
    ),
    (
        "B",  # клон исходного golden: слив 50% → waste 15 → 85
        Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5),
        [_campaign("Brand", 500, 100, 0), _campaign("Generic", 500, 200, 5, imps=2000)],
        {},
        85,
        "B",
        500.0,
    ),
    (
        "C",  # два слива 50%+30% → waste 30×0.8=24 → 76
        Metrics(impressions=3000, clicks=190, cost_micros=1_000_000_000, conversions=4),
        [
            _campaign("Brand", 500, 100, 0),
            _campaign("Gen2", 300, 50, 0),
            _campaign("Main", 200, 40, 4),
        ],
        {},
        76,
        "C",
        800.0,
    ),
    (
        "D",  # полный слив (30) + zero_conversions при живом трекинге (20) → ровно 50 (граница D)
        Metrics(impressions=6000, clicks=300, cost_micros=1_000_000_000, conversions=0),
        [_campaign("A", 500, 150, 0, imps=3000), _campaign("B", 500, 150, 0, imps=3000)],
        {"conversion_actions": [_ACTION]},
        50,
        "D",
        1000.0,
    ),
    (
        "F",  # D + два дорогих ключа (keywords 10×0.4=4) → 46
        Metrics(impressions=6000, clicks=300, cost_micros=1_000_000_000, conversions=0),
        [_campaign("A", 500, 150, 0, imps=3000), _campaign("B", 500, 150, 0, imps=3000)],
        {
            "conversion_actions": [_ACTION],
            "keyword_rows": [
                (
                    ("A", "grp", "kw1", "PHRASE"),
                    Metrics(impressions=500, clicks=40, cost_micros=200_000_000, conversions=0),
                ),
                (
                    ("B", "grp", "kw2", "PHRASE"),
                    Metrics(impressions=500, clicks=40, cost_micros=200_000_000, conversions=0),
                ),
            ],
        },
        46,
        "F",
        1000.0,
    ),
]


@pytest.mark.parametrize(
    "label,totals,rows,kwargs,score,grade,at_risk",
    _GOLDEN_MATRIX,
    ids=[r[0] for r in _GOLDEN_MATRIX],
)
def test_golden_matrix_all_grade_bands(label, totals, rows, kwargs, score, grade, at_risk):
    kw_rows = kwargs.pop("keyword_rows", ())
    res = build_audit(_report(totals, rows, keyword_rows=kw_rows), **kwargs)
    assert (res.score, res.grade, res.at_risk) == (score, grade, at_risk)
    assert res.score_model_version == SCORE_MODEL_VERSION  # N1.0a: версия на каждом результате


def test_weight_vector_snapshot_pins_full_model():
    """N1.0b: полный вектор констант модели — точным равенством. Правка ЛЮБОГО веса/порога обязана
    осознанно править этот тест (и тем самым — версию модели N1.0a), а не молча сдвигать grade."""
    assert FAMILY_WEIGHT == {
        "waste": 30.0,
        "conversion_tracking": 20.0,
        "budget": 10.0,
        "keywords": 10.0,
        "geo": 8.0,
        "rsa": 7.0,
        "delivery": 6.0,
        "structure": 5.0,
        "bidding": 4.0,
        "assets": 0.0,
    }
    assert sum(FAMILY_WEIGHT.values()) == 100.0
    assert SEVERITY_MULT == {"warning": 1.0, "info": 0.4}
    assert NONMONEY_INTENSITY == 0.5
    assert GRADE_BANDS == ((90.0, "A"), (80.0, "B"), (65.0, "C"), (50.0, "D"), (0.0, "F"))
    assert DEFAULT_AUDIT_THRESHOLDS == {
        "min_spend": 1.0,
        "pause_min_spend": 5.0,
        "high_cpa_factor": 2.0,
        "kill_cpa_factor": 3.0,
        "budget_share_pct": 60.0,
        "kw_min_spend": 3.0,
        "kw_top_n": 5,
        "min_impressions": 200.0,
        "low_ctr_factor": 0.5,
        "single_campaign_min_spend": 10.0,
        "no_conv_min_spend": 10.0,
        "is_lost_min": 0.10,
        "is_rank_lost_min": 0.20,
        "is_search_floor": 0.05,
        "is_data_tolerance": 0.02,
        "broad_min_spend": 5.0,
        "kw_per_group_max": 20,
        "rsa_min_per_group": 2,
        "qs_fail": 4,
        "qs_component_below_pct": 0.35,
        "smart_bid_min_conv": 30,
        "geo_min_spend": 20.0,
        "schedule_min_spend": 20.0,
        "bid_gap_min": 0.10,
        "kw_rank_lost_top_min": 0.30,
    }


# ── N1.0a: версия score-модели ────────────────────────────────────────────────────────
def test_score_model_version_stable_and_sensitive():
    """Версия детерминирована (12 hex) и меняется от ЛЮБОЙ правки измерения: весов, множителей,
    порогов, бэндов, реестра проверок (вкл. family/severity) и ручной эпохи (семантика формул).
    Пустой аккаунт тоже несёт версию (снапшоту/тренду нужна)."""
    assert re.fullmatch(r"[0-9a-f]{12}", SCORE_MODEL_VERSION)
    assert compute_score_model_version() == SCORE_MODEL_VERSION
    assert compute_score_model_version(family_weight={"waste": 31.0}) != SCORE_MODEL_VERSION
    assert compute_score_model_version(severity_mult={"warning": 0.9}) != SCORE_MODEL_VERSION
    assert (
        compute_score_model_version(thresholds={**DEFAULT_AUDIT_THRESHOLDS, "kw_min_spend": 4.0})
        != SCORE_MODEL_VERSION
    )
    # новый чек / смена severity существующего / бамп эпохи — всё сдвигает версию
    assert (
        compute_score_model_version(
            check_registry={**CHECK_REGISTRY, "new_check": ("waste", "warning")}
        )
        != SCORE_MODEL_VERSION
    )
    assert (
        compute_score_model_version(
            check_registry={**CHECK_REGISTRY, "low_ctr_ad": ("rsa", "warning")}
        )
        != SCORE_MODEL_VERSION
    )
    assert compute_score_model_version(epoch=999) != SCORE_MODEL_VERSION
    empty = build_audit(_report(Metrics(), []))
    assert empty.score_model_version == SCORE_MODEL_VERSION


def test_check_registry_matches_engine_source():
    """Дрейф-гард: каждая тройка check_id/family/severity в исходнике движка обязана совпадать с
    CHECK_REGISTRY (и наоборот) — иначе версия модели (N1.0a) молча не заметит новый/изменённый чек."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "audit" / "engine.py").read_text(
        encoding="utf-8"
    )
    triples = re.findall(r'check_id="([a-z_]+)",\s*family="([a-z_]+)",\s*severity="([a-z]+)"', src)
    in_source = {cid: (fam, sev) for cid, fam, sev in triples}
    assert set(re.findall(r'check_id="([a-z_]+)"', src)) == set(in_source), (
        "у какого-то Finding в движке check_id/family/severity не литералы подряд — "
        "дрейф-гард реестра его не видит, поправь конструкцию или этот regex"
    )
    assert in_source == CHECK_REGISTRY
    assert set(CHECK_IDS) == set(CHECK_REGISTRY)


# ── N1.2: BROAD-ключи без Smart Bidding (семья bidding, прозой) ───────────────────────
def _broad_fixture(
    strategy: str = "MANUAL_CPC",
    status: str = "ENABLED",
    cost: float = 100.0,
    kw_text: str = "купить",
    negative_lists=None,
):
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [
        (
            ("Brand", status),
            Metrics(impressions=1000, clicks=100, cost_micros=500_000_000, conversions=3),
        ),
        _campaign("Generic", 500, 200, 2, imps=2000),
    ]
    kw = [
        (
            ("Brand", "grp", kw_text, "BROAD"),
            Metrics(impressions=100, clicks=10, cost_micros=int(cost * 1_000_000), conversions=1),
        ),
        (
            ("Brand", "grp", "точный", "EXACT"),
            Metrics(impressions=100, clicks=10, cost_micros=90_000_000, conversions=1),
        ),
    ]
    bidding = [SimpleNamespace(name="Brand", strategy_type=strategy, target_cpa=None)]
    return build_audit(
        _report(totals, rows, keyword_rows=kw), bidding=bidding, negative_lists=negative_lists
    )


def _neg(*campaigns: str):
    """Стаб NegativeListsInfo с картой покрытия (пустой = минусов нет ни у кого)."""
    return SimpleNamespace(
        count=1 if campaigns else 0,
        attached_campaigns=len(campaigns),
        campaign_level_count=0,
        campaigns_with_negatives=frozenset(campaigns),
    )


def test_broad_unmanaged_fires_on_manual_bidding_only_prose():
    res = _broad_fixture("MANUAL_CPC")
    f = next(f for f in res.findings if f.check_id == "broad_unmanaged")
    assert f.family == "bidding"
    # НЕДЕНЕЖНАЯ (ревью 2026-07-08): риск конфигурации, не измеренный слив — конвертящий BROAD
    # не инфлирует headline «под риском» и не задваивается с wasteful_keyword той же кампании.
    assert f.at_risk == 0.0
    assert res.at_risk == 0.0  # headline чист: слив меряют spend_no_conv/wasteful_keyword
    assert f.one_tap is False and f.suggested_operation is None  # курация, не one-tap
    # полный BROAD-расход остаётся в фактах для прозы (EXACT не считается)
    assert f.facts["cost"] == 100.0
    assert f.facts["kw_count"] == 1 and f.facts["strategy_type"] == "MANUAL_CPC"


def test_broad_unmanaged_silent_without_data_or_smart_or_paused():
    # Smart Bidding + нет данных о минусах → молчит (fail-safe; см. smart-ветку ниже по файлу)
    assert not any(
        f.check_id == "broad_unmanaged" for f in _broad_fixture("MAXIMIZE_CONVERSIONS").findings
    )
    # PAUSED-кампания → молчит (расхода вперёд не будет)
    assert not any(
        f.check_id == "broad_unmanaged" for f in _broad_fixture(status="PAUSED").findings
    )
    # ниже порога broad_min_spend → молчит
    assert not any(f.check_id == "broad_unmanaged" for f in _broad_fixture(cost=4.0).findings)
    # нет данных о стратегии (bidding не читан) → молчит (fail-safe, не гадаем)
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    kw = [
        (
            ("Brand", "grp", "купить", "BROAD"),
            Metrics(impressions=100, clicks=10, cost_micros=100_000_000, conversions=1),
        )
    ]
    res = build_audit(_report(totals, [_campaign("Brand", 500, 100, 3)], keyword_rows=kw))
    assert not any(f.check_id == "broad_unmanaged" for f in res.findings)


# ── Ф0 / G17 accuracy note: legacy BMM + вторая половина правила ──────────────────────
def test_broad_unmanaged_skips_legacy_bmm():
    """Legacy BMM («+купить +обувь») API отдаёт как match_type=BROAD, но ведёт себя ФРАЗОВО — это
    не «неуправляемое широкое», флажить нельзя даже при Manual CPC (ложный позитив)."""
    assert not any(
        f.check_id == "broad_unmanaged"
        for f in _broad_fixture("MANUAL_CPC", kw_text="+купить +обувь").findings
    )
    # …а настоящий BROAD в той же конфигурации — флажим. Приписку claude-ads «не флажить ВЕСЬ
    # BROAD+Manual CPC» не берём: её посылка («Google срезал +») неверна — «+» в тексте остался,
    # BMM отличим точно. Слепо следуя приписке, мы бы молча погасили реальный слив.
    assert any(f.check_id == "broad_unmanaged" for f in _broad_fixture("MANUAL_CPC").findings)


def test_broad_unmanaged_fires_on_smart_bidding_without_any_negatives():
    """Вторая половина G17: BROAD под Smart Bidding легитимен ТОЛЬКО если есть чем отсекать мусор.
    Ни одного минус-слова у кампании → флаг reason=no_negatives (раньше не ловилось вовсе)."""
    res = _broad_fixture("MAXIMIZE_CONVERSIONS", negative_lists=_neg())
    f = next(f for f in res.findings if f.check_id == "broad_unmanaged")
    assert (
        f.facts["reason"] == "no_negatives" and f.facts["strategy_type"] == "MAXIMIZE_CONVERSIONS"
    )
    assert f.at_risk == 0.0 and f.one_tap is False  # риск конфигурации; курация — не one-tap


def test_broad_unmanaged_smart_silent_when_negatives_exist_or_unknown():
    # минусы у кампании есть → BROAD под Smart Bidding легитимен, молчим
    assert not any(
        f.check_id == "broad_unmanaged"
        for f in _broad_fixture("MAXIMIZE_CONVERSIONS", negative_lists=_neg("Brand")).findings
    )
    # карта минусов не прочитана (стаб без поля) → smart-ветка молчит (fail-safe, не гадаем)
    stub = SimpleNamespace(count=0, attached_campaigns=0)
    assert not any(
        f.check_id == "broad_unmanaged"
        for f in _broad_fixture("MAXIMIZE_CONVERSIONS", negative_lists=stub).findings
    )
    # ручная стратегия при этом флажится и без данных о минусах (ветка от них не зависит)
    assert any(
        f.check_id == "broad_unmanaged"
        for f in _broad_fixture("MANUAL_CPC", negative_lists=stub).findings
    )


def test_no_negative_list_silent_with_campaign_level_negatives():
    """G14/G15: минусы ПРЯМО на кампании — тоже гигиена. Аккаунт без shared-списков, но с прямыми
    минусами → молчим (раньше выдавали ложное «в аккаунте нет минус-слов»)."""
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [_campaign("Main", 1000, 300, 5)]
    direct = SimpleNamespace(
        count=0,
        attached_campaigns=0,
        campaign_level_count=7,
        campaigns_with_negatives=frozenset({"Main"}),
    )
    assert not any(
        f.check_id == "no_negative_list"
        for f in build_audit(_report(totals, rows), negative_lists=direct).findings
    )
    # ни списков, ни прямых минусов → флаг остаётся
    empty = SimpleNamespace(
        count=0, attached_campaigns=0, campaign_level_count=0, campaigns_with_negatives=frozenset()
    )
    assert any(
        f.check_id == "no_negative_list"
        for f in build_audit(_report(totals, rows), negative_lists=empty).findings
    )


def test_is_rows_all_proto3_zero_marked_as_data_gap():
    """Ревью N1.3: IS-строки прочитаны, но ни одна не прошла tolerance (proto3-нули) — это «нет
    данных», а не «budget/rsa в норме»: движок дописывает impression_share в data_gaps."""
    totals = Metrics(impressions=1000, clicks=100, cost_micros=100_000_000, conversions=1)
    rows = [_campaign("A", 100, 100, 1)]
    dead = [_is("A", 0.0, 0.0, 0.0)]
    res = build_audit(_report(totals, rows), is_rows=dead, data_gaps=[])
    assert "impression_share" in res.data_gaps
    # годные строки → пробела нет; engine-only вызов (data_gaps=None) ничего не дописывает
    ok = build_audit(_report(totals, rows), is_rows=[_is("A", 0.5, 0.4, 0.1)], data_gaps=[])
    assert "impression_share" not in ok.data_gaps
    silent = build_audit(_report(totals, rows), is_rows=dead)
    assert silent.data_gaps is None


# ── N1.5: баннер «сначала почини измерение» + консервативный детект двойного счёта ────
def test_measurement_gap_banner_over_score():
    from audit.render import render_audit

    totals = Metrics(impressions=5000, clicks=400, cost_micros=200_000_000, conversions=0)
    # нет живых данных о действиях → баннера нет (это data gap, не утверждение)
    assert build_audit(_report(totals, [])).measurement_gap is False
    # действия прочитаны, активной primary нет → баннер (score НЕ подавляется)
    res = build_audit(_report(totals, []), conversion_actions=[])
    assert res.measurement_gap is True and res.score is not None
    card = render_audit(res, "ru")
    assert "почини измерение" in card and f"{res.score}/100" in card
    assert card.index("почини измерение") < card.index(f"{res.score}/100")  # НАД score
    # ENABLED, но НЕ primary → тоже разрыв (в «Конверсии» ничего не пишет)
    secondary = SimpleNamespace(status="ENABLED", primary_for_goal=False, name="Sec")
    assert build_audit(_report(totals, []), conversion_actions=[secondary]).measurement_gap is True
    # ENABLED primary есть → баннера нет
    ok = SimpleNamespace(status="ENABLED", primary_for_goal=True, name="P")
    assert build_audit(_report(totals, []), conversion_actions=[ok]).measurement_gap is False


def test_duplicate_conversions_conservative():
    totals = Metrics(impressions=5000, clicks=400, cost_micros=200_000_000, conversions=8)
    mk = lambda cat, primary=True, status="ENABLED": SimpleNamespace(  # noqa: E731
        status=status, primary_for_goal=primary, name=cat, category=cat
    )
    # ≥2 ENABLED primary одной категории → info-находка, прозой
    res = build_audit(_report(totals, []), conversion_actions=[mk("PURCHASE"), mk("PURCHASE")])
    f = next(f for f in res.findings if f.check_id == "duplicate_conversions")
    assert f.severity == "info" and f.at_risk == 0.0 and f.one_tap is False
    assert f.facts == {"category": "PURCHASE", "count": 2}
    # разные категории / secondary / не-ENABLED / категория-заглушка → молчим
    for cas in (
        [mk("PURCHASE"), mk("SUBMIT_LEAD_FORM")],
        [mk("PURCHASE"), mk("PURCHASE", primary=False)],
        [mk("PURCHASE"), mk("PURCHASE", status="REMOVED")],
        [mk("DEFAULT"), mk("DEFAULT")],
    ):
        r = build_audit(_report(totals, []), conversion_actions=cas)
        assert not any(f.check_id == "duplicate_conversions" for f in r.findings)


# ── N1.3: «недостаточно данных» ≠ «в норме» ───────────────────────────────────────────
def test_data_gaps_rendered_not_claimed_healthy():
    from audit.render import render_audit

    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [_campaign("Brand", 500, 100, 0), _campaign("Generic", 500, 200, 5, imps=2000)]
    # collect отчитался: conversion_actions и bidding упали → их семьи НЕ «в норме», сигналы — ℹ️
    res = build_audit(
        _report(totals, rows),
        search_terms=[],
        is_rows=[],
        data_gaps=["conversion_actions", "bidding"],
    )
    assert res.data_gaps == ["conversion_actions", "bidding"]
    card = render_audit(res, "ru", actions=False)
    assert "Недостаточно данных" in card and "действия-конверсии" in card
    ok_line = next(line for line in card.splitlines() if line.startswith("✅"))
    assert "Отслеживание конверсий" not in ok_line and "Ставки" not in ok_line
    assert "Структура" in ok_line  # у structure нет доп-сигналов — честное «в норме»
    # score от пробелов данных НЕ меняется
    assert res.score == build_audit(_report(totals, rows)).score


def test_engine_only_render_makes_no_family_claims():
    """/report health зовёт build_audit БЕЗ collect-слоя (data_gaps=None) — рендер не утверждает
    ни «в норме», ни «нет данных» (нечего утверждать: сигналы не собирались)."""
    from audit.render import render_audit

    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [_campaign("Brand", 500, 100, 0), _campaign("Generic", 500, 200, 5, imps=2000)]
    res = build_audit(_report(totals, rows))
    assert res.data_gaps is None
    card = render_audit(res, "ru", actions=False)
    assert "Проверено" not in card and "Недостаточно данных" not in card


async def test_gather_audit_records_data_gaps(monkeypatch):
    """collect: упавший best-effort фетчер → его имя в data_gaps; пустой результат ([]) — НЕ пробел."""
    from audit.collect import gather_audit

    fake_report = SimpleNamespace(customer_id="1", totals=Metrics(), breakdowns=[], currency="USD")

    async def fake_build_report(client, cid, period, **kw):
        return fake_report

    async def fake_run(fn, *args, label=""):
        if label in ("audit_is", "audit_bidding"):
            raise RuntimeError("boom")
        if label == "audit_currency":
            return "USD"
        return []

    monkeypatch.setattr("reports.service.build_account_report_async", fake_build_report)
    monkeypatch.setattr("core.resilience.run_ads_read_call", fake_run)
    res = await gather_audit(object(), "1", None)
    assert set(res.data_gaps) == {"impression_share", "bidding"}


# ── Heartbeat: дизапрувы / 0-показов / приостановка аккаунта (delivery, все НЕденежные) ──
def _policy(campaign, ad_group, ad_id, status):
    return SimpleNamespace(
        campaign=campaign, ad_group=ad_group, ad_id=ad_id, approval_status=status
    )


def test_ads_disapproved_per_campaign_prose_only():
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [_campaign("Brand", 500, 100, 5), _campaign("Generic", 500, 200, 5, imps=2000)]
    ap = [
        _policy("Brand", "g1", "1", "DISAPPROVED"),
        _policy("Brand", "g1", "2", "APPROVED_LIMITED"),
        _policy("Brand", "g2", "3", "AREA_OF_INTEREST_ONLY"),
        _policy("Generic", "g3", "4", "APPROVED"),  # одобрено → не в счёт
    ]
    res = build_audit(_report(totals, rows), ad_policy=ap)
    f = next(f for f in res.findings if f.check_id == "ads_disapproved")
    assert f.family == "delivery"
    assert f.at_risk == 0.0 and res.at_risk == 0.0  # НЕденежная — headline чист
    assert f.one_tap is False and f.suggested_operation is None
    assert f.facts["campaign"] == "Brand"
    assert f.facts["count"] == 3 and f.facts["disapproved"] == 1  # per-campaign агрегат
    assert not any(ff.target_campaign == "Generic" for ff in res.findings)


def test_ads_disapproved_silent_without_policy_or_all_approved():
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [_campaign("Brand", 500, 100, 5), _campaign("Generic", 500, 200, 5, imps=2000)]
    # None → сигнал не прочитан (fail-safe): молчим
    assert not any(
        f.check_id == "ads_disapproved" for f in build_audit(_report(totals, rows)).findings
    )
    # все APPROVED → молчим
    ap = [_policy("Brand", "g1", "1", "APPROVED")]
    assert not any(
        f.check_id == "ads_disapproved"
        for f in build_audit(_report(totals, rows), ad_policy=ap).findings
    )


def test_zero_impressions_enabled_silent_campaign():
    totals = Metrics(impressions=3000, clicks=300, cost_micros=500_000_000, conversions=5)
    rows = [
        _campaign("Live", 500, 300, 5, imps=3000),
        (("Silent", "ENABLED"), Metrics(impressions=0, clicks=0, cost_micros=0, conversions=0)),
    ]
    res = build_audit(_report(totals, rows))
    f = next(f for f in res.findings if f.check_id == "zero_impressions")
    assert f.family == "delivery" and f.target_campaign == "Silent"
    assert f.at_risk == 0.0 and f.one_tap is False


def test_zero_impressions_guards_and_status():
    # мёртвый аккаунт (totals.impressions == 0) → не флагаем (иначе весь Draft красный)
    dead = Metrics(impressions=0, clicks=0, cost_micros=0, conversions=0)
    rows = [(("Silent", "ENABLED"), Metrics(impressions=0, clicks=0, cost_micros=0, conversions=0))]
    assert build_audit(_report(dead, rows)).findings == []  # (нет активности → ранний выход)
    # PAUSED-кампания с 0 показов → не флагаем (её и не должно крутить)
    totals = Metrics(impressions=3000, clicks=300, cost_micros=500_000_000, conversions=5)
    rows2 = [
        _campaign("Live", 500, 300, 5, imps=3000),
        (("Paused", "PAUSED"), Metrics(impressions=0, clicks=0, cost_micros=0, conversions=0)),
    ]
    assert not any(
        f.check_id == "zero_impressions" for f in build_audit(_report(totals, rows2)).findings
    )
    # регресс: golden не приобрёл zero_impressions (все кампании с показами)
    g = build_audit(
        _report(
            Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5),
            [_campaign("Brand", 500, 100, 0), _campaign("Generic", 500, 200, 5, imps=2000)],
        )
    )
    assert [f.check_id for f in g.findings] == ["spend_no_conv"]


def test_account_status_banner_over_score_not_suppressed():
    from audit.render import render_audit

    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [_campaign("Brand", 500, 100, 0), _campaign("Generic", 500, 200, 5, imps=2000)]
    # активный статус / None → баннера нет
    assert build_audit(_report(totals, rows), account_status="ENABLED").account_status is None
    assert build_audit(_report(totals, rows)).account_status is None
    # SUSPENDED → баннер НАД score, score НЕ подавлен
    res = build_audit(_report(totals, rows), account_status="SUSPENDED")
    assert res.account_status == "SUSPENDED" and res.score == 85
    card = render_audit(res, "ru")
    assert "показы остановлены" in card
    assert card.index("показы остановлены") < card.index(f"{res.score}/100")
    # баннер и на мёртвом аккаунте (suspension объясняет отсутствие активности)
    dead = build_audit(_report(Metrics(), []), account_status="CANCELED")
    assert dead.account_status == "CANCELED" and dead.score is None
    assert "показы остановлены" in render_audit(dead, "ru")


def test_delivery_data_gap_not_claimed_healthy():
    from audit.render import render_audit

    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=5)
    rows = [_campaign("Brand", 500, 100, 0), _campaign("Generic", 500, 200, 5, imps=2000)]
    # ad_policy упал (None → data gap) → delivery НЕ «в норме», сигнал — ℹ️
    res = build_audit(
        _report(totals, rows),
        search_terms=[],
        is_rows=[],
        conversion_actions=[SimpleNamespace(status="ENABLED", primary_for_goal=True, category="X")],
        data_gaps=["ad_policy"],
    )
    card = render_audit(res, "ru", actions=False)
    assert "Недостаточно данных" in card and "модерация объявлений" in card
    ok_line = next((line for line in card.splitlines() if line.startswith("✅")), "")
    assert "Показ и модерация" not in ok_line


def test_audit_never_imports_mutations():
    """Инвариант: пакет audit/ read-only — не тянет ads.mutations / ads.service (GR6/GR8)."""
    pkg = pathlib.Path(__file__).resolve().parent.parent / "audit"
    forbidden = ("ads.mutations", "ads.service")
    for p in sorted(pkg.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not any(node.module.startswith(f) for f in forbidden), (
                    f"{p.name}: {node.module}"
                )
            elif isinstance(node, ast.Import):
                for n in node.names:
                    assert not any(n.name.startswith(f) for f in forbidden), f"{p.name}: {n.name}"
