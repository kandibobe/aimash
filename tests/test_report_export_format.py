"""§9 (волна 2.6b/2.6d): форматы чисел в Google Sheets и креатив в разбивке «Объявления».

Было: та же таблица в .xlsx читалась нормально, а в Google Sheets — нет. Форматы жили ТОЛЬКО в
reports/xlsx.py, Sheets писал значения RAW и без numberFormat: CTR показывался как «0.0523»
(сырая доля вместо 5,23%), расход — без разделителей, ставка ключа — без кода валюты. Жирнилась
строка 1, то есть ПОМЕТКА об усечении, а не шапка.

Плюс разбивка «Объявления» давала только ID+тип: понять, КАКОЙ креатив слил бюджет, было нельзя.
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


def _report(breakdowns):
    p = P.last_n_days(7, today=date(2026, 6, 25))
    return SimpleNamespace(
        customer_id="7753643025",
        period=p,
        currency="UGX",
        totals=Q.Metrics(impressions=100, clicks=5, cost_micros=2_000_000),
        prev_totals=None,
        breakdowns=breakdowns,
    )


def _breakdown(note=None):
    m = Q.Metrics(impressions=10, clicks=1, cost_micros=1_000_000)
    return Q.Breakdown(
        "campaign", "Кампании", ["Кампания", "Статус"], [(("A", "ENABLED"), m)], note
    )


# ── единый реестр форматов ───────────────────────────────────────────────────────
def test_formats_registry_is_single_source():
    assert X._METRIC_FORMATS is Q.METRIC_FORMATS
    assert len(Q.METRIC_FORMATS) == len(Q.METRIC_HEADERS)
    assert Q.METRIC_FORMATS[Q.METRIC_HEADERS.index("CTR")] == "0.00%"


# ── раскладка вкладок: шапка знает своё место ────────────────────────────────────
def test_sheet_tab_layout_summary_and_breakdown():
    tabs = SH.build_sheets_data(_report([_breakdown()]))
    summary = tabs[0]
    assert summary.dim_count == 1  # колонка «Период»
    assert summary.rows[summary.header_row][0] == "Период"  # шапка найдена по своему индексу
    camp = tabs[1]
    assert (camp.dim_count, camp.header_row) == (2, 0)
    assert camp.rows[camp.header_row][:2] == ["Кампания", "Статус"]


def test_note_row_shifts_header_row():
    """Пометка об усечении — первой строкой: шапка ниже, и жирнить надо ЕЁ, а не пометку."""
    tabs = SH.build_sheets_data(_report([_breakdown(note="показаны топ-1000 кампаний по расходу")]))
    camp = tabs[1]
    assert camp.header_row == 1
    assert camp.rows[0] == ["показаны топ-1000 кампаний по расходу"]
    bold = [
        r
        for r in SH.format_requests(tabs)
        if "repeatCell" in r and "textFormat" in r["repeatCell"]["cell"]["userEnteredFormat"]
    ]
    camp_bold = next(r for r in bold if r["repeatCell"]["range"]["sheetId"] == 1)
    assert camp_bold["repeatCell"]["range"]["startRowIndex"] == 1  # строка шапки, не пометки


# ── форматы чисел (главная регрессия 2.6b) ───────────────────────────────────────
def _num_formats(requests, sheet_id):
    out = {}
    for r in requests:
        rng = r.get("repeatCell", {}).get("range", {})
        fmt = r.get("repeatCell", {}).get("cell", {}).get("userEnteredFormat", {})
        if rng.get("sheetId") == sheet_id and "numberFormat" in fmt:
            out[rng["startColumnIndex"]] = fmt["numberFormat"]
    return out


def test_ctr_column_gets_percent_format():
    tabs = SH.build_sheets_data(_report([_breakdown()]))
    fmts = _num_formats(SH.format_requests(tabs), 1)  # вкладка «Кампании»: 2 измерения
    ctr_col = 2 + Q.METRIC_HEADERS.index("CTR")
    assert fmts[ctr_col] == {"type": "PERCENT", "pattern": "0.00%"}
    cost_col = 2 + Q.METRIC_HEADERS.index("Расход")
    assert fmts[cost_col] == {"type": "NUMBER", "pattern": "#,##0.00"}


def test_number_formats_start_below_header_and_cover_data():
    tabs = SH.build_sheets_data(_report([_breakdown(note="усечено")]))
    reqs = [
        r
        for r in SH.format_requests(tabs)
        if r.get("repeatCell", {}).get("range", {}).get("sheetId") == 1
        and "numberFormat" in r["repeatCell"]["cell"]["userEnteredFormat"]
    ]
    assert reqs, "колонки метрик должны получать формат"
    for r in reqs:
        rng = r["repeatCell"]["range"]
        assert rng["startRowIndex"] == 2  # пометка(0) + шапка(1) → данные с 2
        assert rng["endRowIndex"] == 3  # ровно одна строка данных


def test_empty_breakdown_gets_no_number_formats():
    tabs = SH.build_sheets_data(_report([Q.Breakdown("device", "Устройства", ["Устройство"], [])]))
    assert not _num_formats(SH.format_requests(tabs), 1)


# ── валюта в таблице ключей (§19.4.2) ────────────────────────────────────────────
def test_keyword_headers_carry_currency():
    assert SH.keyword_headers("UGX")[3] == "Top-of-page bid, UGX"
    assert SH.keyword_headers() == SH._KW_HEADERS  # без валюты — как было
    idea = SimpleNamespace(text="цветы", avg_monthly_searches=10, competition="LOW", low_bid=0.3)
    rows = SH.build_keyword_sheet_rows([idea], {}, "UGX")
    assert rows[0][3] == "Top-of-page bid, UGX"
    assert rows[1][0] == "цветы"


# ── разбивка «Объявления»: креатив, а не только ID (2.6d) ────────────────────────
class _FakeGA:
    def __init__(self, rows, sink):
        self._rows, self._sink = rows, sink

    def search(self, *, customer_id, query):
        self._sink.append(query)
        return list(self._rows)


class _FakeClient:
    def __init__(self, rows, sink):
        self._rows, self._sink = rows, sink

    def get_service(self, name):
        return _FakeGA(self._rows, self._sink)


def _ad_row():
    return SimpleNamespace(
        metrics=SimpleNamespace(
            impressions=100,
            clicks=10,
            cost_micros=1_000_000,
            conversions=1.0,
            conversions_value=5.0,
        ),
        campaign=SimpleNamespace(name="Camp"),
        ad_group=SimpleNamespace(name="AG"),
        ad_group_ad=SimpleNamespace(
            ad=SimpleNamespace(
                id=123,
                type=SimpleNamespace(name="RESPONSIVE_SEARCH_AD"),
                responsive_search_ad=SimpleNamespace(
                    headlines=[
                        SimpleNamespace(text="Цветы с доставкой"),
                        SimpleNamespace(text="24/7"),
                    ]
                ),
                final_urls=["https://example.com/flowers"],
            )
        ),
    )


def test_ad_breakdown_shows_creative():
    queries: list[str] = []
    with allowed_ids(DRAFT_ACCOUNT_ID):
        b = Q.fetch_by_ad(
            _FakeClient([_ad_row()], queries),
            DRAFT_ACCOUNT_ID,
            P.last_n_days(7, today=date(2026, 6, 25)),
        )
    assert b.dim_headers == ["Кампания", "Группа", "ID объявления", "Тип", "Заголовки", "Ссылка"]
    dims = b.rows[0][0]
    assert dims[4] == "Цветы с доставкой | 24/7"  # видно, ЧТО показывалось
    assert dims[5] == "https://example.com/flowers"
    assert "ad_group_ad.ad.responsive_search_ad.headlines" in queries[0]
    assert "ad_group_ad.ad.final_urls" in queries[0]


def test_ad_breakdown_survives_non_rsa_ad():
    """GDN/видео заголовков RSA не имеют — пустая ячейка, а не падение."""
    row = _ad_row()
    row.ad_group_ad.ad = SimpleNamespace(id=7, type=SimpleNamespace(name="RESPONSIVE_DISPLAY_AD"))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        b = Q.fetch_by_ad(
            _FakeClient([row], []), DRAFT_ACCOUNT_ID, P.last_n_days(7, today=date(2026, 6, 25))
        )
    assert b.rows[0][0][4] == "" and b.rows[0][0][5] == ""
