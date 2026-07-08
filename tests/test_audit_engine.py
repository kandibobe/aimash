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
    is_rows = [_is("Expensive", 0.5, 0.4, 0.1)]  # budget-constrained
    res = build_audit(_report(totals, rows), is_rows=is_rows)
    money_kinds = {"high_cpa", "budget_imbalance", "is_budget_constrained", "is_rank_constrained"}
    for f in res.findings:
        if f.check_id in money_kinds:
            assert f.one_tap is False
            assert f.suggested_operation is None
    assert any(f.check_id == "high_cpa" for f in res.findings)
    assert any(f.check_id == "is_budget_constrained" for f in res.findings)


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
        "budget": 12.0,
        "bidding": 10.0,
        "keywords": 10.0,
        "rsa": 8.0,
        "structure": 6.0,
        "geo": 2.0,
        "assets": 2.0,
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
        "is_data_tolerance": 0.02,
        "broad_min_spend": 5.0,
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
def _broad_fixture(strategy: str = "MANUAL_CPC", status: str = "ENABLED", cost: float = 100.0):
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
            ("Brand", "grp", "купить", "BROAD"),
            Metrics(impressions=100, clicks=10, cost_micros=int(cost * 1_000_000), conversions=1),
        ),
        (
            ("Brand", "grp", "точный", "EXACT"),
            Metrics(impressions=100, clicks=10, cost_micros=90_000_000, conversions=1),
        ),
    ]
    bidding = [SimpleNamespace(name="Brand", strategy_type=strategy, target_cpa=None)]
    return build_audit(_report(totals, rows, keyword_rows=kw), bidding=bidding)


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
    # Smart Bidding → молчит (broad под конверсионным сигналом легитимен)
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
