"""Ф1: слой ставок и позиций — «какие слова поднять». Офлайн, без SDK/сети.

Пинуем три вещи, ради которых слой и сделан:
1. Совет «подними ставку» появляется ТОЛЬКО там, где ставку решает рекламодатель (ручные стратегии):
   на Smart Bidding cpc_bid ключа аукционом не управляет — совет был бы ложью.
2. Деньги: находки НЕ идут в «Под риском» (at_risk=0) — это упущенная выгода, а не потраченное;
   и НЕ дают кнопки «применить» (golden rule #3: ставка — только прямой командой через confirm-гейт).
3. Фетчер деградирует: если аккаунт не отдаёт доли верхних позиций на уровне ключа, ставки и оценки
   позиций всё равно читаются (второй запрос без этих метрик), а проверка по рангу молчит.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402
from reports import period as P  # noqa: E402
from reports import queries as Q  # noqa: E402
from reports.queries import (  # noqa: E402
    Breakdown,
    BidLandscapeRow,
    BidSimulation,
    BudgetSimulation,
    Metrics,
    SimPoint,
)

from audit.engine import ONE_TAP_OPS, build_audit  # noqa: E402
from audit.render import finding_text  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


def _report(campaign_cost: float = 300.0, conv: float = 10.0, currency: str = "USD"):
    """Аккаунт с расходом 300 и 10 конверсиями → средний CPA 30 (потолок для «приемлемого CPA»)."""
    totals = Metrics(
        impressions=5000, clicks=300, cost_micros=int(campaign_cost * 1_000_000), conversions=conv
    )
    rows = [
        (
            ("Search", "ENABLED"),
            Metrics(
                impressions=5000,
                clicks=300,
                cost_micros=int(campaign_cost * 1_000_000),
                conversions=conv,
            ),
        )
    ]
    bds = [Breakdown("campaign", "Кампании", ["Кампания", "Статус"], rows)]
    return SimpleNamespace(customer_id="123", totals=totals, breakdowns=bds, currency=currency)


def _bl(
    keyword: str = "ремонт окон",
    *,
    strategy: str = "MANUAL_CPC",
    bid: float = 0.5,
    fpc: float = 1.0,
    top: float = 2.0,
    cost: float = 50.0,
    clicks: int = 20,
    imps: int = 1000,
    conv: float = 0.0,
    top_is: float = 0.0,
    rank_lost_top: float = 0.0,
    ag: str = "11",
    crit: str = "22",
) -> BidLandscapeRow:
    return BidLandscapeRow(
        campaign="Search",
        ad_group="AG",
        ad_group_id=ag,
        criterion_id=crit,
        keyword=keyword,
        match_type="EXACT",
        strategy_type=strategy,
        bid=bid,
        first_page_cpc=fpc,
        top_of_page_cpc=top,
        first_position_cpc=top + 1.0,
        top_is=top_is,
        abs_top_is=0.0,
        rank_lost_top_is=rank_lost_top,
        metrics=Metrics(
            impressions=imps,
            clicks=clicks,
            cost_micros=int(cost * 1_000_000),
            conversions=conv,
        ),
    )


def _ids(res) -> set[str]:
    return {f.check_id for f in res.findings}


# ── Проверки движка ──────────────────────────────────────────────────────────────────
def test_bid_below_first_page_fires_on_manual_bidding():
    """Ставка 0.5 при оценке первой страницы 1.0 (ручная стратегия) → ключ почти не выходит на
    первую страницу. Находка несёт АДРЕС ключа (criterion_id/ad_group_id) — без него совет некуда
    применить — и целевую ставку из оценки Google, а не из головы."""
    res = build_audit(_report(), bid_landscape=[_bl(bid=0.5, fpc=1.0)])
    f = next(f for f in res.findings if f.check_id == "bid_below_first_page")
    assert f.family == "bidding" and f.severity == "warning"
    assert f.facts["bid"] == 0.5 and f.facts["target_bid"] == 1.0 and f.facts["uplift_pct"] == 100
    assert f.evidence["criterion_id"] == "22" and f.evidence["ad_group_id"] == "11"


def test_bid_advice_silent_on_smart_bidding():
    """Анти-ложноположительный гард: на Smart Bidding (tCPA/tROAS/Maximize*) ставку ключа Google
    игнорирует — «подними cpc_bid» там было бы ложью. Те же цифры, другая стратегия → тишина."""
    rows = [_bl(strategy="TARGET_CPA", bid=0.5, fpc=1.0, conv=3, cost=60)]
    assert not (
        _ids(build_audit(_report(), bid_landscape=rows))
        & {
            "bid_below_first_page",
            "bid_below_top_of_page",
        }
    )


def test_bid_below_top_of_page_needs_proven_value_and_acceptable_cpa():
    """Верх страницы советуем только тем, кто УЖЕ конвертит по приемлемому CPA (≤ средний по
    аккаунту 30). Дорогой ключ (CPA 100) — не «подними ставку», а совсем другой разговор."""
    good = _bl(keyword="окна пвх", bid=1.5, fpc=1.0, top=2.5, conv=2, cost=40)  # CPA 20 ≤ 30
    bad = _bl(keyword="окна дорого", bid=1.5, fpc=1.0, top=2.5, conv=1, cost=100)  # CPA 100 > 30
    res = build_audit(_report(), bid_landscape=[good, bad])
    hits = [f for f in res.findings if f.check_id == "bid_below_top_of_page"]
    assert [f.facts["keyword"] for f in hits] == ["окна пвх"]
    assert hits[0].severity == "info" and hits[0].facts["target_bid"] == 2.5


def test_bid_gap_below_threshold_is_noise_not_advice():
    """Разрыв ставка↔оценка < bid_gap_min (10%) — шум оценки Google, а не совет: 0.96 против 1.0."""
    res = build_audit(_report(), bid_landscape=[_bl(bid=0.96, fpc=1.0, top=1.02, conv=2, cost=40)])
    assert not (_ids(res) & {"bid_below_first_page", "bid_below_top_of_page"})


def test_bid_findings_are_upside_not_at_risk_and_never_one_tap():
    """Rule #3 + инвариант денег: находка про ставку не идёт в «Под риском» (это НЕ потраченное) и
    не даёт кнопки «применить» — только метка update_keyword_bid для замера эффекта."""
    res = build_audit(
        _report(),
        bid_landscape=[
            _bl(bid=0.5, fpc=1.0),
            _bl("окна пвх", bid=1.5, fpc=1.0, top=2.5, conv=2, cost=40),
        ],
    )
    bids = [f for f in res.findings if f.check_id.startswith("bid_below_")]
    assert len(bids) == 2
    for f in bids:
        assert f.at_risk == 0.0
        assert f.suggested_operation is None and not f.one_tap
        assert f.advice_operation == "update_keyword_bid"
        assert f.advice_operation not in ONE_TAP_OPS
    assert res.at_risk == 0.0  # апсайд не попадает в headline «под риском»


def test_top_is_rank_lost_aggregates_and_stays_silent_without_data():
    """Потеря ВЕРХА по рангу ≥ 30% у платящих ключей → один агрегат (сколько + худший). Доля 0.0 —
    это «не прочитано» (proto3-zero / деградация фетчера), а не «ноль потерь»: тогда молчим (GR8)."""
    rows = [
        _bl("окна", bid=2.0, fpc=1.0, top=1.5, cost=50, rank_lost_top=0.55),
        _bl("двери", bid=2.0, fpc=1.0, top=1.5, cost=30, rank_lost_top=0.35),
        _bl("шумный", bid=2.0, fpc=1.0, top=1.5, cost=1.0, rank_lost_top=0.9),  # ниже kw_min_spend
    ]
    f = next(
        f
        for f in build_audit(_report(), bid_landscape=rows).findings
        if f.check_id == "top_is_rank_lost"
    )
    assert f.facts["count"] == 2 and f.facts["worst_kw"] == "окна" and f.facts["worst_share"] == 55
    assert f.at_risk == 0.0 and f.suggested_operation is None

    quiet = [_bl("окна", bid=2.0, fpc=1.0, top=1.5, cost=50, rank_lost_top=0.0)]
    assert "top_is_rank_lost" not in _ids(build_audit(_report(), bid_landscape=quiet))


def test_no_bid_landscape_no_findings():
    """Сигнал не прочитан (фетчер упал → None) → проверки молчат, аудит не падает."""
    assert not (
        _ids(build_audit(_report(), bid_landscape=None))
        & {"bid_below_first_page", "bid_below_top_of_page", "top_is_rank_lost"}
    )


def test_bid_finding_text_ru_en_carries_numbers_and_rule3():
    """Текст находки — детерминированный (КОД, не модель): несёт ставку, целевую ставку Google и
    оговорку правила #3 («только по твоей команде»). Обе локали."""
    res = build_audit(_report(), bid_landscape=[_bl(bid=0.5, fpc=1.0)])
    f = next(f for f in res.findings if f.check_id == "bid_below_first_page")
    ru = finding_text(f, "ru", "USD")
    en = finding_text(f, "en", "USD")
    assert "0.50 USD" in ru and "1.00 USD" in ru and "команде" in ru
    assert "0.50 USD" in en and "1.00 USD" in en and "direct command" in en


