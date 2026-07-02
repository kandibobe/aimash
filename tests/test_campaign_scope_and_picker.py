"""§8/§9: фильтр отчёта по кампании (GAQL-скоуп) + классификация неактивных дочерних в MCC-сводке +
перечислитель аккаунтов для пикера. Офлайн, без живого Google Ads."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.read import ChildAccount  # noqa: E402
from core.config import settings  # noqa: E402
from reports import period as P  # noqa: E402
from reports import queries as Q  # noqa: E402
from reports.mcc import build_mcc_summary  # noqa: E402
from reports.queries import Metrics  # noqa: E402


@contextmanager
def _ids(allowed: str = "", read: str = ""):
    pa, pr = settings.google_ads_allowed_customer_ids, settings.google_ads_read_customer_ids
    settings.google_ads_allowed_customer_ids = allowed
    settings.google_ads_read_customer_ids = read
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = pa
        settings.google_ads_read_customer_ids = pr


class _CapturingGA:
    def __init__(self):
        self.queries: list[str] = []

    def search(self, *, customer_id, query):
        self.queries.append(query)
        return []


class _CapturingClient:
    def __init__(self):
        self.ga = _CapturingGA()

    def get_service(self, name):
        return self.ga


DRAFT = "7753643025"


# ── Фильтр по кампании: FROM campaign + campaign.id = X, unscoped — байт-в-байт как раньше ──
def test_campaign_scoped_totals_switch_to_from_campaign_with_id():
    client = _CapturingClient()
    period = P.last_n_days(7)
    with _ids(allowed=DRAFT):
        Q.fetch_totals(client, DRAFT, period, "123")
    q = client.ga.queries[-1]
    assert "FROM campaign" in q
    assert "campaign.id = 123" in q


def test_unscoped_totals_are_from_customer_without_campaign_filter():
    client = _CapturingClient()
    period = P.last_n_days(7)
    with _ids(allowed=DRAFT):
        Q.fetch_totals(client, DRAFT, period, None)
    q = client.ga.queries[-1]
    assert "FROM customer" in q
    assert "campaign.id" not in q


def test_campaign_scoped_device_breakdown_switches_source():
    client = _CapturingClient()
    period = P.last_n_days(7)
    with _ids(allowed=DRAFT):
        Q.fetch_by_device(client, DRAFT, period, "555")
    q = client.ga.queries[-1]
    assert "FROM campaign" in q and "campaign.id = 555" in q


def test_campaign_scoped_id_is_int_coerced_no_injection():
    client = _CapturingClient()
    period = P.last_n_days(7)
    with _ids(allowed=DRAFT):
        Q.fetch_by_campaign(client, DRAFT, period, "42")
    assert "campaign.id = 42" in client.ga.queries[-1]


# ── MCC: неактивные (не ENABLED) уходят в inactive, НЕ в errors, метрику не читаем ──
def _child(cid: str, status: str = "ENABLED", manager: bool = False) -> ChildAccount:
    return ChildAccount(
        id=cid, name=f"acct-{cid}", currency="USD", manager=manager, level=1, status=status
    )


def test_inactive_child_goes_to_inactive_bucket_not_errors():
    active, canceled = "1112223334", "2223334445"
    children = [_child(active, "ENABLED"), _child(canceled, "CANCELED")]
    fetched: list[str] = []

    def fake_fetch(_client, cid, _period):
        fetched.append(cid)
        return Metrics(cost_micros=1_000_000, clicks=10)

    with _ids(allowed=DRAFT, read=f"{active},{canceled}"):
        summary = build_mcc_summary(
            object(),
            "5556667778",
            P.last_n_days(7),
            list_children=lambda *_: children,
            fetch=fake_fetch,
        )
    assert [c.account.id for c in summary.children] == [active]  # только ENABLED прочитан
    assert [c.id for c in summary.inactive] == [canceled]  # CANCELED — в inactive
    assert summary.errors == []  # и НЕ в ошибках
    assert canceled not in fetched  # метрику неактивного не запрашивали


# ── Пикер: перечислитель аккаунтов = Draft + read-list, все проходят ensure_read_allowed ──
def test_read_account_rows_lists_draft_and_read_ids():
    import bot.main as bm

    extra = "6764040266"
    with _ids(allowed=DRAFT, read=extra):
        rows = bm._read_account_rows(chat_id=1)
    ids = {r.id for r in rows}
    assert DRAFT in ids
    assert extra in ids  # §8: 676-404-0266 доступен на чтение через env read-list
