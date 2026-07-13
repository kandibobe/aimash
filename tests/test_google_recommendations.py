"""Ф2: рекомендации Google С ЕГО ЖЕ оценкой эффекта (recommendation.impact). Офлайн, без SDK/сети.

Пинуем ровно то, ради чего фаза сделана:
1. Фетчер БЕРЁТ impact-метрики (докстринг «в v24 недоступны» был неверен — поля есть в протобуфе
   RecommendationImpact/RecommendationMetrics) и деградирует, если сервер их отвергнет.
2. Прирост показываем только там, где Google его ОЦЕНИЛ: нули impact = «не оценено», а не «нуль».
3. 🔒 Мнение Google НЕ двигает наш балл (score_intensity=0, семья вне FAMILY_WEIGHT) и НЕ даёт
   кнопку «применить»: часть рекомендаций меняет бюджет/ставки ⇒ только прямая команда через гейт.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402
from reports import queries as Q  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402

from audit.engine import ONE_TAP_OPS, build_audit  # noqa: E402
from audit.render import finding_text, render_audit  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


def _report(cost: float = 300.0, conv: float = 10.0):
    totals = Metrics(
        impressions=5000, clicks=300, cost_micros=int(cost * 1_000_000), conversions=conv
    )
    rows = [
        (
            ("Search", "ENABLED"),
            Metrics(
                impressions=5000, clicks=300, cost_micros=int(cost * 1_000_000), conversions=conv
            ),
        )
    ]
    return SimpleNamespace(
        customer_id="123",
        totals=totals,
        breakdowns=[Breakdown("campaign", "Кампании", ["Кампания", "Статус"], rows)],
        currency="USD",
    )


def _rec(
    type_: str = "KEYWORD",
    *,
    campaign: str = "Search",
    base_conv: float = 10.0,
    pot_conv: float = 22.0,
    base_cost: float = 100.0,
    pot_cost: float = 140.0,
    dismissed: bool = False,
) -> Q.RecommendationRow:
    return Q.RecommendationRow(
        type=type_,
        dismissed=dismissed,
        campaign=campaign,
        resource_name=f"customers/123/recommendations/{type_}",
        base_conversions=base_conv,
        potential_conversions=pot_conv,
        base_cost=base_cost,
        potential_cost=pot_cost,
    )


# ── Фетчер: impact есть в v24 ────────────────────────────────────────────────────────
def _gaql_row(with_impact: bool = True):
    rec = SimpleNamespace(
        type=SimpleNamespace(name="KEYWORD"),
        dismissed=False,
        resource_name="customers/123/recommendations/KW1",
        impact=SimpleNamespace(
            base_metrics=SimpleNamespace(conversions=10.0, cost_micros=100_000_000, clicks=200),
            potential_metrics=SimpleNamespace(
                conversions=22.0, cost_micros=140_000_000, clicks=260
            ),
        )
        if with_impact
        else None,
    )
    return SimpleNamespace(recommendation=rec, campaign=SimpleNamespace(name="Search"))


class _Client:
    """Фейковый GoogleAdsService: по флагу отвергает запрос с impact (как сервер на аккаунте, где
    поля недоступны) — фетчер обязан повторить БЕЗ них, а не уронить чтение целиком."""

    def __init__(self, rows, fail_impact: bool = False):
        self._rows, self._fail = rows, fail_impact
        self.seen: list[str] = []

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self

    def search(self, *, customer_id, query):
        self.seen.append(query)
        if self._fail and "recommendation.impact" in query:
            raise RuntimeError("field not selectable")
        return list(self._rows)


def test_fetch_recommendations_reads_impact_metrics():
    """Готовый ответ Google на «что оптимизировать» — с цифрами: +12 конв. за +40. Деньги делит КОД."""
    client = _Client([_gaql_row()])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        rows = Q.fetch_recommendations(client, DRAFT_ACCOUNT_ID)
    assert len(rows) == 1
    r = rows[0]
    assert r.type == "KEYWORD" and r.campaign == "Search" and not r.dismissed
    assert r.add_conversions == 12.0 and r.add_cost == 40.0 and r.add_clicks == 60.0
    q = client.seen[0]
    assert "recommendation.impact.potential_metrics.conversions" in q
    assert "FROM recommendation" in q


def test_fetch_recommendations_degrades_without_impact():
    """Сервер отверг impact → типы рекомендаций читаем ВСЁ РАВНО, прирост 0.0 = «не оценено» (gr8)."""
    client = _Client([_gaql_row(with_impact=False)], fail_impact=True)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        rows = Q.fetch_recommendations(client, DRAFT_ACCOUNT_ID)
    assert len(client.seen) == 2 and "recommendation.impact" not in client.seen[1]
    assert rows[0].type == "KEYWORD" and rows[0].add_conversions == 0.0


# ── Чек: второе мнение с цифрами, но БЕЗ влияния на наш балл ──────────────────────────
def test_check_sums_googles_own_forecast_and_ranks_by_conversions():
    recs = [
        _rec("KEYWORD", pot_conv=22.0),  # +12 конв.
        _rec("CAMPAIGN_BUDGET", base_conv=5.0, pot_conv=9.0, base_cost=50.0, pot_cost=90.0),  # +4
        _rec(
            "CALLOUT_ASSET", base_conv=0.0, pot_conv=0.0, base_cost=0.0, pot_cost=0.0
        ),  # не оценён
        _rec("SITELINK_ASSET", dismissed=True),  # отклонённая — не в счёт
    ]
    f = next(
        f
        for f in build_audit(_report(), recommendations=recs).findings
        if f.check_id == "google_recommendations_pending"
    )
    assert f.facts["count"] == 3  # dismissed исключена
    assert f.facts["add_conversions"] == 16.0 and f.facts["add_cost"] == 80.0
    assert [t["type"] for t in f.facts["top"]] == ["KEYWORD", "CAMPAIGN_BUDGET", "CALLOUT_ASSET"]
    assert f.facts["top"][2]["add_conversions"] == 0.0  # «Google не оценил», а не «ноль эффекта»


def test_google_opinion_never_touches_our_score_or_buttons():
    """🔒 Балл считает НАШ код по нашим правилам: чужая модель, которую мы не можем ни объяснить, ни
    проверить, не штрафует клиента. И кнопки «применить» нет — бюджет/ставки только прямой командой."""
    plain = build_audit(_report())
    with_recs = build_audit(_report(), recommendations=[_rec(), _rec("CAMPAIGN_BUDGET")])
    assert with_recs.score == plain.score and with_recs.grade == plain.grade
    f = next(f for f in with_recs.findings if f.check_id == "google_recommendations_pending")
    assert f.at_risk == 0.0 and f.score_intensity == 0.0
    assert f.suggested_operation is None and not f.one_tap
    assert f.suggested_operation not in ONE_TAP_OPS
    assert with_recs.families["recommendations"]["penalty"] == 0.0


def test_check_silent_when_recommendations_absent_or_all_dismissed():
    """Нет данных / всё отклонено → находки нет вовсе (пустая карточка честнее выдуманной)."""
    assert not [
        f for f in build_audit(_report()).findings if f.check_id == "google_recommendations_pending"
    ]
    only_dismissed = build_audit(_report(), recommendations=[_rec(dismissed=True)])
    assert "google_recommendations_pending" not in {f.check_id for f in only_dismissed.findings}


# ── Рендер: конкретика вместо «3 типа» ───────────────────────────────────────────────
def test_render_shows_googles_numbers_not_just_types():
    res = build_audit(
        _report(), recommendations=[_rec("KEYWORD"), _rec("CAMPAIGN_BUDGET", pot_conv=14.0)]
    )
    f = next(f for f in res.findings if f.check_id == "google_recommendations_pending")
    line = finding_text(f, "ru", "USD")
    assert "+16" in line and "Keyword" in line and "подтверждение" in line
    en = finding_text(f, "en", "USD")
    assert "+16" in en and "confirmation" in en
    card = render_audit(res, "ru")
    assert "Google оценивает это в +16 конв." in card  # вместо прежнего «Google советует: 3 типа»


def test_render_stays_silent_about_gain_when_google_did_not_estimate_it():
    """impact пуст (Google не оценил) → в шапке только типы, без выдуманного «+0 конверсий»."""
    res = build_audit(
        _report(),
        recommendations=[_rec("KEYWORD", base_conv=0.0, pot_conv=0.0, base_cost=0.0, pot_cost=0.0)],
    )
    card = render_audit(res, "ru")
    assert "Google советует: Keyword" in card and "оценивает" not in card