# ── Фетчер (GAQL-контракт + деградация) ──────────────────────────────────────────────
def _gaql_row(*, with_is: bool = True):
    metrics = SimpleNamespace(
        impressions=1000,
        clicks=20,
        cost_micros=50_000_000,
        conversions=2.0,
        conversions_value=100.0,
    )
    if with_is:
        metrics.search_top_impression_share = 0.2
        metrics.search_absolute_top_impression_share = 0.05
        metrics.search_rank_lost_top_impression_share = 0.45
    return SimpleNamespace(
        campaign=SimpleNamespace(
            name="Search", bidding_strategy_type=SimpleNamespace(name="MANUAL_CPC")
        ),
        ad_group=SimpleNamespace(id=11, name="AG"),
        ad_group_criterion=SimpleNamespace(
            criterion_id=22,
            keyword=SimpleNamespace(text="ремонт окон", match_type=SimpleNamespace(name="EXACT")),
            effective_cpc_bid_micros=500_000,
            position_estimates=SimpleNamespace(
                first_page_cpc_micros=1_000_000,
                top_of_page_cpc_micros=2_000_000,
                first_position_cpc_micros=3_000_000,
            ),
        ),
        metrics=metrics,
    )


class _GA:
    """Фейковый GoogleAdsService: первый запрос (с долями верха) может «упасть», как на аккаунте,
    где эти метрики на уровне ключа недоступны."""

    def __init__(self, rows, fail_top_is: bool, seen: list[str]):
        self._rows, self._fail, self._seen = rows, fail_top_is, seen

    def search(self, *, customer_id, query):
        self._seen.append(query)
        if self._fail and "search_rank_lost_top_impression_share" in query:
            raise RuntimeError("field not selectable for this resource")
        return list(self._rows)


