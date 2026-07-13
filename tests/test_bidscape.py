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
from reports.queries import Breakdown, BidLandscapeRow, Metrics  # noqa: E402

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
) -> BidLandscapeRow:
    return BidLandscapeRow(
        campaign="Search",
        ad_group="AG",
        ad_group_id="11",
        criterion_id="22",
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
