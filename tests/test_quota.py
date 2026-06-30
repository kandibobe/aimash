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
