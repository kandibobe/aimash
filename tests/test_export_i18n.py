"""RU-утечка в артефактах: .xlsx и Google Sheets на lang='en' не должны содержать кириллицы.

docs/ACCEPTANCE.md утверждал, что RU-утечка закрыта, — она была закрыта только в СООБЩЕНИЯХ:
EN-менеджер получал книгу с листами «Сводка»/«Кампании» и колонками «Показы»/«Расход».

Гард структурный, а не по списку строк: собираем ВСЕ экспорты (отчёт, MCC-сводка, DEEP-книга,
вкладки Sheets) на латинских ДАННЫХ и требуем, чтобы в EN-версии не осталось ни одного
кириллического символа. Любой новый RU-литерал в reports/* (или ярлык без EN-пары в labels._EN)
уронит этот тест. Значения ячеек (имена кампаний, ENABLED, MOBILE) — данные Google, их не переводим,
поэтому в фикстурах они латиницей.
"""

from __future__ import annotations

import pathlib
import sys
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.read import ChildAccount  # noqa: E402
from core.config import settings  # noqa: E402
from reports import queries as Q  # noqa: E402
from reports import service as S  # noqa: E402
from reports.labels import loc, metric_headers, top_note  # noqa: E402
from reports.period import last_n_days  # noqa: E402
from reports.sheets import build_sheets_data  # noqa: E402
from reports.xlsx import build_mcc_deep_workbook, build_mcc_workbook, build_workbook  # noqa: E402

_CYR = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")


def _cyrillic(texts) -> list[str]:
    return [t for t in texts if _CYR & set(t)]


@contextmanager
def _allowed(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


def _row(cost_micros: int = 2_000_000):
    """Строка SDK с ЛАТИНСКИМИ данными: всё кириллическое в EN-книге — наш ярлык, а не данные."""
    return SimpleNamespace(
        metrics=SimpleNamespace(
            impressions=100,
            clicks=10,
            cost_micros=cost_micros,
            conversions=2.0,
            conversions_value=50.0,
        ),
        campaign=SimpleNamespace(name="Camp A", status=SimpleNamespace(name="ENABLED")),
        ad_group=SimpleNamespace(name="AG", status=SimpleNamespace(name="ENABLED")),
        ad_group_criterion=SimpleNamespace(
            keyword=SimpleNamespace(text="buy flowers", match_type=SimpleNamespace(name="PHRASE"))
        ),
        ad_group_ad=SimpleNamespace(
            ad=SimpleNamespace(
                id=123,
                type=SimpleNamespace(name="RESPONSIVE_SEARCH_AD"),
                final_urls=["https://example.com"],
                responsive_search_ad=SimpleNamespace(headlines=[SimpleNamespace(text="Buy now")]),
            )
        ),
        segments=SimpleNamespace(
            device=SimpleNamespace(name="MOBILE"),
            ad_network_type=SimpleNamespace(name="SEARCH"),
            date="2026-06-01",
        ),
    )


class _FakeClient:
    def get_service(self, name):
        assert name == "GoogleAdsService"
        return SimpleNamespace(search=lambda *, customer_id, query: [_row()])


def _report():
    """Отчёт со ВСЕМИ разбивками — титулы/колонки берём из реального reports.queries."""
    with _allowed(DRAFT_ACCOUNT_ID):
        r = S.build_account_report(
            _FakeClient(), DRAFT_ACCOUNT_ID, last_n_days(7, today=date(2026, 6, 25))
        )
    r.currency = "UGX"
    return r


def _wb_texts(wb) -> list[str]:
    out = list(wb.sheetnames)
    for ws in wb.worksheets:
        out += [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
    return out


def test_report_workbook_en_has_no_cyrillic():
    leaks = _cyrillic(_wb_texts(build_workbook(_report(), "en")))
    assert not leaks, f"RU-утечка в EN-книге отчёта: {leaks}"


def test_report_workbook_ru_stays_russian():
    """Обратная совместимость: дефолт (ru) — прежняя книга."""
    texts = _wb_texts(build_workbook(_report()))
    assert "Сводка" in texts and "Показы" in texts


def test_sheets_tabs_en_have_no_cyrillic():
    tabs = build_sheets_data(_report(), "en")
    texts = [t.title for t in tabs] + [
        str(c) for t in tabs for row in t.rows for c in row if c is not None
    ]
    leaks = _cyrillic(texts)
    assert not leaks, f"RU-утечка во вкладках Sheets: {leaks}"
    assert tabs[0].title == "Summary"


def _child(cid, name):
    return ChildAccount(id=cid, name=name, currency="UGX", manager=False, level=1, status="ENABLED")


def _mcc_summary():
    totals = Q.Metrics(impressions=10, clicks=2, cost_micros=5_000_000)
    return SimpleNamespace(
        manager_id="9990001112",
        period=last_n_days(7, today=date(2026, 6, 25)),
        subtotals=[SimpleNamespace(currency="UGX", accounts=1, totals=totals)],
        children=[
            SimpleNamespace(
                account=_child("1111111111", "Tower"),
                totals=totals,
                campaign_status={"ENABLED": 3},
            )
        ],
        inactive=[
            ChildAccount(
                id="3333333333",
                name="Old",
                currency="UGX",
                manager=False,
                level=1,
                status="CANCELED",
            )
        ],
        skipped=["5555555555"],
        managers=["4444444444"],
        errors=[("6666666666", "RuntimeError: x")],
    )


def test_mcc_workbook_en_has_no_cyrillic():
    leaks = _cyrillic(_wb_texts(build_mcc_workbook(_mcc_summary(), "en")))
    assert not leaks, f"RU-утечка в EN-книге /mcc: {leaks}"


def test_mcc_deep_workbook_en_has_no_cyrillic():
    s = _mcc_summary()
    deep = SimpleNamespace(
        manager_id=s.manager_id,
        period=s.period,
        items=[(_child("1111111111", "Tower"), _report())],
        skipped=s.skipped,
        managers=s.managers,
        inactive=s.inactive,
        errors=s.errors,
    )
    wb = build_mcc_deep_workbook(deep, "en")
    leaks = _cyrillic(_wb_texts(wb))
    assert not leaks, f"RU-утечка в EN DEEP-книге: {leaks}"
    assert wb.sheetnames[0] == "Summary" and wb.sheetnames[-1] == "Skipped and errors"


def test_every_label_emitted_by_queries_has_en_pair():
    """Ярлыки порождает КОД (Breakdown.title/dim_headers) — у каждого обязана быть EN-пара."""
    p = last_n_days(7, today=date(2026, 6, 25))
    with _allowed(DRAFT_ACCOUNT_ID):
        bs = [fn(_FakeClient(), DRAFT_ACCOUNT_ID, p) for fn in Q.BREAKDOWN_FETCHERS]
    labels = [b.title for b in bs] + [h for b in bs for h in b.dim_headers]
    untranslated = _cyrillic([loc(x, "en") for x in labels])
    assert not untranslated, f"нет EN-пары в reports.labels._EN: {untranslated}"


def test_metric_headers_and_note_localized():
    en = metric_headers("UGX", "en")
    assert en[4] == "Cost, UGX" and en[8] == "ROAS"  # деньги — с кодом валюты, ROAS — без
    assert not _cyrillic(en)
    assert not _cyrillic([top_note("keyword", 1000, "en"), top_note("ad", 1000, "en")])
    assert metric_headers() == Q.METRIC_HEADERS  # дефолт не изменился
