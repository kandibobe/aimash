"""§20.4 (Фаза D): журнал краулинга (clients.crawl_jobs) + реконсиляция зависших (scheduler).

Проверяем: running→done/failed; guard (mark_* только из running); reconcile_stale_crawls флипает
зависшие running (старше TTL) в failed (in-process задача умерла с процессом на рестарте).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients import crawl_jobs  # noqa: E402
from db.models import CrawlJob  # noqa: E402
from db.session import Session, init_db  # noqa: E402
from scheduler.jobs import reconcile_stale_crawls  # noqa: E402


@pytest.mark.asyncio
async def test_lifecycle_running_to_done():
    await init_db()
    job = await crawl_jobs.create_running(
        customer_id="5000000001", chat_id=1, domain="ex.com", mode="full"
    )
    assert await crawl_jobs.get_status(job) == "running"
    await crawl_jobs.mark_done(job, pages_crawled=7)
    assert await crawl_jobs.get_status(job) == "done"
    async with Session() as s:
        row = (await s.execute(select(CrawlJob).where(CrawlJob.job_id == job))).scalar_one()
    assert row.pages_crawled == 7 and row.finished_at is not None


@pytest.mark.asyncio
async def test_lifecycle_running_to_failed_redacts_error():
    await init_db()
    job = await crawl_jobs.create_running(customer_id="5000000002", chat_id=1, domain="ex.com")
    await crawl_jobs.mark_failed(job, error="timeout while fetching")
    assert await crawl_jobs.get_status(job) == "failed"


@pytest.mark.asyncio
async def test_mark_done_guarded_after_terminal():
    await init_db()
    job = await crawl_jobs.create_running(customer_id="5000000003", chat_id=1, domain="ex.com")
    await crawl_jobs.mark_failed(job, error="boom")
    await crawl_jobs.mark_done(job, pages_crawled=99)  # не должен «оживить» терминальный статус
    assert await crawl_jobs.get_status(job) == "failed"


@pytest.mark.asyncio
async def test_reconcile_flips_stale_running_to_failed():
    await init_db()
    job = await crawl_jobs.create_running(customer_id="5000000004", chat_id=1, domain="stale.com")
    # смотрим из БУДУЩЕГО: cutoff = future - 30мин ещё позже created_at → задача «зависла»
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    n = await reconcile_stale_crawls(now=future, stale_minutes=30)
    assert n >= 1
    assert await crawl_jobs.get_status(job) == "failed"


@pytest.mark.asyncio
async def test_reconcile_leaves_fresh_running():
    await init_db()
    job = await crawl_jobs.create_running(customer_id="5000000005", chat_id=1, domain="fresh.com")
    # текущий момент: свежая задача НЕ старше 30 мин → не трогаем
    n = await reconcile_stale_crawls(stale_minutes=30)
    assert await crawl_jobs.get_status(job) == "running"
    assert isinstance(n, int)
