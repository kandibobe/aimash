"""Запуск планировщика (APScheduler AsyncIOScheduler) в общем event loop бота.

⛔ Golden rule #3: планировщик НЕ меняет аккаунт — только чтение, уведомления и очистка
просроченных черновиков (см. scheduler.jobs). Вызывать из bot.main.main() при запущенном loop.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.logging import log
from scheduler import jobs

# Кадэнс по умолчанию (позже — настраиваемо через UserSettings/настройки):
REPORT_CRON = {"hour": 9, "minute": 0}  # ежедневный плановый отчёт в 09:00 (локальное время)
ANOMALY_INTERVAL_HOURS = 6  # проверка аномалий каждые 6 ч
CLEANUP_INTERVAL_MINUTES = 60  # очистка просроченных черновиков ежечасно


def setup_scheduler(bot) -> AsyncIOScheduler:
    """Создать и ЗАПУСТИТЬ планировщик в текущем (running) event loop. Возвращает scheduler
    (для graceful shutdown). Задачи — только read/notify/cleanup, без mutations."""
    sched = AsyncIOScheduler()
    sched.add_job(
        jobs.run_scheduled_report,
        CronTrigger(**REPORT_CRON),
        args=[bot],
        id="scheduled_report",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    sched.add_job(
        jobs.run_anomaly_check,
        IntervalTrigger(hours=ANOMALY_INTERVAL_HOURS),
        args=[bot],
        id="anomaly_check",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    sched.add_job(
        jobs.cleanup_stale_proposals,
        IntervalTrigger(minutes=CLEANUP_INTERVAL_MINUTES),
        id="cleanup_stale",
        replace_existing=True,
        misfire_grace_time=600,
    )
    sched.start()
    log.info(
        "scheduler запущен: отчёт %s, аномалии каждые %dч, очистка каждые %dмин (read-only)",
        REPORT_CRON,
        ANOMALY_INTERVAL_HOURS,
        CLEANUP_INTERVAL_MINUTES,
    )
    return sched
