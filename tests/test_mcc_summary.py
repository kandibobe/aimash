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


# ── 3.5: скор /audit в сводке — рендер + bulk-чтение снапшотов ─────────────────────
def _summ(children):
    from reports.mcc import MccSummary, aggregate_by_currency
    from reports.period import last_n_days

    s = MccSummary(manager_id="555", period=last_n_days(7), children=children)
    s.subtotals = aggregate_by_currency(children)
    return s


def test_summary_text_mcc_health_scores_sort_and_risk():
    """Скор из кэша в строке аккаунта; внутри валюты сперва деньги «под риском», потом расход;
    сумма риска — в подытоге валюты; без снапшота — честное «н/д»; старая эпоха — «*»."""
    from reports.service import summary_text_mcc

    a = ChildReport(_child("1111111111", "USD"), _m(cost_micros=9_000_000, clicks=90))
    b = ChildReport(_child("2222222222", "USD"), _m(cost_micros=5_000_000, clicks=50))
    b.health_score, b.health_grade, b.health_at_risk = 43, "D", 120.0
    c = ChildReport(_child("3333333333", "USD"), _m(cost_micros=1_000_000, clicks=10))
    c.health_score, c.health_grade, c.health_at_risk, c.health_stale = 88, "A", 0.0, True

    txt = summary_text_mcc(_summ([a, b, c]), lang="ru")
    # b (риск 120) выше a (расход 9) выше c (расход 1) — at_risk доминирует, дальше расход
    assert txt.index("2222222222") < txt.index("1111111111") < txt.index("3333333333")
    assert "🩺 <b>43</b> (D)" in txt
    assert "🩺 н/д" in txt  # a — аудит не прогонялся
    assert "🩺 <b>88</b> (A)*" in txt  # c — модель оценки обновилась
    assert "под риском" in txt and "120" in txt  # суммарный риск в подытоге USD
    assert "скор /audit" in txt  # сноска-легенда

    en = summary_text_mcc(_summ([a, b, c]), lang="en")
    assert "🩺 n/a" in en and "at risk" in en


def test_summary_text_mcc_flags_only_on_spending_accounts():
    """Флаги из УЖЕ собранного: «конверсий нет» (расход>0, конв==0) и «активных кампаний нет»
    (расход>0, ENABLED==0). Здоровый и не тративший аккаунты — без флагов."""
    from reports.service import summary_text_mcc

    burn = ChildReport(
        _child("1111111111", "USD"),
        _m(cost_micros=5_000_000, clicks=10),
        campaign_status={"PAUSED": 2},
    )
    healthy = ChildReport(
        _child("2222222222", "USD"),
        Metrics(clicks=10, cost_micros=3_000_000, conversions=4.0),
        campaign_status={"ENABLED": 1},
    )
    idle = ChildReport(_child("3333333333", "USD"), _m(cost_micros=0), campaign_status={})

    txt = summary_text_mcc(_summ([burn, healthy, idle]), lang="ru")
    lines = {
        cid: next(ln for ln in txt.splitlines() if f"acct-{cid}" in ln)
        for cid in ("1111111111", "2222222222", "3333333333")
    }
    assert "конверсий нет" in lines["1111111111"]
    assert "активных кампаний нет" in lines["1111111111"]
    assert "конверсий нет" not in lines["2222222222"]
    assert "активных кампаний нет" not in lines["2222222222"]
    assert "конверсий нет" not in lines["3333333333"]  # не тратил — не пугаем


def test_summary_text_mcc_without_snapshots_stays_clean():
    """Ни одного снапшота ⇒ ни «🩺 н/д» в строках, ни сноски — сводка как раньше."""
    from reports.service import summary_text_mcc

    cr = ChildReport(_child("1112223334", "USD"), _m(cost_micros=5_000_000, clicks=10))
    txt = summary_text_mcc(_summ([cr]), lang="ru")
    assert "🩺" not in txt and "скор /audit" not in txt


async def test_latest_snapshots_bulk_freshest_per_account_one_window():
    """audit.snapshot.latest_snapshots: ОДИН запрос → свежайший снапшот КАЖДОГО аккаунта своего
    окна (чужое period_days не подмешивается); без снапшотов — аккаунт отсутствует в словаре."""
    from types import SimpleNamespace

    from audit.snapshot import latest_snapshots, record_snapshot
    from db.session import init_db

    await init_db()

    def res(cid, score, ar=0.0, ver="e6:abc"):
        return SimpleNamespace(
            customer_id=cid,
            has_activity=True,
            score=score,
            grade="B",
            total_spend=100.0,
            at_risk=ar,
            currency="USD",
            families={},
            score_model_version=ver,
        )

    a, b = "9999000333", "9999000444"
    assert await record_snapshot(res(a, 50), snapshot_date="2026-07-01", period_days=30)
    assert await record_snapshot(res(a, 72, ar=40.0), snapshot_date="2026-07-15", period_days=30)
    assert await record_snapshot(
        res(a, 99), snapshot_date="2026-07-16", period_days=7
    )  # чужое окно
    assert await record_snapshot(
        res(b, 61, ver="e5:old"), snapshot_date="2026-07-10", period_days=30
    )

    snaps = await latest_snapshots([a, b, "0000000001"])
    assert snaps[a].score == 72 and snaps[a].snapshot_date == "2026-07-15"  # свежайший 30-дневный
    assert snaps[a].at_risk == 40.0
    assert snaps[b].score_model_version == "e5:old"  # сверку эпохи делает bot-слой
    assert "0000000001" not in snaps  # аудит не прогонялся — честно нет ключа
    assert await latest_snapshots([]) == {}
