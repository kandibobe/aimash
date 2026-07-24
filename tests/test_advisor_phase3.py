"""Темы советов keywords / rsa / structure — ЧЕРЕЗ движок аудита (единственный источник находок).

До 2026-07-13 эти сценарии проверяли детекторы advisor.rules (rec_wasteful_keyword / rec_low_ctr_ad /
rec_single_campaign) — копии чеков аудита с отдельными порогами. Детекторы удалены (см.
advisor/from_findings.py), но сами СЦЕНАРИИ никуда не делись: их поведение теперь обеспечивают чеки
audit.engine, а /advise получает находки через маппер. Тесты сохранены и переведены на новый путь —
иначе слияние молча вырезало бы покрытие (анти-спам-кап по ключам, гарды CTR, гарды одной кампании).

Чистое ядро (без сети/SDK). Рекомендации остаются advisory: suggested_operation — метка, исполнение
идёт через confirm-гейт (инварианты — tests/test_advisor.py).
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from advisor.from_findings import to_recommendations  # noqa: E402
from audit.engine import build_audit  # noqa: E402
from audit.thresholds import DEFAULT_AUDIT_THRESHOLDS as T  # noqa: E402
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
    bds = [Breakdown("campaign", "Кампании", ["Кампания", "Статус"], campaign_rows or [])]
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


def _recs(report, topics=None):
    res = build_audit(report)
    return to_recommendations(res, "ru", res.currency, topics=topics)


def _of_kind(report, kind):
    return [r for r in _recs(report) if r.kind == kind]


# ── keywords ────────────────────────────────────────────────────────────────────────
def test_wasteful_keyword_detected():
    rows = [(("Camp", "AG", "дешёвые кроссовки", "BROAD"), _m(20, clicks=15, conv=0))]
    recs = _of_kind(_report(keyword_rows=rows), "wasteful_keyword")
    assert len(recs) == 1
    r = recs[0]
    assert r.topic == "keywords"  # topic == семья чека (одна таксономия)
    assert r.target_campaign == "Camp"
    assert r.suggested_operation == "add_negative_keywords"  # метка, не путь исполнения
    assert r.facts["keyword"] == "дешёвые кроссовки"


def test_wasteful_keyword_capped_and_sorted():
    rows = [(("C", "AG", f"kw{i}", "BROAD"), _m(float(i), clicks=5, conv=0)) for i in range(3, 20)]
    recs = _of_kind(_report(keyword_rows=rows), "wasteful_keyword")
    assert len(recs) == int(T["kw_top_n"])  # анти-спам: не больше kw_top_n
    costs = [r.evidence["cost"] for r in recs]
    assert costs == sorted(costs, reverse=True)  # самые дорогие первыми


def test_wasteful_keyword_ignores_converting_and_cheap():
    rows = [
        (("C", "AG", "good", "EXACT"), _m(50, clicks=20, conv=3)),  # есть конверсии
        (("C", "AG", "tiny", "BROAD"), _m(1, clicks=1, conv=0)),  # ниже kw_min_spend
    ]
    assert _of_kind(_report(keyword_rows=rows), "wasteful_keyword") == []


# ── rsa ───────────────────────────────────────────────────────────────────────────
def test_low_ctr_ad_detected():
    # аккаунт CTR = 100/1000 = 10%; объявление 10/500 = 2% < 0.5×10% = 5%
    totals = _m(100, clicks=100, impressions=1000)
    rows = [(("Camp", "AG", "123", "RESPONSIVE_SEARCH_AD"), _m(30, clicks=10, impressions=500))]
    recs = _of_kind(_report(ad_rows=rows, totals=totals), "low_ctr_ad")
    assert len(recs) == 1 and recs[0].topic == "rsa"
    assert recs[0].suggested_operation == "create_rsa"  # advice_operation (кнопки НЕ даёт)


def test_low_ctr_ad_silent_without_account_ctr():
    totals = _m(0, clicks=0, impressions=0)  # acct ctr 0 → чек молчит
    rows = [(("C", "AG", "1", "RSA"), _m(30, clicks=1, impressions=500))]
    assert _of_kind(_report(ad_rows=rows, totals=totals), "low_ctr_ad") == []


def test_low_ctr_ad_ignores_low_impressions():
    totals = _m(100, clicks=100, impressions=1000)  # ctr 10%
    rows = [(("C", "AG", "1", "RSA"), _m(1, clicks=0, impressions=50))]  # <min_impressions
    assert _of_kind(_report(ad_rows=rows, totals=totals), "low_ctr_ad") == []


# ── structure ─────────────────────────────────────────────────────────────────────
def test_single_campaign_detected():
    rows = [(("Solo", "ENABLED"), _m(120, clicks=40, conv=2))]
    recs = _of_kind(
        _report(campaign_rows=rows, totals=_m(120, clicks=40, conv=2)), "single_campaign"
    )
    assert len(recs) == 1 and recs[0].topic == "structure"
    # не измеряемо (новая кампания ≠ старая) → метки операции нет вовсе
    assert recs[0].suggested_operation is None


def test_single_campaign_silent_with_multiple_or_paused():
    two = [(("A", "ENABLED"), _m(120)), (("B", "ENABLED"), _m(80))]
    assert _of_kind(_report(campaign_rows=two, totals=_m(200)), "single_campaign") == []
    paused = [(("A", "ENABLED"), _m(120)), (("B", "PAUSED"), _m(80))]
    # ровно одна ENABLED (A) с расходом ≥ порога → срабатывает
    assert len(_of_kind(_report(campaign_rows=paused, totals=_m(200)), "single_campaign")) == 1
    tiny = [(("A", "ENABLED"), _m(2))]  # ниже single_campaign_min_spend
    assert _of_kind(_report(campaign_rows=tiny, totals=_m(2)), "single_campaign") == []


# ── фильтр по темам меню /advise ───────────────────────────────────────────────────
def test_topic_filter_maps_menu_topic_to_check_families():
    rep = _report(
        campaign_rows=[(("Solo", "ENABLED"), _m(150, clicks=50, conv=0))],
        keyword_rows=[(("Solo", "AG", "kw", "BROAD"), _m(20, clicks=10, conv=0))],
        ad_rows=[(("Solo", "AG", "1", "RSA"), _m(10, clicks=2, impressions=500))],
        totals=_m(150, clicks=50, impressions=1000, conv=0),
    )
    only_kw = _recs(rep, topics=["keywords"])
    assert only_kw and {c.topic for c in only_kw} == {"keywords"}
    all_topics = {c.topic for c in _recs(rep)}
    assert "keywords" in all_topics and "structure" in all_topics


# ── текст рекомендации: один рендер на /audit и /advise ─────────────────────────────
def test_body_is_rendered_for_every_kind():
    rep = _report(
        campaign_rows=[(("Solo", "ENABLED"), _m(150, clicks=50, conv=0))],
        keyword_rows=[(("Solo", "AG", "kw", "BROAD"), _m(20, clicks=10, conv=0))],
        ad_rows=[(("Solo", "AG", "1", "RSA"), _m(10, clicks=2, impressions=500))],
        totals=_m(150, clicks=50, impressions=1000, conv=0),
    )
    res = build_audit(rep)
    for lang in ("ru", "en"):
        for r in to_recommendations(res, lang, res.currency):
            assert r.body, f"пустой текст рекомендации {r.kind} ({lang})"