class _Client:
    def __init__(self, rows, fail_top_is: bool = False):
        self.seen: list[str] = []
        self._ga = _GA(rows, fail_top_is, self.seen)

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self._ga


def test_fetch_bid_landscape_reads_bids_estimates_and_top_is():
    p = P.last_n_days(7, today=date(2026, 6, 25))
    client = _Client([_gaql_row()])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        rows = Q.fetch_bid_landscape(client, DRAFT_ACCOUNT_ID, p)
    assert len(rows) == 1
    r = rows[0]
    assert r.bid == 0.5 and r.first_page_cpc == 1.0 and r.top_of_page_cpc == 2.0
    assert r.strategy_type == "MANUAL_CPC" and r.criterion_id == "22" and r.ad_group_id == "11"
    assert r.rank_lost_top_is == 0.45 and r.metrics.cost == 50.0
    q = client.seen[0]
    assert "FROM keyword_view" in q and "ad_group_criterion.position_estimates" in q
    assert "ad_group_criterion.status = 'ENABLED'" in q and "ORDER BY metrics.cost_micros DESC" in q


def _sim(amount, clicks, cost, conv, imps=1000, top=100) -> SimPoint:
    return SimPoint(
        amount=amount,
        clicks=clicks,
        cost=cost,
        impressions=imps,
        conversions=conv,
        top_slot_impressions=top,
    )


# Точки симулятора Google для ключа (11,22) при текущей ставке 0.5. Шаг 0.5→1.0 окупается
# (Δ40 / Δ2 конв = предельный CPA 20 ≤ 30 = средний по аккаунту), шаг 1.0→1.5 — нет (Δ50 / Δ1 = 50).
_BID_POINTS = [
    _sim(0.5, 20, 50.0, 2.0),
    _sim(1.0, 35, 90.0, 4.0),
    _sim(1.5, 45, 140.0, 5.0),
]


