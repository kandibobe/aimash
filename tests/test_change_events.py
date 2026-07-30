"""Р6: журнал ЧУЖИХ правок (`change_event`). Офлайн, без SDK/сети.

Дыра, которую закрывает ридер (deploy/hermes/RISK_REGISTER.md, Р6): Google НЕ уведомляет о том,
что в аккаунте кто-то поменял бюджет руками. Freshness-гейт ловит чужую правку только в момент
исполнения НАШЕГО черновика — правку, сделанную помимо нас, не видит никто.

Пинуем ровно то, что ломается молча:
1. Контракт ресурса (LIMIT обязателен ≤10000, обе границы окна, ORDER BY DESC) — нарушишь, и
   сервер отвергнет запрос целиком.
2. Границы окна: верхняя — ЗАВТРА, иначе сегодняшних правок (ради которых всё и делается) в
   выборке не будет вовсе, а ответ «изменений нет» выглядит как настоящий.
3. Бессмысленное окно — отказ, а не тихое подрезание: укоротив окно молча, мы вернули бы
   «изменений нет» там, где их просто не искали.
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import date  # noqa: E402

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402
from reports import queries as Q  # noqa: E402

TODAY = date(2026, 7, 30)


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


def _ev(
    *,
    when: str = "2026-07-30 11:20:31.000000",
    resource_type: str = "CAMPAIGN_BUDGET",
    operation: str = "UPDATE",
    client_type: str = "GOOGLE_ADS_WEB_CLIENT",
    email: str = "manager@agency.example",
    resource: str = "customers/123/campaignBudgets/77",
    paths: tuple[str, ...] = ("amount_micros",),
):
    return SimpleNamespace(
        change_event=SimpleNamespace(
            change_date_time=when,
            change_resource_type=SimpleNamespace(name=resource_type),
            resource_change_operation=SimpleNamespace(name=operation),
            client_type=SimpleNamespace(name=client_type),
            user_email=email,
            change_resource_name=resource,
            changed_fields=SimpleNamespace(paths=list(paths)),
        )
    )


class _Client:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.seen: list[str] = []

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self

    def search(self, *, customer_id, query):
        self.seen.append(query)
        return list(self._rows)


def _query(days: int = 7, limit: int = 200, today: date = TODAY) -> str:
    client = _Client([_ev()])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        Q.fetch_change_events(client, DRAFT_ACCOUNT_ID, days=days, today=today, limit=limit)
    return client.seen[0]


# ── Контракт ресурса: без этого сервер отвергает запрос целиком ──────────────────────
def test_query_carries_mandatory_limit_and_window():
    q = _query()
    assert "FROM change_event" in q
    m = re.search(r"LIMIT (\d+)", q)
    assert m, "LIMIT для change_event ОБЯЗАТЕЛЕН — без него сервер отвергает запрос"
    assert int(m.group(1)) <= Q.CHANGE_EVENT_MAX_LIMIT
    assert "change_event.change_date_time >=" in q  # обе границы обязательны
    assert "change_event.change_date_time <=" in q
    assert "ORDER BY change_event.change_date_time DESC" in q  # свежие первыми — усечение по хвосту


def test_query_selects_only_documented_fields():
    """old_resource/new_resource НЕ выбираем (oneof по 15+ типам), leaf-путей внутрь композитов
    тоже нет — на этом уже обжигались с recommendation.impact (queries.py:440)."""
    q = _query()
    select = q.split(" FROM ")[0]
    assert "old_resource" not in select and "new_resource" not in select
    for f in ("change_date_time", "change_resource_type", "change_resource_name", "client_type"):
        assert f"change_event.{f}" in select
    assert "change_event.changed_fields" in select
    assert "change_event.changed_fields." not in select  # композит целиком, не leaf-путь


# ── Границы окна ─────────────────────────────────────────────────────────────────────
def test_window_upper_bound_is_tomorrow_so_today_is_included():
    """`change_date_time` — момент времени; сравнение с 'YYYY-MM-DD' берёт полночь. Верхняя
    граница «сегодня» отрезала бы ВЕСЬ сегодняшний день — то есть ровно те правки, ради которых
    алерт и существует, а ответ «изменений нет» был бы неотличим от настоящего."""
    q = _query(days=7)
    assert "'2026-07-31'" in q  # завтра
    assert "'2026-07-24'" in q  # ширина ровно 7 суток, считая сегодня


def test_window_width_stays_within_the_30_day_retention():
    """Максимальное окно должно помещаться в ретенцию даже с суточным сдвигом даты аккаунта.

    Верхняя граница — `today + 1`, а `today` best-effort (при недоступной таймзоне берётся дата
    ХОСТА). На аккаунте, чья дата на сутки впереди хостовой, окно ровно в 30 дней вылезло бы за
    ретенцию, и сервер отверг бы запрос ЦЕЛИКОМ — алерт молчал бы каждый цикл."""
    q = _query(days=Q.CHANGE_EVENT_MAX_DAYS)
    lo, hi = (date.fromisoformat(s) for s in re.findall(r"'(\d{4}-\d{2}-\d{2})'", q))
    retention = Q.CHANGE_EVENT_RETENTION_DAYS
    assert (hi - lo).days < retention  # с запасом на сутки; шире — сервер откажет
    assert (TODAY - lo).days < retention  # глубже 30 дней истории в API нет


@pytest.mark.parametrize("days", [0, -1, 30, 365])
def test_meaningless_window_is_refused_not_silently_trimmed(days):
    client = _Client()
    with allowed_ids(DRAFT_ACCOUNT_ID), pytest.raises(ValueError):
        Q.fetch_change_events(client, DRAFT_ACCOUNT_ID, days=days, today=TODAY)
    assert client.seen == []  # до сети не дошли


def test_limit_is_clamped_because_truncation_hits_the_tail():
    # Подрезание LIMIT безвредно (свежие события идут первыми), подрезание ОКНА — нет.
    assert f"LIMIT {Q.CHANGE_EVENT_MAX_LIMIT}" in _query(limit=99_999)
    assert "LIMIT 1" in _query(limit=0)


# ── Разбор строки ────────────────────────────────────────────────────────────────────
def test_row_parses_enums_and_changed_fields():
    client = _Client([_ev(paths=("amount_micros", "name"))])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        rows = Q.fetch_change_events(client, DRAFT_ACCOUNT_ID, today=TODAY)
    assert len(rows) == 1
    r = rows[0]
    assert r.resource_type == "CAMPAIGN_BUDGET" and r.operation == "UPDATE"
    assert r.client_type == "GOOGLE_ADS_WEB_CLIENT" and r.user_email == "manager@agency.example"
    assert r.resource_name == "customers/123/campaignBudgets/77"
    assert r.changed_fields == ("amount_micros", "name")  # FieldMask.paths → кортеж
    assert r.via_api is False


def test_row_survives_missing_fields():
    """Пустой FieldMask/незаполненный email — не повод ронять чтение всего журнала."""
    bare = SimpleNamespace(change_event=SimpleNamespace())
    client = _Client([bare])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        rows = Q.fetch_change_events(client, DRAFT_ACCOUNT_ID, today=TODAY)
    assert rows[0].changed_fields == () and rows[0].user_email == ""


def test_via_api_marks_our_own_channel():
    client = _Client([_ev(client_type="GOOGLE_ADS_API")])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        rows = Q.fetch_change_events(client, DRAFT_ACCOUNT_ID, today=TODAY)
    assert rows[0].via_api is True


# ── Замок чтения ─────────────────────────────────────────────────────────────────────
def test_read_lock_is_checked_before_the_network():
    """Замок ЧТЕНИЯ (gr9) — первой исполняемой строкой: журнал правок содержит почты сотрудников,
    читать его на неразрешённом аккаунте нельзя даже «просто посмотреть»."""
    client = _Client([_ev()])
    with allowed_ids(""), pytest.raises(PermissionError):
        Q.fetch_change_events(client, "9999999999", today=TODAY)
    assert client.seen == []
