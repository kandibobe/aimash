"""Юнит-тесты движка аудита — чистые, без SDK/сети (образец tests/test_advisor.py).

Пинуют health-score константами (крит.C1): правка весов/порогов ломает golden-fixture, а не молча
сдвигает всем grade. Проверяют дедуп денег-под-риском (крит.C2), пустой аккаунт («—», не 100),
границы буквы и инвариант «audit/ не импортирует ads.mutations/ads.service».
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

from reports.queries import Breakdown, Metrics

from audit.engine import Finding, _dedup_at_risk, build_audit
from audit.thresholds import grade_for


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
