"""3F (§7): параметры keyword research — сеть и период исторических метрик.

Раньше сеть была зашита GOOGLE_SEARCH, а периода не существовало вовсе (из 4 параметров ТЗ §7
пользователю не был доступен ни один). Фейковый SDK-клиент, офлайн.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from ads.keyword_plan import _year_month_range, generate_keyword_ideas  # noqa: E402
from core.config import settings  # noqa: E402


def test_year_month_range_within_year():
    # сегодня 2026-07 → 12 полных месяцев: 2025-07 … 2026-06 (границы года!)
    (sy, sm), (ey, em) = _year_month_range(date(2026, 7, 3), 12)
    assert (sy, sm) == (2025, "JULY")
    assert (ey, em) == (2026, "JUNE")


def test_year_month_range_january_edge():
    # сегодня январь → конец = декабрь прошлого года
    (sy, sm), (ey, em) = _year_month_range(date(2026, 1, 15), 12)
    assert (sy, sm) == (2025, "JANUARY")
    assert (ey, em) == (2025, "DECEMBER")
    # 24 месяца
    (sy2, sm2), _ = _year_month_range(date(2026, 1, 15), 24)
    assert (sy2, sm2) == (2024, "JANUARY")


class _NS(SimpleNamespace):
    def __getattr__(self, name):
        val = _NS()
        setattr(self, name, val)
        return val


def _client(captured: dict):
    class _Svc:
        def generate_keyword_ideas(self, request):
            captured["request"] = request
            return []

    class _Enums:
        KeywordPlanNetworkEnum = SimpleNamespace(
            GOOGLE_SEARCH="GOOGLE_SEARCH",
            GOOGLE_SEARCH_AND_PARTNERS="GOOGLE_SEARCH_AND_PARTNERS",
        )
        MonthOfYearEnum = SimpleNamespace(
            **{
                m: f"M_{m}"
                for m in (
                    "JANUARY",
                    "FEBRUARY",
                    "MARCH",
                    "APRIL",
                    "MAY",
                    "JUNE",
                    "JULY",
                    "AUGUST",
                    "SEPTEMBER",
                    "OCTOBER",
                    "NOVEMBER",
                    "DECEMBER",
                )
            }
        )

    class _Req(_NS):
        def __init__(self):
            super().__init__()
            self.geo_target_constants = []
            self.keyword_seed = SimpleNamespace(keywords=[])
            self.url_seed = SimpleNamespace(url="")
            self.keyword_and_url_seed = SimpleNamespace(url="", keywords=[])

    class _Client:
        enums = _Enums()

        def get_service(self, name):
            return _Svc()

        def get_type(self, name):
            return _Req()

    return _Client()


@pytest.fixture(autouse=True)
def _allow_read(monkeypatch):
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", "7753643025")


def test_network_partners_passed_to_request():
    captured: dict = {}
    generate_keyword_ideas(
        _client(captured),
        "7753643025",
        seeds=["авто"],
        network="GOOGLE_SEARCH_AND_PARTNERS",
    )
    assert captured["request"].keyword_plan_network == "GOOGLE_SEARCH_AND_PARTNERS"


def test_network_default_and_unknown_fall_back_to_search():
    captured: dict = {}
    generate_keyword_ideas(_client(captured), "7753643025", seeds=["авто"])
    assert captured["request"].keyword_plan_network == "GOOGLE_SEARCH"
    generate_keyword_ideas(_client(captured), "7753643025", seeds=["авто"], network="МУСОР")
    assert captured["request"].keyword_plan_network == "GOOGLE_SEARCH"


def test_months_sets_year_month_range():
    captured: dict = {}
    generate_keyword_ideas(_client(captured), "7753643025", seeds=["авто"], months=12)
    rng = captured["request"].historical_metrics_options.year_month_range
    # месяцы — ТОЛЬКО по именам enum (в proto JANUARY=2; наш фейк маппит имя → "M_<NAME>")
    assert str(rng.start.month).startswith("M_") and str(rng.end.month).startswith("M_")
    assert int(rng.end.year) >= int(rng.start.year)


def test_months_none_leaves_defaults():
    captured: dict = {}
    generate_keyword_ideas(_client(captured), "7753643025", seeds=["авто"])
    rng = captured["request"].historical_metrics_options.year_month_range
    # опции не трогались: автосозданный _NS-узел без выставленного year
    assert not isinstance(getattr(rng.start, "year", None), int)
