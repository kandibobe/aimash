"""advisor Фаза 3: темы советов keywords / rsa / structure + фильтр build_candidates по темам.

Чистое ядро (advisor.rules) — детерминировано, без сети/SDK. Рекомендации остаются advisory:
suggested_operation — метка, исполнение любого совета идёт через confirm-гейт (инварианты — в
tests/test_advisor.py).
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from advisor import render, rules  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402


def _m(cost, *, clicks=10, conv=0.0, impressions=100):
    return Metrics(
        impressions=impressions,
        clicks=clicks,
        cost_micros=int(cost * 1_000_000),
        conversions=conv,
        conv_value=0.0,
    )


def _report(*, campaign_rows=None, keyword_rows=None, ad_rows=None, totals=None, currency="USD"):
    bds = []
    if campaign_rows is not None:
        bds.append(Breakdown("campaign", "Кампании", ["Кампания", "Статус"], campaign_rows))
    if keyword_rows is not None:
        bds.append(Breakdown("keyword", "Ключи", ["К", "Г", "Ключ", "Тип"], keyword_rows))
    if ad_rows is not None:
        bds.append(Breakdown("ad", "Объявления", ["К", "Г", "ID", "Тип"], ad_rows))
    period = SimpleNamespace(label="last 30 days", date_from="2026-06-04", date_to="2026-07-03")
    return SimpleNamespace(
        customer_id="7753643025",
        period=period,
        totals=totals if totals is not None else _m(0),
        prev_totals=None,
        breakdowns=bds,
        currency=currency,
    )


T = rules.DEFAULT_ADVISOR_THRESHOLDS


# ── keywords ────────────────────────────────────────────────────────────────────────
def test_wasteful_keyword_detected():
    rows = [(("Camp", "AG", "дешёвые кроссовки", "BROAD"), _m(20, clicks=15, conv=0))]
    recs = rules.rec_wasteful_keyword(_report(keyword_rows=rows), T)
    assert len(recs) == 1
    r = recs[0]
    assert r.kind == "wasteful_keyword" and r.topic == "keywords"
    assert r.target_campaign == "Camp"
    assert r.suggested_operation == "add_negative_keywords"  # метка, не путь исполнения
    assert r.facts["keyword"] == "дешёвые кроссовки"


def test_wasteful_keyword_capped_and_sorted():
    rows = [(("C", "AG", f"kw{i}", "BROAD"), _m(float(i), clicks=5, conv=0)) for i in range(3, 20)]
    recs = rules.rec_wasteful_keyword(_report(keyword_rows=rows), T)
    assert len(recs) == int(T["kw_top_n"])  # анти-спам: не больше kw_top_n
    costs = [r.evidence["cost"] for r in recs]
    assert costs == sorted(costs, reverse=True)  # самые дорогие первыми


def test_wasteful_keyword_ignores_converting_and_cheap():
    rows = [
        (("C", "AG", "good", "EXACT"), _m(50, clicks=20, conv=3)),  # есть конверсии
        (("C", "AG", "tiny", "BROAD"), _m(1, clicks=1, conv=0)),  # ниже kw_min_spend
    ]
    assert rules.rec_wasteful_keyword(_report(keyword_rows=rows), T) == []


# ── rsa ───────────────────────────────────────────────────────────────────────────
def test_low_ctr_ad_detected():
    # аккаунт CTR = 100/1000 = 10%; объявление 10/500 = 2% < 0.5×10% = 5%
    totals = _m(100, clicks=100, impressions=1000)
    rows = [(("Camp", "AG", "123", "RESPONSIVE_SEARCH_AD"), _m(30, clicks=10, impressions=500))]
    recs = rules.rec_low_ctr_ad(_report(ad_rows=rows, totals=totals), T)
    assert len(recs) == 1 and recs[0].kind == "low_ctr_ad" and recs[0].topic == "rsa"
    assert recs[0].suggested_operation == "create_rsa"


def test_low_ctr_ad_silent_without_account_ctr():
    totals = _m(0, clicks=0, impressions=0)  # acct ctr 0 → правило молчит
    rows = [(("C", "AG", "1", "RSA"), _m(30, clicks=1, impressions=500))]
    assert rules.rec_low_ctr_ad(_report(ad_rows=rows, totals=totals), T) == []


def test_low_ctr_ad_ignores_low_impressions():
    totals = _m(100, clicks=100, impressions=1000)  # ctr 10%
    rows = [(("C", "AG", "1", "RSA"), _m(1, clicks=0, impressions=50))]  # <min_impressions
    assert rules.rec_low_ctr_ad(_report(ad_rows=rows, totals=totals), T) == []


# ── structure ─────────────────────────────────────────────────────────────────────
def test_single_campaign_detected():
    rows = [(("Solo", "ENABLED"), _m(120, clicks=40, conv=2))]
    recs = rules.rec_single_campaign(_report(campaign_rows=rows), T)
    assert len(recs) == 1 and recs[0].kind == "single_campaign" and recs[0].topic == "structure"
    # фикс ревью: не измеряемо (новая кампания ≠ старая) → suggested_operation честно None
    assert recs[0].suggested_operation is None


def test_single_campaign_silent_with_multiple_or_paused():
    two = [(("A", "ENABLED"), _m(120)), (("B", "ENABLED"), _m(80))]
    assert rules.rec_single_campaign(_report(campaign_rows=two), T) == []
    paused = [(("A", "ENABLED"), _m(120)), (("B", "PAUSED"), _m(80))]
    # ровно одна ENABLED (A) с расходом ≥ порога → срабатывает
    assert len(rules.rec_single_campaign(_report(campaign_rows=paused), T)) == 1
    tiny = [(("A", "ENABLED"), _m(2))]  # ниже single_campaign_min_spend
    assert rules.rec_single_campaign(_report(campaign_rows=tiny), T) == []


# ── build_candidates: фильтр по темам ───────────────────────────────────────────────
def test_build_candidates_topic_filter():
    rep = _report(
        campaign_rows=[(("Solo", "ENABLED"), _m(150, clicks=50, conv=0))],
        keyword_rows=[(("Solo", "AG", "kw", "BROAD"), _m(20, clicks=10, conv=0))],
        ad_rows=[(("Solo", "AG", "1", "RSA"), _m(10, clicks=2, impressions=500))],
        totals=_m(150, clicks=50, impressions=1000, conv=0),
    )
    only_kw = rules.build_candidates(rep, topics=["keywords"])
    assert only_kw and {c.topic for c in only_kw} == {"keywords"}
    all_topics = {c.topic for c in rules.build_candidates(rep)}
    assert "keywords" in all_topics and "structure" in all_topics


# ── рендер новых видов ──────────────────────────────────────────────────────────────
def test_render_new_kinds_nonempty():
    cases = [
        rules.Recommendation(
            kind="wasteful_keyword",
            topic="keywords",
            severity="warning",
            target_campaign="C",
            facts={
                "campaign": "C",
                "keyword": "kw",
                "match_type": "BROAD",
                "cost": 20,
                "clicks": 10,
                "currency": "USD",
            },
        ),
        rules.Recommendation(
            kind="low_ctr_ad",
            topic="rsa",
            severity="info",
            target_campaign="C",
            facts={
                "campaign": "C",
                "ad_group": "AG",
                "ctr": 2.0,
                "acct_ctr": 10.0,
                "currency": "USD",
            },
        ),
        rules.Recommendation(
            kind="single_campaign",
            topic="structure",
            severity="info",
            target_campaign="Solo",
            facts={"campaign": "Solo", "cost": 120, "currency": "USD"},
        ),
    ]
    for rec in cases:
        text = render.render_recommendation(rec, "ru")
        assert text and rec.facts["campaign"] in text
        assert render.render_recommendation(rec, "en")  # EN тоже не пустой