def test_sim_bid_upside_walks_marginal_steps_not_to_the_ceiling():
    """Главное свойство: идём по точкам, пока КАЖДЫЙ шаг окупается, и останавливаемся на первом
    неокупаемом. Совет «до 1.5» протащил бы вместе с прибыльным куском убыточный (предельный CPA 50
    при среднем 30) — движок обязан остановиться на 1.0."""
    sims = [BidSimulation(ad_group_id="11", criterion_id="22", points=_BID_POINTS)]
    res = build_audit(_report(), bid_landscape=[_bl(bid=0.5)], bid_simulations=sims)
    f = next(f for f in res.findings if f.check_id == "sim_bid_upside")
    assert f.facts["target_bid"] == 1.0 and f.facts["uplift_pct"] == 100
    assert f.facts["add_conversions"] == 2.0 and f.facts["add_cost"] == 40.0
    assert f.facts["marginal_cpa"] == 20.0
    assert f.family == "bidding" and f.severity == "info"
    assert f.at_risk == 0.0 and f.suggested_operation is None and not f.one_tap
    assert f.advice_operation == "update_keyword_bid" and f.advice_operation not in ONE_TAP_OPS


def test_sim_bid_upside_silent_without_cpa_ceiling():
    """Конверсий в аккаунте нет и цели нет → потолка окупаемости нет. «Поднимай» без потолка — это
    про чужие деньги: молчим (fail-closed), хотя точки симулятора и обещают рост."""
    sims = [BidSimulation(ad_group_id="11", criterion_id="22", points=_BID_POINTS)]
    res = build_audit(_report(conv=0.0), bid_landscape=[_bl(bid=0.5)], bid_simulations=sims)
    assert "sim_bid_upside" not in _ids(res)


def test_sim_bid_upside_silent_on_smart_bidding_and_without_landscape():
    """Симулятор есть, но ставкой ключа управляет не рекламодатель (tCPA) → тишина. И наоборот: без
    строки bid_landscape склеить симулятор не с чем (нет ни ставки, ни стратегии) → тоже тишина."""
    sims = [BidSimulation(ad_group_id="11", criterion_id="22", points=_BID_POINTS)]
    smart = build_audit(
        _report(), bid_landscape=[_bl(strategy="TARGET_CPA", bid=0.5)], bid_simulations=sims
    )
    assert "sim_bid_upside" not in _ids(smart)
    assert "sim_bid_upside" not in _ids(build_audit(_report(), bid_simulations=sims))


def test_sim_gain_below_threshold_is_noise():
    """Прирост < sim_min_conv_gain (0.5 конв) — шум прогноза, не совет."""
    tiny = [_sim(0.5, 20, 50.0, 2.0), _sim(1.0, 22, 55.0, 2.3)]
    sims = [BidSimulation(ad_group_id="11", criterion_id="22", points=tiny)]
    res = build_audit(_report(), bid_landscape=[_bl(bid=0.5)], bid_simulations=sims)
    assert "sim_bid_upside" not in _ids(res)


def test_sim_budget_upside_uses_google_numbers_and_stays_advice_only():
    """Бюджет: шаг 10→20 окупается (Δ90 / Δ3 конв = 30 = потолок), шаг 20→30 — нет (Δ90 / Δ0.5 = 180).
    Бюджет — деньги ⇒ ни at_risk, ни кнопки: только прозой и по прямой команде (rule #3)."""
    pts = [
        _sim(10.0, 200, 280.0, 8.0),
        _sim(20.0, 300, 370.0, 11.0),
        _sim(30.0, 340, 460.0, 11.5),
    ]
    sims = [BudgetSimulation(campaign_id="7", campaign="Search", current_budget=10.0, points=pts)]
    res = build_audit(_report(), budget_simulations=sims)
    f = next(f for f in res.findings if f.check_id == "sim_budget_upside")
    assert f.family == "budget" and f.facts["target_budget"] == 20.0
    assert f.facts["add_conversions"] == 3.0 and f.facts["marginal_cpa"] == 30.0
    assert f.at_risk == 0.0 and f.suggested_operation is None and not f.one_tap
    assert f.advice_operation == "update_budget" and f.advice_operation not in ONE_TAP_OPS
    ru = finding_text(f, "ru", "USD")
    assert "20.00 USD" in ru and "+3.0 конв" in ru and "команде" in ru
    assert "+3.0 conversions" in finding_text(f, "en", "USD")


def test_sim_absence_is_not_a_data_gap():
    """Симулятора нет — это НОРМА (Google строит его только при достаточных данных), а не пробел
    чтения: аудит не должен объявлять семьи bidding/budget «непрочитанными»."""
    res = build_audit(_report(), bid_landscape=[_bl(bid=0.5)], bid_simulations=[])
    assert not (_ids(res) & {"sim_bid_upside", "sim_budget_upside"})
    assert "bid_below_first_page" in _ids(res)  # остальной слой работает как ни в чём не бывало


