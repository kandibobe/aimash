"""Ф5а: конкурентное давление — суррогат по данным аукциона (имён конкурентов Google не отдаёт).

Что пинуется:
1. Молчание там, где давления НЕТ: забираешь своё / упёрся в бюджет / данных мало / доли не сходятся.
   Находка «конкуренты давят» на здоровом аккаунте — вранье, которое читателю дороже, чем её отсутствие.
2. Взвешивание по показам: кампания на 100 показов не должна тянуть картину аккаунта наравне с кампанией
   на 10 000. Среднее арифметическое здесь дало бы уверенно неверную цифру.
3. Балл НЕ трогается: ранг/бюджет уже штрафуют is_rank_constrained/is_budget_constrained/is_lost_revenue —
   агрегат поверх них посчитал бы одну болезнь дважды (тот же класс, что задвоение at_risk в эпохе 4).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reports.queries import Breakdown, ImpressionShareRow, Metrics  # noqa: E402

from audit.engine import build_audit  # noqa: E402
from audit.render import finding_text  # noqa: E402


def _report(campaigns: list[tuple[str, int, float, float]]):
    """campaigns = [(имя, показы, расход, конверсии)]."""
    rows = [
        (
            (name, "ENABLED"),
            Metrics(
                impressions=imp,
                clicks=max(1, imp // 20),
                cost_micros=int(cost * 1_000_000),
                conversions=conv,
            ),
        )
        for name, imp, cost, conv in campaigns
    ]
    totals = Metrics(
        impressions=sum(c[1] for c in campaigns),
        clicks=sum(max(1, c[1] // 20) for c in campaigns),
        cost_micros=int(sum(c[2] for c in campaigns) * 1_000_000),
        conversions=sum(c[3] for c in campaigns),
    )
    return SimpleNamespace(
        customer_id="123",
        totals=totals,
        breakdowns=[Breakdown("campaign", "Кампании", ["Кампания", "Статус"], rows)],
        currency="USD",
    )


def _is_row(name: str, *, share: float, budget: float, rank: float, channel: str = "SEARCH"):
    return ImpressionShareRow(
        campaign_id="1",
        campaign_name=name,
        channel_type=channel,
        search_is=share,
        budget_lost_is=budget,
        rank_lost_is=rank,
    )


def _kw(
    text: str,
    *,
    impressions: int,
    cost: float,
    bid: float = 0.0,
    top_cpc: float = 0.0,
    top_is: float = 0.0,
):
    return SimpleNamespace(
        campaign="Search",
        ad_group="Общая",
        ad_group_id="1",
        criterion_id="2",
        keyword=text,
        match_type="EXACT",
        strategy_type="MANUAL_CPC",
        bid=bid,
        first_page_cpc=0.0,
        top_of_page_cpc=top_cpc,
        first_position_cpc=0.0,
        top_is=top_is,
        abs_top_is=0.0,
        rank_lost_top_is=0.0,
        metrics=Metrics(impressions=impressions, clicks=10, cost_micros=int(cost * 1_000_000)),
    )


def _ids(res) -> set[str]:
    return {f.check_id for f in res.findings}


def _find(res, cid: str):
    return next(f for f in res.findings if f.check_id == cid)


def test_pressure_is_weighted_by_impressions_not_averaged():
    """Малая кампания с чудовищным rank_lost НЕ должна перекрашивать картину аккаунта: 9000 показов
    против 1000. Арифметическое среднее дало бы 55% потери по рангу, взвешенное — 24%."""
    report = _report([("Большая", 9000, 400.0, 20.0), ("Малая", 1000, 40.0, 1.0)])
    is_rows = [
        _is_row("Большая", share=0.50, budget=0.30, rank=0.20),
        _is_row("Малая", share=0.05, budget=0.05, rank=0.90),
    ]
    f = _find(build_audit(report, is_rows=is_rows), "competitive_pressure")
    assert f.facts["share"] == 46  # (0.50·9000 + 0.05·1000)/10000
    assert f.facts["rank_lost"] == 27  # (0.20·9000 + 0.90·1000)/10000 — а не среднее 55
    assert f.facts["budget_lost"] == 28  # (0.30·9000 + 0.05·1000)/10000 = 27.5 → 28
    assert f.facts["campaigns"] == 2


def test_pressure_never_touches_the_score_or_the_money():
    """🔒 Ранг/бюджет уже штрафуют другие чеки. Агрегат — диагноз: at_risk=0, вклада в балл нет.
    Балл аккаунта обязан совпасть с баллом того же аккаунта, посчитанного без IS-строк."""
    report = _report([("Search", 9000, 400.0, 20.0)])
    is_rows = [_is_row("Search", share=0.30, budget=0.20, rank=0.50)]
    with_is = build_audit(report, is_rows=is_rows)
    f = _find(with_is, "competitive_pressure")
    assert (f.at_risk, f.score_intensity, f.suggested_operation) == (0.0, 0.0, None)
    assert f.family == "competition"  # семья ВНЕ FAMILY_WEIGHT → вес 0
    # Штраф идёт только от is_rank_constrained (семья rsa) — вклад competition-семьи ровно нулевой.
    assert with_is.families.get("competition", {}).get("penalty", 0.0) == 0.0


def test_pressure_silent_when_you_take_your_share_or_lose_to_budget_only():
    report = _report([("Search", 9000, 400.0, 20.0)])
    healthy = [_is_row("Search", share=0.85, budget=0.05, rank=0.10)]  # забираешь своё
    budget_bound = [_is_row("Search", share=0.40, budget=0.55, rank=0.05)]  # упёрся в СВОИ деньги
    assert "competitive_pressure" not in _ids(build_audit(report, is_rows=healthy))
    assert "competitive_pressure" not in _ids(build_audit(report, is_rows=budget_bound))
    # …и это не «мы ничего не заметили»: бюджетное ограничение ловит СВОЙ чек (кампания конвертит →
    # это упущенная выручка, is_lost_revenue; без конверсий был бы is_budget_constrained).
    assert "is_lost_revenue" in _ids(build_audit(report, is_rows=budget_bound))


def test_pressure_silent_on_thin_or_incomplete_data():
    """Доли не сходятся к 1.0 (proto3-zero = «не прочитано») и мало показов → тишина, а не «нет конкурентов»."""
    incomplete = [_is_row("Search", share=0.0, budget=0.0, rank=0.0)]
    assert "competitive_pressure" not in _ids(
        build_audit(_report([("Search", 9000, 400.0, 20.0)]), is_rows=incomplete)
    )
    thin = _report([("Search", 100, 20.0, 1.0)])  # 100 показов — это не рынок
    assert "competitive_pressure" not in _ids(
        build_audit(thin, is_rows=[_is_row("Search", share=0.20, budget=0.10, rank=0.70)])
    )


def test_pressure_verdict_names_the_culprit():
    report = _report([("Search", 9000, 400.0, 20.0)])
    rank = build_audit(report, is_rows=[_is_row("Search", share=0.30, budget=0.20, rank=0.50)])
    budget = build_audit(report, is_rows=[_is_row("Search", share=0.30, budget=0.45, rank=0.25)])
    assert _find(rank, "competitive_pressure").facts["verdict"] == "rank"
    assert _find(budget, "competitive_pressure").facts["verdict"] == "budget"
    ru = finding_text(_find(rank, "competitive_pressure"), "ru", "USD")
    assert "РАНГЕ" in ru and "30%" in ru
    assert "RANK" in finding_text(_find(rank, "competitive_pressure"), "en", "USD")


def test_pressure_adds_top_of_page_facts_only_when_that_layer_was_read():
    report = _report([("Search", 9000, 400.0, 20.0)])
    is_rows = [_is_row("Search", share=0.30, budget=0.20, rank=0.50)]
    kws = [
        _kw("ноутбук", impressions=8000, cost=100.0, bid=1.0, top_cpc=2.5, top_is=0.25),
        _kw("ноутбук asus", impressions=2000, cost=50.0, bid=3.0, top_cpc=2.0, top_is=0.75),
        _kw(
            "копейка", impressions=500, cost=0.5, bid=0.1, top_cpc=2.0
        ),  # не платящий → не в «underbid»
    ]
    f = _find(build_audit(report, is_rows=is_rows, bid_landscape=kws), "competitive_pressure")
    # Ключ, который НИКОГДА не был наверху, честно разбавляет долю (0 в числителе, показы — в
    # знаменателе): 3500/10500 = 33%. Считать только «верхние» ключи = завысить картину.
    assert f.facts["top_is"] == 33
    assert (f.facts["underbid"], f.facts["paying"]) == (
        1,
        2,
    )  # ставка ниже верха — только у первого
    assert "верху страницы" in finding_text(f, "ru", "USD") or "Наверху" in finding_text(
        f, "ru", "USD"
    )

    # Слой долей верха не прочитан (фетчер деградировал → все нули) — молчим о верхе, а не пишем «0%».
    blind = [_kw("ноутбук", impressions=8000, cost=100.0, bid=1.0, top_cpc=2.5)]
    g = _find(build_audit(report, is_rows=is_rows, bid_landscape=blind), "competitive_pressure")
    assert "top_is" not in g.facts and g.facts["paying"] == 1
