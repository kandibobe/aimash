"""Тесты сводного отчёта по MCC (ТЗ §8, reports/mcc.py). Офлайн, без SDK.

Проверяют: агрегацию по валютам БЕЗ FX (разные валюты не суммируются в одну), корректный пересчёт
производных (ROAS из суммы сырых счётчиков), и fail-closed обход дочерних — менеджерские и
не-разрешённые на чтение аккаунты пропускаются и ВИДНЫ в summary (без тихого замалчивания).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.read import ChildAccount  # noqa: E402
from core.config import settings  # noqa: E402
from reports.mcc import ChildReport, aggregate_by_currency, build_mcc_summary_async  # noqa: E402
from reports.queries import Metrics  # noqa: E402


@contextmanager
def _ids(allowed: str, read: str):
    """Временно задать мутационный и read allow-list (вернуть как было)."""
    pa, pr = settings.google_ads_allowed_customer_ids, settings.google_ads_read_customer_ids
    settings.google_ads_allowed_customer_ids = allowed
    settings.google_ads_read_customer_ids = read
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = pa
        settings.google_ads_read_customer_ids = pr


def _child(cid: str, currency: str, *, manager: bool = False) -> ChildAccount:
    return ChildAccount(
        id=cid, name=f"acct-{cid}", currency=currency, manager=manager, level=1, status="ENABLED"
    )


def _m(cost_micros: int = 0, clicks: int = 0, conv_value: float = 0.0) -> Metrics:
    return Metrics(clicks=clicks, cost_micros=cost_micros, conv_value=conv_value)


def test_aggregate_by_currency_groups_and_recomputes():
    children = [
        ChildReport(_child("1", "USD"), _m(cost_micros=10_000_000, clicks=100, conv_value=50.0)),
        ChildReport(_child("2", "USD"), _m(cost_micros=30_000_000, clicks=300, conv_value=150.0)),
        ChildReport(_child("3", "EUR"), _m(cost_micros=20_000_000, clicks=50, conv_value=40.0)),
    ]
    subs = aggregate_by_currency(children)
    by_cur = {s.currency: s for s in subs}
    assert by_cur["USD"].accounts == 2
    assert by_cur["USD"].totals.cost == 40.0  # 10 + 30
    assert by_cur["USD"].totals.clicks == 400
    assert by_cur["EUR"].totals.cost == 20.0
    # порядок — по убыванию расхода: USD(40) перед EUR(20)
    assert [s.currency for s in subs] == ["USD", "EUR"]
    # производные пересчитаны из СУММЫ сырых счётчиков: ROAS USD = (50+150)/40 = 5.0
    assert round(by_cur["USD"].totals.roas, 2) == 5.0


def test_currencies_never_summed_across():
    # Разные валюты дают РАЗНЫЕ подытоги — никогда не складываются в один «грязный» total.
    children = [
        ChildReport(_child("1", "USD"), _m(cost_micros=5_000_000)),
        ChildReport(_child("2", "EUR"), _m(cost_micros=7_000_000)),
        ChildReport(_child("3", "UAH"), _m(cost_micros=9_000_000)),
    ]
    subs = aggregate_by_currency(children)
    assert len(subs) == 3
    assert {s.currency for s in subs} == {"USD", "EUR", "UAH"}
    assert all(s.accounts == 1 for s in subs)


async def test_build_mcc_summary_skips_managers_and_non_read_allowed():
    DRAFT = "7753643025"
    CHILD_OK = "1112223334"
    CHILD_BLOCKED = "9998887776"
    MGR = "5556667778"
    children = [
        _child(MGR, "USD", manager=True),  # менеджер — без своих метрик
        _child(DRAFT, "USD"),  # мутационный аккаунт — читается
        _child(CHILD_OK, "EUR"),  # в read-list — читается
        _child(CHILD_BLOCKED, "USD"),  # НЕ в списках — пропуск (fail-closed)
    ]

    def fake_list(_client, _manager_id):
        return children

    def fake_fetch(_client, _cid, _period):
        return _m(cost_micros=1_000_000, clicks=10)

    with _ids(allowed=DRAFT, read=CHILD_OK):
        summary = await build_mcc_summary_async(
            object(), MGR, object(), list_children=fake_list, fetch=fake_fetch
        )

    assert summary.managers == [MGR]
    assert summary.skipped == [CHILD_BLOCKED]
    assert {c.account.id for c in summary.children} == {DRAFT, CHILD_OK}
    by_cur = {s.currency: s for s in summary.subtotals}
    assert by_cur["USD"].accounts == 1  # только DRAFT (CHILD_BLOCKED пропущен)
    assert by_cur["EUR"].accounts == 1  # CHILD_OK


async def test_build_mcc_summary_empty_read_list_includes_only_mutation_account():
    # read-list пуст → читаем ТОЛЬКО мутационный аккаунт (поведение по умолчанию, fail-closed).
    DRAFT = "7753643025"
    CHILD = "1112223334"
    children = [_child(DRAFT, "USD"), _child(CHILD, "EUR")]

    with _ids(allowed=DRAFT, read=""):
        summary = await build_mcc_summary_async(
            object(),
            "5556667778",
            object(),
            list_children=lambda *_: children,
            fetch=lambda *_: _m(cost_micros=2_000_000),
        )

    assert {c.account.id for c in summary.children} == {DRAFT}
    assert summary.skipped == [CHILD]


# ── §8: разбивка кампаний ПО СТАТУСУ (не только счётчик активных) ──────────────────
def test_fmt_campaign_status_breakdown():
    from reports.service import _fmt_campaign_status

    assert _fmt_campaign_status(None) == ""
    assert _fmt_campaign_status({}) == ""
    s = _fmt_campaign_status({"ENABLED": 3, "PAUSED": 5})
    assert "▶️3" in s and "⏸5" in s
    assert "Pending:2" in _fmt_campaign_status({"PENDING": 2})  # прочие статусы — «Имя:N»


def test_summary_text_mcc_shows_status_breakdown():
    from reports.mcc import MccSummary
    from reports.period import last_n_days
    from reports.service import summary_text_mcc

    cr = ChildReport(
        _child("1112223334", "USD"),
        _m(cost_micros=5_000_000, clicks=10),
        active_campaigns=3,
        campaign_status={"ENABLED": 3, "PAUSED": 2},
    )
    summ = MccSummary(manager_id="555", period=last_n_days(7), children=[cr])
    txt = summary_text_mcc(summ, lang="ru")
    assert "▶️3" in txt and "⏸2" in txt  # статус кампаний виден в строке аккаунта


def test_campaign_status_counts_query_and_count():
    from types import SimpleNamespace

    from reports.queries import campaign_status_counts

    captured: dict = {}

    class _Svc:
        def search(self, customer_id, query):
            captured["q"] = query
            return [
                SimpleNamespace(campaign=SimpleNamespace(status=SimpleNamespace(name="ENABLED"))),
                SimpleNamespace(campaign=SimpleNamespace(status=SimpleNamespace(name="ENABLED"))),
                SimpleNamespace(campaign=SimpleNamespace(status=SimpleNamespace(name="PAUSED"))),
            ]

    class _Client:
        def get_service(self, name):
            return _Svc()

    with _ids(allowed="1112223334", read=""):
        counts = campaign_status_counts(_Client(), "1112223334")
    assert counts == {"ENABLED": 2, "PAUSED": 1}
    assert "!= 'REMOVED'" in captured["q"] and "FROM campaign" in captured["q"]