class _Routed:
    """Фейковый клиент с маршрутизацией по ресурсу в FROM (симулятор + справочник кампаний)."""

    def __init__(self, by_resource: dict):
        self.seen: list[str] = []
        self._map = by_resource

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self

    def search(self, *, customer_id, query):
        self.seen.append(query)
        for res, rows in self._map.items():
            if f"FROM {res}" in query:
                return list(rows)
        return []


def test_fetch_bid_simulations_contract():
    """CPC_BID + UNIFORM: только так точки несут АБСОЛЮТНУЮ ставку, из SCALING конкретной ставки
    не назовёшь. Точки приходят отсортированными по ставке (движок идёт по ним снизу вверх)."""
    row = SimpleNamespace(
        ad_group_criterion_simulation=SimpleNamespace(
            ad_group_id=11,
            criterion_id=22,
            cpc_bid_point_list=SimpleNamespace(
                points=[
                    SimpleNamespace(
                        cpc_bid_micros=1_000_000,
                        clicks=35,
                        cost_micros=90_000_000,
                        impressions=2000,
                        biddable_conversions=4.0,
                        top_slot_impressions=500,
                    ),
                    SimpleNamespace(
                        cpc_bid_micros=500_000,
                        clicks=20,
                        cost_micros=50_000_000,
                        impressions=1000,
                        biddable_conversions=2.0,
                        top_slot_impressions=100,
                    ),
                ]
            ),
        )
    )
    client = _Routed({"ad_group_criterion_simulation": [row]})
    with allowed_ids(DRAFT_ACCOUNT_ID):
        sims = Q.fetch_bid_simulations(client, DRAFT_ACCOUNT_ID)
    q = client.seen[0]
    assert "FROM ad_group_criterion_simulation" in q
    assert "type = 'CPC_BID'" in q and "modification_method = 'UNIFORM'" in q
    assert [p.amount for p in sims[0].points] == [0.5, 1.0]  # отсортированы
    assert sims[0].points[0].cost == 50.0 and sims[0].points[1].conversions == 4.0


def test_fetch_budget_simulations_joins_current_budget_and_name():
    """Симулятор бюджета знает только campaign_id и точки — без «где мы сейчас» прирост считать не
    от чего. Фетчер вторым запросом добирает имя кампании и её ТЕКУЩИЙ дневной бюджет."""
    sim_row = SimpleNamespace(
        campaign_simulation=SimpleNamespace(
            campaign_id=7,
            budget_point_list=SimpleNamespace(
                points=[
                    SimpleNamespace(
                        budget_amount_micros=10_000_000,
                        clicks=200,
                        cost_micros=280_000_000,
                        impressions=9000,
                        biddable_conversions=8.0,
                        top_slot_impressions=3000,
                    )
                ]
            ),
        )
    )
    camp_row = SimpleNamespace(
        campaign=SimpleNamespace(id=7, name="Search"),
        campaign_budget=SimpleNamespace(amount_micros=10_000_000),
    )
    client = _Routed({"campaign_simulation": [sim_row], "campaign ": [camp_row]})
    with allowed_ids(DRAFT_ACCOUNT_ID):
        sims = Q.fetch_budget_simulations(client, DRAFT_ACCOUNT_ID)
    assert len(client.seen) == 2 and "campaign_budget.amount_micros" in client.seen[1]
    assert sims[0].campaign == "Search" and sims[0].current_budget == 10.0
    assert sims[0].points[0].amount == 10.0


def test_fetch_budget_simulations_skips_second_query_when_no_sims():
    """Симуляторов нет → справочник кампаний не читаем вовсе (лишний запрос к API — тоже цена)."""
    client = _Routed({"campaign_simulation": []})
    with allowed_ids(DRAFT_ACCOUNT_ID):
        assert Q.fetch_budget_simulations(client, DRAFT_ACCOUNT_ID) == []
    assert len(client.seen) == 1


