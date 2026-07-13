"""§8 (волна 2.6): окно отчёта — в ТАЙМЗОНЕ АККАУНТА, а не хоста.

Google Ads режет segments.date по TZ аккаунта. Раньше /report /export /sheets /audit /bids
/searchterms строили окно по дате ХОСТА (логика TZ жила только в scheduler и /mcc) → для аккаунта
западнее хоста в окно попадал НЕПОЛНЫЙ день (ложная «просадка»), восточнее — терялся последний
полный. Проверяем и чистую арифметику (reanchor), и что чокпойнт в build_account_report_async
реально пере-якоряет окно ФЕТЧЕРОВ (не только подпись).
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.read as R  # noqa: E402
from core.config import settings  # noqa: E402
from reports import period as P  # noqa: E402
from reports import service as S  # noqa: E402
from reports import tz as TZ  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@contextmanager
def account_tz(tz: str | Exception):
    """Подменить чтение TZ аккаунта (позднее связывание: reports.tz импортирует ads.read внутри)."""
    prev = R.account_timezone

    def fake(client, customer_id):
        if isinstance(tz, Exception):
            raise tz
        return tz

    R.account_timezone = fake
    try:
        yield
    finally:
        R.account_timezone = prev


# ── чистая арифметика пере-якорения ──────────────────────────────────────────────
def test_reanchor_shifts_relative_window():
    p = P.last_n_days(7, today=date(2026, 6, 25))
    r = TZ.reanchor(p, date(2026, 6, 24))  # «сегодня» аккаунта на день раньше хоста
    assert (r.date_from, r.date_to) == (date(2026, 6, 17), date(2026, 6, 23))
    assert (r.kind, r.n) == ("last_n", 7)


def test_reanchor_mtd():
    r = TZ.reanchor(P.month_to_date(today=date(2026, 7, 1)), date(2026, 6, 30))
    assert (r.date_from, r.date_to) == (date(2026, 6, 1), date(2026, 6, 29))


def test_reanchor_custom_untouched():
    """Явные даты («за 3 августа», ISO-диапазон) менеджер назвал сам — сдвигать их нельзя."""
    p = P.custom(date(2026, 1, 1), date(2026, 1, 31))
    assert TZ.reanchor(p, date(2026, 6, 24)) == p


def test_reanchor_idempotent():
    p = P.last_n_days(30, today=date(2026, 6, 25))
    once = TZ.reanchor(p, date(2026, 6, 24))
    assert TZ.reanchor(once, date(2026, 6, 24)) == once  # вызывающие могут якорить повторно


# ── account_period: чтение TZ, fail-soft, экономия запроса на custom ──────────────
def test_account_period_uses_account_today():
    with account_tz("America/Los_Angeles"):
        p = asyncio.run(TZ.account_period(object(), "111", P.last_n_days(7)))
    expected = datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1)
    assert p.date_to == expected  # «вчера» АККАУНТА, а не хоста


def test_account_period_falls_back_to_host_date_on_tz_failure():
    base = P.last_n_days(7)
    with account_tz(RuntimeError("нет доступа к customer")):
        p = asyncio.run(TZ.account_period(object(), "111", base))
    assert p == base  # сбой чтения TZ не роняет отчёт и не сдвигает окно


def test_account_period_custom_does_not_read_tz():
    """Абсолютный период → лишний GAQL не тратим (и не зависим от доступности TZ)."""
    with account_tz(AssertionError("TZ читать не должны")):
        p = P.custom(date(2026, 1, 1), date(2026, 1, 31))
        assert asyncio.run(TZ.account_period(object(), "111", p)) == p


def test_account_period_none_is_safe():
    assert asyncio.run(TZ.account_period(object(), "111", None)) is None


# ── чокпойнт: окно ФЕТЧЕРОВ, а не только подпись ─────────────────────────────────
class _FakeGA:
    def __init__(self, sink):
        self._sink = sink

    def search(self, *, customer_id, query):
        self._sink.append(query)
        return []


class _FakeClient:
    def __init__(self, sink):
        self._sink = sink

    def get_service(self, name):
        return _FakeGA(self._sink)


def test_build_report_anchors_window_to_account_tz():
    """Ключевая регрессия: GAQL всех фетчеров идёт с датами аккаунта, и report.period им равен."""
    queries: list[str] = []
    with allowed_ids("222"), account_tz("Pacific/Auckland"):  # UTC+12/13 — почти всегда «завтра»
        report = asyncio.run(
            S.build_account_report_async(
                _FakeClient(queries), "222", P.last_n_days(7), with_comparison=False
            )
        )
    acct_today = datetime.now(ZoneInfo("Pacific/Auckland")).date()
    assert report.period.date_to == acct_today - timedelta(days=1)
    between = report.period.gaql_between()
    assert queries and all(between in q for q in queries), (
        "фетчеры обязаны получить ПЕРЕ-ЯКОРЕННОЕ окно (иначе подпись и данные о разных днях)"
    )
