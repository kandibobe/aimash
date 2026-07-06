"""Дневная квота операций Google Ads (§3, core.quota). Чистая логика, без SDK/сети.

Проверяют: учёт операций в окне, блок мутаций на ≥95% (fail-closed), чтение НЕ блокируется,
limit=0 выключает гард, snapshot структурен. Время — реальное (time.time), окно 24ч не истекает
за тест."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import quota  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def _limit(n: int):
    prev = settings.google_ads_daily_op_limit
    settings.google_ads_daily_op_limit = n
    quota.reset()
    try:
        yield
    finally:
        settings.google_ads_daily_op_limit = prev
        quota.reset()


def test_record_and_usage_pct():
    with _limit(10):
        for _ in range(4):
            quota.record(kind="read")
        assert abs(quota.usage_pct() - 0.4) < 1e-9


def test_block_mutation_at_95pct_reads_never_block():
    with _limit(10):
        for _ in range(9):
            quota.record(kind="mutate")
        quota.check_mutation_allowed()  # 90% < 95% → ок
        quota.record(kind="mutate")  # 10/10 = 100%
        with pytest.raises(quota.QuotaExceededError):
            quota.check_mutation_allowed()
        # Чтение НИКОГДА не блокируется (нет гейта в read-пути) — record просто считает.
        quota.record(kind="read")  # не бросает


def test_limit_zero_disables_guard():
    with _limit(0):
        for _ in range(100):
            quota.record(kind="mutate")
        assert quota.usage_pct() == 0.0
        quota.check_mutation_allowed()  # без лимита — гарда нет


def test_snapshot_shape_and_by_account():
    with _limit(1000):
        quota.record("111", kind="mutate")
        quota.record("111", kind="read")
        quota.record("222", kind="read")
        snap = quota.snapshot()
        assert snap["limit"] == 1000 and snap["used"] == 3
        assert snap["by_account"]["111"] == 2 and snap["by_account"]["222"] == 1
        assert snap["window_hours"] == 24


def test_record_count_batch():
    """1F4: батч из N операций = N событий квоты (Google тарифицирует каждую mutate-операцию)."""
    with _limit(100):
        quota.record("111", kind="mutate", count=50)
        assert abs(quota.usage_pct() - 0.5) < 1e-9
        assert quota.snapshot()["by_account"]["111"] == 50
        # кламп: мусорный count не ломает счётчик (минимум 1)
        quota.record(kind="read", count=0)
        assert quota.snapshot()["used"] == 51


async def test_run_ads_call_passes_op_count_to_quota(monkeypatch):
    """run_ads_call прокидывает op_count в quota.record (учёт батча, §3)."""
    from core import resilience

    seen: dict = {}

    def _fake_record(account=None, *, kind="read", count=1):
        seen.update(account=account, kind=kind, count=count)

    monkeypatch.setattr(quota, "record", _fake_record)
    monkeypatch.setattr(quota, "check_mutation_allowed", lambda account=None: None)

    def _sdk_call():
        return {"ok": True}

    res = await resilience.run_ads_call(_sdk_call, account="777", op_count=42)
    assert res == {"ok": True}
    assert seen == {"account": "777", "kind": "mutate", "count": 42}


async def test_run_ads_create_call_counts_quota_and_blocks_before_sdk(monkeypatch):
    """1.2 (аудит 2026-07-06): создатели идут через run_ads_create_call — квота учитывается
    (op_count батча), а на ≥95% блок происходит ДО SDK-вызова (fail-closed, без трат)."""
    from core import resilience

    seen: dict = {}

    def _fake_record(account=None, *, kind="read", count=1):
        seen.update(account=account, kind=kind, count=count)

    monkeypatch.setattr(quota, "record", _fake_record)
    monkeypatch.setattr(quota, "check_mutation_allowed", lambda account=None: None)

    def _sdk_create():
        return {"campaign": "customers/1/campaigns/2"}

    res = await resilience.run_ads_create_call(_sdk_create, account="777", op_count=17)
    assert res == {"campaign": "customers/1/campaigns/2"}
    assert seen == {"account": "777", "kind": "mutate", "count": 17}

    # Блок квоты — ДО SDK: сам вызов не должен исполниться.
    def _boom(account=None):
        raise quota.QuotaExceededError("лимит")

    called: list = []

    def _sdk_never():
        called.append(1)

    monkeypatch.setattr(quota, "check_mutation_allowed", _boom)
    with pytest.raises(quota.QuotaExceededError):
        await resilience.run_ads_create_call(_sdk_never, account="777")
    assert called == []  # SDK не тронут


async def test_run_ads_create_call_does_not_retry(monkeypatch):
    """Создатели НЕ идемпотентны: транзиентная ошибка НЕ ретраится (один вызов — одна попытка)."""
    from google.api_core import exceptions as gapi

    from core import resilience

    monkeypatch.setattr(quota, "check_mutation_allowed", lambda account=None: None)
    monkeypatch.setattr(quota, "record", lambda *a, **kw: None)
    attempts: list = []

    def _flaky():
        attempts.append(1)
        raise gapi.InternalServerError("транзиент")  # для run_ads_call это ретраебл

    with pytest.raises(gapi.InternalServerError):
        await resilience.run_ads_create_call(_flaky)
    assert len(attempts) == 1  # ровно одна попытка — без backoff-повторов