def test_fetch_bid_landscape_degrades_without_keyword_top_is():
    """Доли верха на уровне ключа отвергнуты сервером → ставки/оценки позиций читаем ВСЁ РАВНО
    (вторым запросом без них), доли остаются 0.0 → проверка по рангу промолчит, а не соврёт."""
    p = P.last_n_days(7, today=date(2026, 6, 25))
    client = _Client([_gaql_row(with_is=False)], fail_top_is=True)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        rows = Q.fetch_bid_landscape(client, DRAFT_ACCOUNT_ID, p)
    assert len(client.seen) == 2  # первый с долями упал, второй — без них
    assert "search_rank_lost_top_impression_share" not in client.seen[1]
    assert rows[0].bid == 0.5 and rows[0].first_page_cpc == 1.0
    assert rows[0].rank_lost_top_is == 0.0  # нет данных → 0.0, чек по рангу молчит
    assert "top_is_rank_lost" not in _ids(build_audit(_report(), bid_landscape=rows))


# ── Доска /bids (bidscape.board): один источник с чеками, ранг — по прогнозу конверсий ───
def _board(rows, sims=None, **kw):
    from audit.bidscape import board

    kw.setdefault("acct_cpa", 30.0)  # средний CPA аккаунта из _report()
    kw.setdefault("min_cost", 1.0)
    return board(rows, sims, **kw)


def test_board_ranks_sim_forecast_above_position_gap():
    """Ставку поднимают ради КОНВЕРСИЙ: ключ, которому симулятор Google обещает прирост, обязан быть
    выше ключа, у которого есть только разрыв до оценки позиции (прироста Google там не обещал)."""
    gap_only = _bl("двери", bid=0.5, fpc=1.0, cost=90.0, ag="11", crit="99")
    with_sim = _bl("окна", bid=0.5, fpc=1.0, cost=10.0, ag="11", crit="22")
    items = _board([gap_only, with_sim], [BidSimulation("11", "22", _BID_POINTS)])
    assert [i.keyword for i in items] == ["окна", "двери"]
    top = items[0]
    assert top.source == "sim" and top.add_conversions == 2.0 and top.target_bid == 1.0
    assert items[1].source == "first_page" and items[1].add_conversions == 0.0  # 0.0 = «неизвестно»


def test_board_takes_simulator_bid_over_position_estimate():
    """Есть оба сигнала → рекомендуем ставку СИМУЛЯТОРА (она ограничена окупаемостью), а не оценку
    позиции: иначе совет «подними до 1.0 (первая страница)» протащил бы неокупаемый шаг."""
    row = _bl("окна", bid=0.5, fpc=1.4, cost=50.0)
    items = _board([row], [BidSimulation("11", "22", _BID_POINTS)])
    assert len(items) == 1 and items[0].source == "sim"
    assert items[0].target_bid == 1.0 and items[0].uplift_pct == 100  # 0.5 → 1.0, а не → 1.4


def test_board_silent_on_smart_bidding_and_without_signals():
    """Smart Bidding: cpc_bid ключа аукционом не управляет → в доску не попадает даже с симулятором.
    Нет ни разрыва, ни окупаемого прогноза → строки нет вовсе (молчание честнее выдуманного совета)."""
    smart = _bl("окна", strategy="TARGET_CPA", bid=0.5, fpc=1.0)
    assert _board([smart], [BidSimulation("11", "22", _BID_POINTS)]) == []
    no_gap = _bl("окна", bid=2.0, fpc=1.0, top=1.5, cost=50.0, ag="11", crit="77")
    assert _board([no_gap]) == []


def test_board_caps_top_n():
    rows = [_bl(f"kw{i}", bid=0.5, fpc=1.0, cost=10.0 + i, crit=str(100 + i)) for i in range(5)]
    assert len(_board(rows, top_n=3)) == 3


def test_bids_card_shows_google_numbers_and_no_button_hint():
    """Карточка /bids: цифры Google + ГОТОВАЯ ФРАЗА команды вместо кнопки — ставка меняется только
    прямой командой через подтверждение (golden rule #3)."""
    from core import texts

    items = _board(
        [_bl("окна", bid=0.5, fpc=1.0, cost=10.0)], [BidSimulation("11", "22", _BID_POINTS)]
    )
    s = texts.fmt_bids(items, currency="USD", period_label="30 дн.")
    assert "окна" in s and "0.50 → <b>1.00</b> USD (+100%)" in s
    assert "Прогноз Google: <b>+2</b> конв." in s and "симулятор Google" in s
    assert "подними ставку ключа" in s and "подтверждения" in s  # вместо кнопки — фраза команды
    en = texts.fmt_bids(items, currency="USD", lang="en")
    assert "Google simulator" in en and "Every change goes through confirmation" in en
