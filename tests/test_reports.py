"""Офлайн-тесты Фазы 2.A: глубокий отчёт по аккаунту + .xlsx. READ-ONLY, без живого Google Ads.

SDK подменяется фейковым клиентом (get_service→search возвращает заранее заданные строки).
Проверяем: математику периода, производные метрики (CTR/CPC/CPA/ROAS), агрегацию из строк,
замок аккаунта на чтении (golden rule #9), структуру .xlsx.
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
from core.config import settings  # noqa: E402
from reports import period as P  # noqa: E402
from reports import queries as Q  # noqa: E402
from reports import service as S  # noqa: E402
from reports import xlsx as X  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


# ── Фейковый Google Ads клиент: одна «универсальная» строка под любой запрос ──────
def _row(cost_micros: int, name: str = "Camp"):
    return SimpleNamespace(
        metrics=SimpleNamespace(
            impressions=100,
            clicks=10,
            cost_micros=cost_micros,
            conversions=2.0,
            conversions_value=50.0,
        ),
        campaign=SimpleNamespace(name=name, status=SimpleNamespace(name="ENABLED")),
        ad_group=SimpleNamespace(name="AG", status=SimpleNamespace(name="ENABLED")),
        ad_group_criterion=SimpleNamespace(
            keyword=SimpleNamespace(text="купить цветы", match_type=SimpleNamespace(name="PHRASE"))
        ),
        ad_group_ad=SimpleNamespace(
            ad=SimpleNamespace(id=123, type=SimpleNamespace(name="RESPONSIVE_SEARCH_AD"))
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
    def __init__(self, rows):
        self._rows = rows

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return _FakeGA(self._rows)


# ── Период ───────────────────────────────────────────────────────────────────────
def test_last_n_days_excludes_today():
    today = date(2026, 6, 25)
    p = P.last_n_days(30, today=today)
    assert p.date_to == date(2026, 6, 24)  # вчера
    assert p.date_from == date(2026, 5, 26)
    assert p.days == 30
    assert p.gaql_between() == "segments.date BETWEEN '2026-05-26' AND '2026-06-24'"


def test_previous_period_is_equal_length_and_adjacent():
    p = P.last_n_days(7, today=date(2026, 6, 25))
    prev = p.previous()
    assert prev.date_to == date(2026, 6, 17)  # день перед началом текущего
    assert prev.days == 7


def test_month_to_date_and_first_of_month_edge():
    assert P.month_to_date(today=date(2026, 6, 25)).date_from == date(2026, 6, 1)
    p1 = P.month_to_date(today=date(2026, 6, 1))  # 1-е число: полных дней нет
    assert p1.date_from == date(2026, 6, 1) and p1.date_to == date(2026, 6, 1)


def test_from_preset_and_custom():
    assert P.from_preset("90", today=date(2026, 6, 25)).days == 90
    assert P.from_preset("MTD", today=date(2026, 6, 25)).date_from == date(2026, 6, 1)
    assert P.custom(date(2026, 1, 1), date(2026, 1, 31)).days == 31
    for bad in ("5", "year", ""):
        try:
            P.from_preset(bad)
            raise AssertionError(f"должно было упасть: {bad!r}")
        except ValueError:
            pass
    try:
        P.custom(date(2026, 2, 2), date(2026, 2, 1))
        raise AssertionError("date_to < date_from должно падать")
    except ValueError:
        pass


# ── Метрики (производные считает КОД) ────────────────────────────────────────────
def test_metrics_derived_and_sum():
    m = Q.Metrics(
        impressions=1000, clicks=50, cost_micros=25_000_000, conversions=5.0, conv_value=200.0
    )
    assert m.cost == 25.0
    assert abs(m.ctr - 0.05) < 1e-9
    assert m.avg_cpc == 0.5
    assert m.cpa == 5.0
    assert m.roas == 8.0
    # деление на ноль безопасно
    z = Q.Metrics()
    assert z.ctr == 0.0 and z.avg_cpc == 0.0 and z.cpa == 0.0 and z.roas == 0.0
    z.add(m)
    assert z.clicks == 50 and z.cost_micros == 25_000_000
    assert len(m.as_row()) == len(Q.METRIC_HEADERS)


# ── Замок аккаунта на чтении (golden rule #9) ────────────────────────────────────
def test_fetchers_reject_foreign_account():
    p = P.last_n_days(7, today=date(2026, 6, 25))
    client = _FakeClient([_row(1_000_000)])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        for fn in (Q.fetch_totals, *Q.BREAKDOWN_FETCHERS):
            try:
                fn(client, "1234567890", p)
                raise AssertionError(f"{fn.__name__}: чужой аккаунт должен падать")
            except PermissionError:
                pass


# ── Агрегация из строк ───────────────────────────────────────────────────────────
def test_fetch_totals_and_breakdowns_aggregate():
    p = P.last_n_days(7, today=date(2026, 6, 25))
    client = _FakeClient([_row(3_000_000, "A"), _row(1_000_000, "B")])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        totals = Q.fetch_totals(client, DRAFT_ACCOUNT_ID, p)
        assert totals.cost == 4.0 and totals.clicks == 20  # сумма двух строк
        camp = Q.fetch_by_campaign(client, DRAFT_ACCOUNT_ID, p)
        assert camp.key == "campaign" and len(camp.rows) == 2
        assert camp.dim_headers == ["Кампания", "Статус"]
        kw = Q.fetch_by_keyword(client, DRAFT_ACCOUNT_ID, p)
        assert kw.rows[0][0][2] == "купить цветы"  # текст ключа
        ad = Q.fetch_by_ad(client, DRAFT_ACCOUNT_ID, p)
        assert ad.rows[0][0][3] == "RESPONSIVE_SEARCH_AD"  # ad.type.name
        dev = Q.fetch_by_device(client, DRAFT_ACCOUNT_ID, p)
        assert dev.rows[0][0][0] == "MOBILE"


# ── Сборка отчёта + текстовая сводка ─────────────────────────────────────────────
def test_build_account_report_and_summary():
    p = P.last_n_days(7, today=date(2026, 6, 25))
    client = _FakeClient([_row(2_000_000, "A")])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        report = S.build_account_report(client, DRAFT_ACCOUNT_ID, p, with_comparison=True)
    assert report.prev_totals is not None
    assert len(report.breakdowns) == len(Q.BREAKDOWN_FETCHERS)
    text = S.summary_text(report)
    assert DRAFT_ACCOUNT_ID in text and "Расход" in text and "к пред. периоду" in text


def test_build_report_without_comparison():
    p = P.last_n_days(30, today=date(2026, 6, 25))
    client = _FakeClient([_row(1_000_000)])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        report = S.build_account_report(client, DRAFT_ACCOUNT_ID, p, with_comparison=False)
    assert report.prev_totals is None


# ── .xlsx: структура и формат ────────────────────────────────────────────────────
def test_xlsx_workbook_structure():
    from openpyxl import load_workbook

    p = P.last_n_days(7, today=date(2026, 6, 25))
    client = _FakeClient([_row(2_000_000, "A"), _row(1_000_000, "B")])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        report = S.build_account_report(client, DRAFT_ACCOUNT_ID, p)
    wb = X.build_workbook(report)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    wb2 = load_workbook(buf)

    assert wb2.sheetnames[0] == "Сводка"
    for title in ("Кампании", "Группы объявлений", "Ключевые слова", "По дням"):
        assert title in wb2.sheetnames
    ws = wb2["Кампании"]
    headers = [c.value for c in ws[1]]
    assert headers == ["Кампания", "Статус", *Q.METRIC_HEADERS]
    # данные на месте: первая кампания A с расходом 2.0
    assert ws.cell(row=2, column=1).value == "A"
    assert ws.cell(row=2, column=7).value == 2.0  # колонка «Расход» (2 dim + 5-я метрика)
