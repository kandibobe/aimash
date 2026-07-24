"""Дневная квота операций Google Ads (§3, core.quota). Общий стор ads_quota_ops (реальная temp-SQLite).

Проверяют: учёт операций в 24ч-окне, блок мутаций на ≥95% (fail-closed), чтение НЕ блокируется,
limit=0 выключает гард, snapshot структурен, граница окна на SQLite (ts>cutoff через db_dt — не
naive/tz-aware мусор), предохранитель reset() (DELETE только на SQLite). Счётчик теперь async+БД —
монкейпатчи в resilience-тестах обязаны быть async, иначе `await` по ним даст TypeError.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import quota  # noqa: E402
from core.config import settings  # noqa: E402
from db.models import AdsQuotaOp  # noqa: E402
from db.session import Session, db_dt, init_db  # noqa: E402


@asynccontextmanager
async def _limit(n: int):
    """Зафиксировать дневной лимит и обнулить стор до/после (изоляция теста). init_db() вызвать ДО —
    reset() это DELETE FROM реальной таблицы."""
    prev = settings.google_ads_daily_op_limit
    settings.google_ads_daily_op_limit = n
    await quota.reset()
    try:
        yield
    finally:
        settings.google_ads_daily_op_limit = prev
        await quota.reset()


async def test_record_and_usage_pct():
    await init_db()
    async with _limit(10):
        for _ in range(4):
            await quota.record(kind="read")
        assert abs(await quota.usage_pct() - 0.4) < 1e-9


async def test_block_mutation_at_95pct_reads_never_block():
    await init_db()
    async with _limit(10):
        for _ in range(9):
            await quota.record(kind="mutate")
        await quota.check_mutation_allowed()  # 90% < 95% → ок
        await quota.record(kind="mutate")  # 10/10 = 100%
        with pytest.raises(quota.QuotaExceededError):
            await quota.check_mutation_allowed()
        # Чтение НИКОГДА не блокируется (нет гейта в read-пути) — record просто считает.
        await quota.record(kind="read")  # не бросает


async def test_limit_zero_disables_guard():
    await init_db()
    async with _limit(0):
        for _ in range(100):
            await quota.record(kind="mutate")
        assert await quota.usage_pct() == 0.0
        await quota.check_mutation_allowed()  # без лимита — гарда нет


async def test_snapshot_shape_and_by_account():
    await init_db()
    async with _limit(1000):
        await quota.record("111", kind="mutate")
        await quota.record("111", kind="read")
        await quota.record("222", kind="read")
        snap = await quota.snapshot()
        assert snap["limit"] == 1000 and snap["used"] == 3
        assert snap["by_account"]["111"] == 2 and snap["by_account"]["222"] == 1
        assert snap["window_hours"] == 24


async def test_record_count_batch():
    """1F4: батч из N операций = N событий квоты (Google тарифицирует каждую mutate-операцию)."""
    await init_db()
    async with _limit(100):
        await quota.record("111", kind="mutate", count=50)
        assert abs(await quota.usage_pct() - 0.5) < 1e-9
        assert (await quota.snapshot())["by_account"]["111"] == 50
        # кламп: мусорный count не ломает счётчик (минимум 1)
        await quota.record(kind="read", count=0)
        assert (await quota.snapshot())["used"] == 51


async def test_window_boundary_sqlite():
    """Граница 24ч-окна на SQLite: db_dt делает сравнение `ts > cutoff` корректным (иначе naive
    (SQLite) vs tz-aware (cutoff) молча даёт мусор — ровно то, чего сиблинги в purge избегают). Строка
    на now-23:59 попадает в счёт, на now-24:01 — уже нет. Считаем строго стор, лимит держим постоянным."""
    await init_db()
    async with _limit(1000):
        now = datetime.now(timezone.utc)
        async with Session() as s:
            s.add(
                AdsQuotaOp(
                    ts=db_dt(now - timedelta(hours=23, minutes=59)),
                    account="111",
                    kind="read",
                    op_count=7,
                )
            )
            s.add(  # за границей окна — не должна считаться
                AdsQuotaOp(
                    ts=db_dt(now - timedelta(hours=24, minutes=1)),
                    account="111",
                    kind="read",
                    op_count=99,
                )
            )
            await s.commit()
        snap = await quota.snapshot()
        assert snap["used"] == 7  # НЕ 106 — старая строка вне окна
        assert snap["by_account"]["111"] == 7
        assert abs(await quota.usage_pct("111") - 0.007) < 1e-9


async def test_reset_guarded_off_sqlite(monkeypatch):
    """Предохранитель: reset() (DELETE FROM ads_quota_ops) запрещён вне SQLite — защита от стирания
    реальной прод-истории квоты при неверном DATABASE_URL. На не-SQLite диалекте reset() бросает
    ДО любого DELETE."""
    await init_db()
    monkeypatch.setattr(quota.engine.dialect, "name", "postgresql", raising=False)
    with pytest.raises(RuntimeError):
        await quota.reset()


async def test_run_ads_call_passes_op_count_to_quota(monkeypatch):
    """run_ads_call прокидывает op_count в quota.record (учёт батча, §3). Фейки async — resilience
    делает `await quota.record/check_mutation_allowed`."""
    from core import resilience

    seen: dict = {}

    async def _fake_record(account=None, *, kind="read", count=1):
        seen.update(account=account, kind=kind, count=count)

    async def _ok(account=None):
        return None

    monkeypatch.setattr(quota, "record", _fake_record)
    monkeypatch.setattr(quota, "check_mutation_allowed", _ok)

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

    async def _fake_record(account=None, *, kind="read", count=1):
        seen.update(account=account, kind=kind, count=count)

    async def _ok(account=None):
        return None

    monkeypatch.setattr(quota, "record", _fake_record)
    monkeypatch.setattr(quota, "check_mutation_allowed", _ok)

    def _sdk_create():
        return {"campaign": "customers/1/campaigns/2"}

    res = await resilience.run_ads_create_call(_sdk_create, account="777", op_count=17)
    assert res == {"campaign": "customers/1/campaigns/2"}
    assert seen == {"account": "777", "kind": "mutate", "count": 17}

    # Блок квоты — ДО SDK: сам вызов не должен исполниться.
    async def _boom(account=None):
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

    async def _ok(account=None):
        return None

    async def _rec(*a, **kw):
        return None

    monkeypatch.setattr(quota, "check_mutation_allowed", _ok)
    monkeypatch.setattr(quota, "record", _rec)
    attempts: list = []

    def _flaky():
        attempts.append(1)
        raise gapi.InternalServerError("транзиент")  # для run_ads_call это ретраебл

    with pytest.raises(gapi.InternalServerError):
        await resilience.run_ads_create_call(_flaky)
    assert len(attempts) == 1  # ровно одна попытка — без backoff-повторов
