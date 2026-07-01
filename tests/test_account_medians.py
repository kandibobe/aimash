"""Офлайн-тесты §19.3: медианы прошлых Search-кампаний (ads.read.search_campaign_medians).

Без живого Google Ads — query-aware фейковый SDK (3 разных GAQL: бюджеты/CPC/match_type).
Проверяем медиану бюджета, средний CPC = cost/clicks, моду типа соответствия, замок чтения и
деградацию на пустом аккаунте.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.read import search_campaign_medians  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


class _QueryAwareGA:
    """Фейковый GoogleAdsService: возвращает разные строки в зависимости от текста запроса."""

    def __init__(self, *, budgets, cpc_rows, mt_rows):
        self._budgets = budgets
        self._cpc = cpc_rows
        self._mt = mt_rows

    def search(self, *, customer_id, query):
        if "campaign_budget.amount_micros" in query:
            return [
                SimpleNamespace(campaign_budget=SimpleNamespace(amount_micros=b))
                for b in self._budgets
            ]
        if "metrics.cost_micros" in query and "campaign" in query:
            return [
                SimpleNamespace(metrics=SimpleNamespace(cost_micros=c, clicks=k))
                for c, k in self._cpc
            ]
        if "match_type" in query:
            return [
                SimpleNamespace(
                    ad_group_criterion=SimpleNamespace(
                        keyword=SimpleNamespace(match_type=SimpleNamespace(name=name))
                    ),
                    metrics=SimpleNamespace(clicks=clk),
                )
                for name, clk in self._mt
            ]
        return []


class _Client:
    def __init__(self, ga):
        self._ga = ga

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self._ga


def test_medians_compute_budget_cpc_matchtype():
    ga = _QueryAwareGA(
        budgets=[40_000_000, 20_000_000, 60_000_000],  # медиана = 40
        cpc_rows=[(180_000, 1), (0, 0)],  # cost=180000, clicks=1 → cpc=180000
        mt_rows=[("PHRASE", 10), ("EXACT", 3), ("PHRASE", 1)],  # phrase больше по кликам
    )
    with allowed_ids(DRAFT_ACCOUNT_ID):
        m = search_campaign_medians(_Client(ga), DRAFT_ACCOUNT_ID)
    assert m.median_daily_budget_micros == 40_000_000
    assert m.avg_cpc_micros == 180_000
    assert m.common_match_type == "phrase"


def test_medians_empty_account_all_none():
    ga = _QueryAwareGA(budgets=[], cpc_rows=[], mt_rows=[])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        m = search_campaign_medians(_Client(ga), DRAFT_ACCOUNT_ID)
    assert m.median_daily_budget_micros is None
    assert m.avg_cpc_micros is None
    assert m.common_match_type is None


def test_medians_read_lock_blocks_foreign_account():
    ga = _QueryAwareGA(budgets=[1], cpc_rows=[], mt_rows=[])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            search_campaign_medians(_Client(ga), "1112223334")  # не в read-allow-list
            raise AssertionError("ожидался PermissionError (замок чтения)")
        except PermissionError:
            pass
