"""Офлайн-тесты §9 (удобство/визуализация): произвольный диапазон дат/день в _period_from_arg,
код валюты во всех денежных метриках (fmt_stats / summary_text / xlsx / Sheets), форматирование
тысяч и стрелки дельт. READ-ONLY, без живого Google Ads (SDK подменён фейком).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from bot import texts  # noqa: E402
from bot.main import _period_from_arg  # noqa: E402
from core.config import settings  # noqa: E402
from reports import queries as Q  # noqa: E402
from reports import service as S  # noqa: E402
from reports import sheets as SH  # noqa: E402
from reports import xlsx as X  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


def _row(cost_micros: int, name: str = "Camp"):
    return SimpleNamespace(
        metrics=SimpleNamespace(
            impressions=12480,
            clicks=512,
            cost_micros=cost_micros,
            conversions=4.0,
            conversions_value=900.0,
        ),
        campaign=SimpleNamespace(name=name, status=SimpleNamespace(name="ENABLED")),
        ad_group=SimpleNamespace(name="AG", status=SimpleNamespace(name="ENABLED")),
        ad_group_criterion=SimpleNamespace(
            keyword=SimpleNamespace(text="k", match_type=SimpleNamespace(name="PHRASE"))
        ),
        ad_group_ad=SimpleNamespace(
            ad=SimpleNamespace(id=1, type=SimpleNamespace(name="RESPONSIVE_SEARCH_AD"))
        ),
        segments=SimpleNamespace(
            device=SimpleNamespace(name="MOBILE"),
            ad_network_type=SimpleNamespace(name="SEARCH"),
            date="2026-06-01",
        ),
    )


class _FakeGA:
    def __init__(self, rows):
        self._rows = rows

    def search(self, *, customer_id, query):
        return list(self._rows)


class _FakeClient:
    def get_service(self, name):
        return _FakeGA([_row(12_480_500_000, "A")])


# ── _period_from_arg: пресет / день / диапазон / ошибки ──────────────────────────
def test_period_arg_preset_and_default():
    assert _period_from_arg("90").days == 90
    assert _period_from_arg(None).days == 30  # дефолт
    assert _period_from_arg("").days == 30
    assert _period_from_arg("MTD").label == "с начала месяца"


def test_period_arg_single_date_is_one_day():
    p = _period_from_arg("2026-06-10")
    assert p.date_from == date(2026, 6, 10) and p.date_to == date(2026, 6, 10)
    assert p.days == 1


def test_period_arg_range_and_order_normalized():
    p = _period_from_arg("2026-06-01 2026-06-30")
    assert p.date_from == date(2026, 6, 1) and p.date_to == date(2026, 6, 30)
    # обратный порядок нормализуется (min/max), не падает
    p2 = _period_from_arg("2026-06-30 — 2026-06-01")
    assert p2.date_from == date(2026, 6, 1) and p2.date_to == date(2026, 6, 30)


def test_period_arg_bad_inputs_raise():
    for bad in ("2026-13-01", "garbage", "5"):
        try:
            _period_from_arg(bad)
            raise AssertionError(f"должно было упасть: {bad!r}")
        except ValueError:
            pass


# ── Валюта в заголовках метрик ───────────────────────────────────────────────────
def test_metric_headers_currency_suffix():
    assert Q.metric_headers("") == Q.METRIC_HEADERS  # обратная совместимость
    h = Q.metric_headers("USD")
    assert "Расход, USD" in h and "Сред. CPC, USD" in h and "CPA, USD" in h and "Ценность, USD" in h
    assert "Показы" in h and "ROAS" in h  # неденежные — без валюты


# ── Форматирование тысяч / денег / дельт ─────────────────────────────────────────
def test_thousands_money_and_delta_arrows():
    assert S._thou(12480) == "12 480"
    assert S._money(12480.5, "USD") == "12 480.50 USD"
    assert S._money(5.0, "") == "5.00"  # без валюты — просто число
    assert S._delta_pct(120, 100).startswith("▲") and "+20%" in S._delta_pct(120, 100)
    assert S._delta_pct(80, 100).startswith("▼")
    assert S._delta_pct(100, 100).startswith("•")


# ── summary_text: валюта + тысячи ────────────────────────────────────────────────
def test_summary_text_shows_currency_and_thousands():
    p = S.Period(date(2026, 6, 1), date(2026, 6, 7), "тест")
    with allowed_ids(DRAFT_ACCOUNT_ID):
        report = S.build_account_report(
            _FakeClient(), DRAFT_ACCOUNT_ID, p, with_comparison=True, currency="USD"
        )
    assert report.currency == "USD"
    text = S.summary_text(report)
    assert "USD" in text  # валюта на денежных метриках
    assert "12 480" in text  # разделитель тысяч на показах
    assert "к пред. периоду" in text and "Расход" in text  # контракт не сломан


# ── fmt_stats: валюта только при наличии ─────────────────────────────────────────
def test_fmt_stats_currency_optional():
    st = {"impressions": 12480, "clicks": 512, "cost": 12480.5, "conversions": 4, "conv_value": 900}
    with_cur = texts.fmt_stats("7753643025", 30, st, "USD")
    # разделитель тысяч в bot.texts — неразрывный пробел (\xa0); проверяем дробную часть + валюту
    assert "USD" in with_cur and "480.50 USD" in with_cur
    assert "USD" not in texts.fmt_stats("7753643025", 30, st)  # без валюты — нет суффикса


# ── xlsx / Sheets: валюта в заголовках денежных колонок ──────────────────────────
def test_xlsx_and_sheets_currency_headers():
    from openpyxl import load_workbook

    p = S.Period(date(2026, 6, 1), date(2026, 6, 7), "тест")
    with allowed_ids(DRAFT_ACCOUNT_ID):
        report = S.build_account_report(_FakeClient(), DRAFT_ACCOUNT_ID, p, currency="USD")

    buf = BytesIO()
    X.build_workbook(report).save(buf)
    buf.seek(0)
    wb = load_workbook(buf)
    camp_headers = [c.value for c in wb["Кампании"][1]]
    assert "Расход, USD" in camp_headers and "Кампания" in camp_headers

    tabs = SH.build_sheets_data(report)
    breakdown_tab = next(t for t in tabs if t.title == "Кампании")
    header_row = next(r for r in breakdown_tab.rows if "Кампания" in r)
    assert "Расход, USD" in header_row
