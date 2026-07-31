"""§20.4: персист журнала краулинга (crawl_jobs). Чистые DB-примитивы — без бота, сети и краула.

Жизненный цикл: create_running → mark_done | mark_failed. Зависшие running (in-process задача
умерла с процессом на рестарте) реконсилятся в failed отдельной scheduler-джобой
(scheduler.jobs.reconcile_stale_crawls). error редактируется (redact_text) — без секретов/PII.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from core.logging import redact_text
from db.models import CrawlJob
from db.session import Session


async def create_running(*, customer_id: str, chat_id: int, domain: str, mode: str = "full") -> str:
    """Создать строку задачи в статусе running и вернуть job_id (hex uuid)."""
    job_id = uuid.uuid4().hex
    async with Session() as s:
        s.add(
            CrawlJob(
                job_id=job_id,
                customer_id=customer_id,
                chat_id=chat_id,
                domain=domain,
                mode=mode,
                status="running",
            )
        )
        await s.commit()
    return job_id


async def mark_done(job_id: str, *, pages_crawled: int) -> None:
    async with Session() as s:
        j = (
            await s.execute(select(CrawlJob).where(CrawlJob.job_id == job_id))
        ).scalar_one_or_none()
        if j is not None and j.status == "running":
            j.status = "done"
            j.pages_crawled = int(pages_crawled)
            j.finished_at = func.now()
            await s.commit()


async def mark_failed(job_id: str, *, error: str) -> None:
    async with Session() as s:
        j = (
            await s.execute(select(CrawlJob).where(CrawlJob.job_id == job_id))
        ).scalar_one_or_none()
        if j is not None and j.status == "running":
            j.status = "failed"
            j.error = redact_text(str(error))[:2000]  # без секретов/PII (golden rule #5)
            j.finished_at = func.now()
            await s.commit()


async def get_status(job_id: str, *, customer_id: str | None = None) -> str | None:
    """Текущий статус задачи (для тестов/диагностики). None — задачи нет."""
    conditions = [CrawlJob.job_id == job_id]
    if customer_id is not None:
        conditions.append(CrawlJob.customer_id == str(customer_id))
    async with Session() as s:
        return (await s.execute(select(CrawlJob.status).where(*conditions))).scalar_one_or_none()
