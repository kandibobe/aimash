"""D6 (удобство 2026-07): «было→станет» для гео и стратегии ставок.

read_before снимает текущее ГЕО (read_campaign_targeting) и тип стратегии
(resolve.campaign_bidding_strategy); fmt_mutation_summary показывает реальный diff.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.service as service  # noqa: E402
import core.texts as texts  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def allowed(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


def test_diffable_ops_include_geo_and_bidding():
    assert {
        "set_geo_location",
        "set_geo_proximity",
        "set_bidding_strategy",
    } <= service._DIFFABLE_OPS


def test_non_diffable_returns_none():
    import asyncio

    assert asyncio.run(service.read_before("create_search_campaign", {"campaign": "X"})) is None


# ── резолвер текущей стратегии (read-only) ───────────────────────────────────────
def test_campaign_bidding_strategy_reads_enum_name():
    from ads import resolve

    class _GA:
        def search(self, customer_id, query):
            assert "bidding_strategy_type" in query
            return [
                SimpleNamespace(
                    campaign=SimpleNamespace(
                        id=42, bidding_strategy_type=SimpleNamespace(name="MAXIMIZE_CONVERSIONS")
                    )
                )
            ]

    class _Client:
        def get_service(self, name):
            return _GA()

    with allowed(DRAFT_ACCOUNT_ID):
        info = resolve.campaign_bidding_strategy(_Client(), DRAFT_ACCOUNT_ID, "К")
    assert info == {"id": "42", "strategy": "MAXIMIZE_CONVERSIONS"}


def test_campaign_bidding_strategy_none_when_absent():
    from ads import resolve

    class _GA:
        def search(self, customer_id, query):
            return []

    class _Client:
        def get_service(self, name):
            return _GA()

    with allowed(DRAFT_ACCOUNT_ID):
        assert resolve.campaign_bidding_strategy(_Client(), DRAFT_ACCOUNT_ID, "нет") is None


# ── fmt показывает «было → станет» когда снимок есть, и fallback когда нет ────────
def test_geo_location_summary_shows_before_after():
    p = {
        "campaign": "К",
        "locations": ["Uganda"],
        "country_code": "UG",
        "_before": {"kind": "geo", "before_locations": ["Kyiv"], "before_proximity": []},
    }
    assert "Kyiv" in texts.fmt_mutation_summary("set_geo_location", p, "ru")
    assert "→" in texts.fmt_mutation_summary("set_geo_location", p, "en")


def test_geo_empty_before_reads_all_regions():
    p = {
        "campaign": "К",
        "locations": ["UG"],
        "country_code": "UG",
        "_before": {"kind": "geo", "before_locations": [], "before_proximity": []},
    }
    assert "все регионы" in texts.fmt_mutation_summary("set_geo_location", p, "ru")
    assert "all regions" in texts.fmt_mutation_summary("set_geo_location", p, "en")


def test_bidding_summary_humanizes_enum_before():
    p = {
        "campaign": "К",
        "strategy": "maximize_conversions",
        "_before": {"kind": "bidding", "before_strategy": "MANUAL_CPC"},
    }
    ru = texts.fmt_mutation_summary("set_bidding_strategy", p, "ru")
    assert "Ручная CPC" in ru and "Максимум конверсий" in ru
    en = texts.fmt_mutation_summary("set_bidding_strategy", p, "en")
    assert "Manual CPC" in en and "Maximize conversions" in en


def test_summary_fallback_without_before():
    # снимка нет (read_before вернул None) — прежняя формулировка «Заменит прежний …»
    p = {"campaign": "К", "locations": ["X"], "country_code": "UG"}
    assert "Заменит" in texts.fmt_mutation_summary("set_geo_location", p, "ru")
