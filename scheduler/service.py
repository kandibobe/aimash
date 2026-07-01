"""Запуск планировщика (APScheduler AsyncIOScheduler) в общем event loop бота.

⛔ Golden rule #3: планировщик НЕ меняет аккаунт — только чтение, уведомления и очистка
просроченных черновиков (см. scheduler.jobs). Вызывать из bot.main.main() при запущенном loop.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import settings
from core.logging import log
from scheduler import jobs

# Дефолт планового отчёта — fallback, если REPORT_SCHEDULE невалиден (см. report_trigger).
_DEFAULT_REPORT_CRON = {"hour": 9, "minute": 0}  # ежедневно 09:00 (локальное время)


def report_trigger() -> CronTrigger:
    """CronTrigger планового отчёта из settings.report_schedule (стандартная crontab-строка —
    покрывает ежедн./еженед., ТЗ §14). Невалидную строку НЕ роняем стартом (расписание — не
    security-гейт): откат на ежедневно 09:00 + громкий лог (fail-safe). Изолировано → тестируемо."""
    raw = (settings.report_schedule or "").strip()
    try:
        return CronTrigger.from_crontab(raw)
    except (ValueError, TypeError) as e:
        log.warning(
            "REPORT_SCHEDULE=%r невалиден (%s) — откат на ежедневно 09:00",
            raw,
            type(e).__name__,
        )
        return CronTrigger(**_DEFAULT_REPORT_CRON)


def setup_scheduler(bot) -> AsyncIOScheduler:
    """Создать и ЗАПУСТИТЬ планировщик в текущем (running) event loop. Возвращает scheduler
    (для graceful shutdown). Задачи — только read/notify/cleanup, без mutations. Кадэнс — из env
    (§14): REPORT_SCHEDULE / ANOMALY_INTERVAL_HOURS / CLEANUP_INTERVAL_MINUTES."""
    sched = AsyncIOScheduler()
    sched.add_job(
        jobs.run_scheduled_report,
        report_trigger(),
        args=[bot],
        id="scheduled_report",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    sched.add_job(
        jobs.run_anomaly_check,
        IntervalTrigger(hours=settings.anomaly_interval_hours),
        args=[bot],
        id="anomaly_check",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    sched.add_job(
        jobs.cleanup_stale_proposals,
        IntervalTrigger(minutes=settings.cleanup_interval_minutes),
        id="cleanup_stale",
        replace_existing=True,
        misfire_grace_time=600,
    )
    sched.add_job(
        jobs.cleanup_stale_campaign_drafts,
        IntervalTrigger(minutes=settings.cleanup_interval_minutes),
        id="cleanup_stale_drafts",
        replace_existing=True,
        misfire_grace_time=600,
    )
    sched.add_job(
        jobs.reconcile_stale_crawls,  # §20.4: зависшие running-краулы (после рестарта) → failed
        IntervalTrigger(minutes=settings.cleanup_interval_minutes),
        id="reconcile_stale_crawls",
        replace_existing=True,
        misfire_grace_time=600,
    )
    sched.start()
    log.info(
        "scheduler запущен: отчёт cron=%r, аномалии каждые %dч, очистка каждые %dмин (read-only)",
        settings.report_schedule,
        settings.anomaly_interval_hours,
        settings.cleanup_interval_minutes,
    )
    return sched
