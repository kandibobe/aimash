"""Офлайн-тесты чтения Google Ads (ads/read.py) — READ-ONLY, ничего не меняет.

Закрывает дыру покрытия из аудита: read-путь (account_stats/list_campaigns/list_child_accounts)
не имел прямых юнит-тестов. Без живого Google Ads — клиент фейковый (SimpleNamespace-строки);
БД не нужна. Проверяем:
- account_stats: агрегацию строк + перевод cost_micros→валюту (÷1e6) + кламп days (90 → молча 30);
- list_campaigns: маппинг id/name/status.name; пустой результат;
- замок аккаунта (ensure_allowed, gr #9) и замок обхода MCC (ensure_manager_allowed, fail-closed).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.read import account_stats, list_campaigns, list_child_accounts  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@contextmanager
def login_customer_id(value: str):
    """Временно задать настроенный MCC (для ensure_manager_allowed); вернуть как было."""
    prev = settings.google_ads_login_customer_id
    settings.google_ads_login_customer_id = value
    try:
        yield
    finally:
        settings.google_ads_login_customer_id = prev


# ── Фейковый SDK ─────────────────────────────────────────────────────────────────
class _FakeGA:
    def __init__(self, rows):
        self._rows = rows
        self.last_query: str | None = None

    def search(self, *, customer_id, query):
        self.last_query = query
        return list(self._rows)


class _FakeClient:
    def __init__(self, rows):
        self._ga = _FakeGA(rows)

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self._ga


def _metrics_row(*, imp=0, clk=0, cost_micros=0, conv=0.0, cval=0.0):
    return SimpleNamespace(
        metrics=SimpleNamespace(
            impressions=imp,
            clicks=clk,
            cost_micros=cost_micros,
            conversions=conv,
            conversions_value=cval,
        )
    )


def _campaign_row(*, cid="1", name="Кампания", status="ENABLED"):
    return SimpleNamespace(
        campaign=SimpleNamespace(id=int(cid), name=name, status=SimpleNamespace(name=status))
    )


def _child_row(
    *, cid="7753643025", name="Aimash Draft", cur="USD", manager=False, level=1, status="ENABLED"
):
    return SimpleNamespace(
        customer_client=SimpleNamespace(
            id=int(cid),
            descriptive_name=name,
            currency_code=cur,
            manager=manager,
            level=level,
            status=SimpleNamespace(name=status),
        )
    )


# ── account_stats: агрегация + micros→валюта ─────────────────────────────────────
def test_account_stats_aggregates_and_converts_cost():
    rows = [
        _metrics_row(imp=100, clk=10, cost_micros=2_000_000, conv=1.0, cval=50.0),
        _metrics_row(imp=50, clk=5, cost_micros=500_000, conv=0.5, cval=20.0),
    ]
    with allowed_ids(DRAFT_ACCOUNT_ID):
        stats = account_stats(_FakeClient(rows), DRAFT_ACCOUNT_ID)
    assert stats.impressions == 150
    assert stats.clicks == 15
    assert stats.cost == 2.5  # (2_000_000 + 500_000) / 1e6
    assert stats.conversions == 1.5
    assert stats.conv_value == 70.0


def test_account_stats_empty_is_zero():
    with allowed_ids(DRAFT_ACCOUNT_ID):
        stats = account_stats(_FakeClient([]), DRAFT_ACCOUNT_ID)
    assert (stats.impressions, stats.clicks, stats.cost) == (0, 0, 0.0)
    assert stats.conversions == 0.0 and stats.conv_value == 0.0


def test_account_stats_window_matches_requested_days():
    """Окно строится из РЕАЛЬНОГО N (любого), а не схлопывается в пресет — иначе подпись врала бы
    про объём денежных метрик (находка аудита: 90 дн. показывали 30 дн. данных)."""
    from datetime import date, timedelta

    client = _FakeClient([])
    end = date.today() - timedelta(days=1)  # вчера (как LAST_N_DAYS — без неполного сегодня)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        for n in (7, 14, 90, 60):
            account_stats(client, DRAFT_ACCOUNT_ID, days=n)
            q = client._ga.last_query
            start = end - timedelta(days=n - 1)
            assert f"BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'" in q
            assert "LAST_" not in q  # больше не пресет — реальное окно N дней


def test_account_stats_rejects_foreign_account():
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            account_stats(_FakeClient([]), "1234567890")


# ── list_campaigns ───────────────────────────────────────────────────────────────
def test_list_campaigns_maps_rows():
    rows = [_campaign_row(cid="1", name="A"), _campaign_row(cid="2", name="B", status="PAUSED")]
    with allowed_ids(DRAFT_ACCOUNT_ID):
        out = list_campaigns(_FakeClient(rows), DRAFT_ACCOUNT_ID)
    assert out == [
        {"id": "1", "name": "A", "status": "ENABLED"},
        {"id": "2", "name": "B", "status": "PAUSED"},
    ]


def test_list_campaigns_empty():
    with allowed_ids(DRAFT_ACCOUNT_ID):
        assert list_campaigns(_FakeClient([]), DRAFT_ACCOUNT_ID) == []


def test_list_campaigns_rejects_foreign_account():
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            list_campaigns(_FakeClient([]), "1234567890")


# ── list_child_accounts: обход MCC за ensure_manager_allowed ──────────────────────
def test_list_child_accounts_maps_rows():
    rows = [_child_row(cid="7753643025", name="Aimash Draft", cur="USD")]
    with login_customer_id(DRAFT_ACCOUNT_ID):
        out = list_child_accounts(_FakeClient(rows), DRAFT_ACCOUNT_ID)
    assert len(out) == 1
    assert out[0].id == "7753643025"
    assert out[0].currency == "USD"
    assert out[0].status == "ENABLED"  # из status.name


def test_list_child_accounts_fail_closed_without_login_id():
    with login_customer_id(""):
        with pytest.raises(PermissionError):
            list_child_accounts(_FakeClient([]), DRAFT_ACCOUNT_ID)


def test_list_child_accounts_rejects_foreign_manager():
    with login_customer_id(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            list_child_accounts(_FakeClient([]), "1234567890")
