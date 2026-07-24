"""Замечание 2 (2026-07-17): инварианты SELECT-клаузы GAQL v24 для фетчеров /audit.

Четыре фетчера падали на КАЖДОМ живом прогоне (половина семей аудита «слепла»), потому что
v24 отвергал их запросы. Правила добыты живыми пробами + метаданными GoogleAdsFieldService
(не из доки — там этого нет явно):

1. Для geographic_view / campaign_asset / ad_group_asset / user_location_view / asset_group
   фильтр `campaign.status` в WHERE требует `campaign.status` в SELECT
   («must be present in SELECT clause»). Требование ВИД-специфично: FROM campaign,
   ad_group_ad, keyword_view так НЕ требуют — поэтому инвариант держим списком view,
   а не «для всех подряд» (иначе гард сам станет ложной догмой).
2. Композит `asset_group.asset_coverage` в SELECT не допускается (selectable=False);
   selectable — лист `.ad_strength_action_items`.
3. У recommendation наоборот: selectable — КОМПОЗИТ `recommendation.impact`; leaf-пути
   `impact.{base,potential}_metrics.*` — «Unrecognized field». Молчаливая деградация
   в fetch_recommendations маскировала это: impact тихо выкидывался на каждом прогоне.

Тест ловит регрессию офлайн: шпион-клиент записывает все запросы фетчеров, ассерты — по
разобранным SELECT/FROM/WHERE. Живая сверка — scratchpad-проба или /verify-live.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402
from reports.queries import (  # noqa: E402
    _recs_query,
    fetch_campaign_assets,
    fetch_campaign_settings,
    fetch_geo_waste,
    fetch_pmax_asset_groups,
)


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


class _SpyClient:
    """Записывает каждый GAQL-запрос, отвечает пустотой: нам нужен ТЕКСТ запроса, не данные."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def get_service(self, _name: str):
        spy = self

        class _Svc:
            def search(self, customer_id: str, query: str):
                spy.queries.append(query)
                return []

        return _Svc()


def _parse(q: str) -> tuple[list[str], str, str]:
    """(поля SELECT, ресурс FROM, хвост после WHERE — м.б. с ORDER BY/LIMIT, для substring-чека)."""
    head, _, rest = q.partition(" FROM ")
    fields = [f.strip() for f in head.removeprefix("SELECT ").split(",")]
    resource = rest.split()[0]
    where = rest.partition(" WHERE ")[2]
    return fields, resource, where


def _collect_fetcher_queries() -> list[str]:
    spy = _SpyClient()
    period = SimpleNamespace(
        gaql_between=lambda: "segments.date BETWEEN '2026-01-01' AND '2026-01-31'"
    )
    with allowed_ids(DRAFT_ACCOUNT_ID):
        fetch_geo_waste(spy, DRAFT_ACCOUNT_ID, period)
        fetch_campaign_assets(spy, DRAFT_ACCOUNT_ID)
        fetch_campaign_settings(spy, DRAFT_ACCOUNT_ID, period)
        # pmax: при пустом ответе выходит после ПЕРВОГО запроса — он и был битым (композит+status).
        fetch_pmax_asset_groups(spy, DRAFT_ACCOUNT_ID, period)
    assert len(spy.queries) >= 8, f"шпион поймал слишком мало запросов: {spy.queries}"
    return spy.queries


# View, где живая проба подтвердила: фильтр campaign.status без него же в SELECT отвергается.
_STATUS_IN_SELECT_VIEWS = frozenset(
    {"geographic_view", "campaign_asset", "ad_group_asset", "user_location_view", "asset_group"}
)


def test_status_filter_requires_status_in_select_for_known_views():
    for q in _collect_fetcher_queries():
        fields, resource, where = _parse(q)
        if resource in _STATUS_IN_SELECT_VIEWS and "campaign.status" in where:
            assert "campaign.status" in fields, (
                f"v24 отвергнет запрос ({resource}): campaign.status в WHERE, но не в SELECT:\n{q}"
            )


def test_no_composite_asset_coverage_in_select():
    for q in _collect_fetcher_queries():
        fields, _, _ = _parse(q)
        assert "asset_group.asset_coverage" not in fields, (
            f"композит asset_coverage не selectable в v24 (нужен лист .ad_strength_action_items):\n{q}"
        )


def test_recs_query_uses_impact_composite_not_leaf_paths():
    q = _recs_query(50, with_impact=True)
    fields, resource, _ = _parse(q)
    assert resource == "recommendation"
    assert "recommendation.impact" in fields, (
        "impact-композит выкинут — прирост опять станет «не оценено»"
    )
    for f in fields:
        assert not f.startswith("recommendation.impact."), (
            f"leaf-путь impact.* — «Unrecognized field» в v24: {f}"
        )
    # Без impact запрос остаётся валидным (ветка деградации).
    assert "recommendation.impact" not in _recs_query(50, with_impact=False)
