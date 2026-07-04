"""Запуск планировщика (APScheduler AsyncIOScheduler) в общем event loop бота.

⛔ Golden rule #3: планировщик НЕ меняет аккаунт — только чтение, уведомления и очистка
просроченных черновиков (см. scheduler.jobs). Вызывать из bot.main.main() при запущенном loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import settings
from core.errors import capture_exception
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


async def register_user_report_schedules(sched: AsyncIOScheduler, bot) -> int:
    """§14 (P1-I): персональные расписания планового отчёта. Читает UserSettings.report_schedule
    (crontab-строки) и регистрирует per-chat cron-джобу run_scheduled_report(bot, only_chat=chat)
    для каждого оператора со своим расписанием. Глобальная джоба таких операторов ПРОПУСКАЕТ
    (_custom_report_chats) — без дубля. Оживляет ранее «мёртвую» колонку report_schedule.

    Вызывать ПОСЛЕ setup_scheduler в async-контексте старта (нужна БД). Невалидный crontab у
    оператора — НЕ роняем (лог + пропуск, как report_trigger). Возвращает число зарегистрированных.
    Runtime-изменения расписания подхватываются на рестарте (или повторным вызовом из /refresh)."""
    from sqlalchemy import select

    from core.config import settings as _settings
    from db.models import UserSettings
    from db.session import Session

    wl = set(_settings.whitelist)
    if not wl:
        return 0
    async with Session() as s:
        rows = (
            await s.execute(
                select(UserSettings.chat_id, UserSettings.report_schedule).where(
                    UserSettings.report_schedule.isnot(None)
                )
            )
        ).all()
    n = 0
    for chat_id, cron in rows:
        if int(chat_id) not in wl or not (cron or "").strip():
            continue
        try:
            trig = CronTrigger.from_crontab(cron.strip())
        except (ValueError, TypeError) as e:
            log.warning(
                "per-user report cron chat=%s=%r невалиден (%s) — пропуск",
                chat_id,
                cron,
                type(e).__name__,
            )
            continue
        sched.add_job(
            jobs.run_scheduled_report,
            trig,
            args=[bot],
            kwargs={"only_chat": int(chat_id)},
            id=f"scheduled_report_{int(chat_id)}",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        n += 1
    if n:
        log.info("scheduler: персональных расписаний отчёта зарегистрировано: %d", n)
    return n


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
    sched.add_job(
        # §12: зависшие executing-черновики (процесс упал посреди мутации) → needs_review +
        # уведомление владельца. next_run_time=now: рестарт — именно момент рождения зависших,
        # прогоняем сразу, не ждём первый интервал.
        jobs.reconcile_stale_executing,
        IntervalTrigger(minutes=settings.cleanup_interval_minutes),
        args=[bot],
        id="reconcile_stale_executing",
        replace_existing=True,
        misfire_grace_time=600,
        next_run_time=datetime.now(timezone.utc),
    )
    # §advisor Слой B: замер результата применённых рекомендаций (read-only, delta+verdict в КОДЕ).
    sched.add_job(
        jobs.run_recommendation_followups,
        IntervalTrigger(hours=24),
        args=[bot],
        id="advise_followups",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # §advisor: проактивные рекомендации операторам с opt-in (ui_prefs.advise_proactive). READ-ONLY,
    # не создаёт proposal. Ежедневно; по умолчанию НИКОМУ (fail-closed к анти-спаму).
    sched.add_job(
        jobs.run_recommendations_digest,
        CronTrigger(hour=9, minute=30),
        args=[bot],
        id="advise_digest",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # A1 (§15): проактивный алерт админам о НОВЫХ error_events. Регистрируем ТОЛЬКО если фича включена
    # (ERROR_ALERT_INTERVAL_MINUTES > 0) — иначе no-op-джоба зря крутилась бы. next_run_time=now:
    # первый прогон на старте ставит базлайн (max id), чтобы не спамить историей и ловить всё СВЕЖЕЕ.
    if settings.error_alert_interval_minutes > 0:
        sched.add_job(
            jobs.run_error_alerts,
            IntervalTrigger(minutes=settings.error_alert_interval_minutes),
            args=[bot],
            id="error_alerts",
            replace_existing=True,
            misfire_grace_time=300,
            next_run_time=datetime.now(timezone.utc),
        )
    # C2 (§15): ретеншн-очистка растущих таблиц (error_events/crawl_jobs) — удаление старых строк
    # (cleanup-джобы выше лишь меняют статус). audit_log НЕ трогаем (денежный реестр). Кадэнс — раз в
    # сутки достаточно (объём растёт медленно). Регистрируем всегда: пороги=0 внутри → no-op.
    sched.add_job(
        jobs.purge_stale_rows,
        IntervalTrigger(hours=24),
        id="purge_stale_rows",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # §15 (P2-c): исключение ВЕРХНЕГО уровня джобы (вне per-account try) — в capture_exception/Sentry
    # + /diag, а не только в лог APScheduler. Листенер синхронный → планируем async-фиксацию в loop.
    def _on_job_error(event) -> None:
        exc = getattr(event, "exception", None)
        if exc is None:
            return
        try:
            asyncio.get_running_loop().create_task(
                capture_exception(exc, where=f"scheduler:{event.job_id}")
            )
        except RuntimeError:  # нет running loop (не должно случаться для AsyncIOScheduler)
            log.warning("scheduler job %s упала, но loop недоступен для capture", event.job_id)

    sched.add_listener(_on_job_error, EVENT_JOB_ERROR)
    sched.start()
    log.info(
        "scheduler запущен: отчёт cron=%r, аномалии каждые %dч, очистка каждые %dмин (read-only)",
        settings.report_schedule,
        settings.anomaly_interval_hours,
        settings.cleanup_interval_minutes,
    )
    return sched
