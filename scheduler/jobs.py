"""Задачи планировщика: плановые отчёты, проверка аномалий, очистка просроченных черновиков.

READ-ONLY + уведомления. ⛔ НИКОГДА не импортирует ads.mutations и не вызывает
execute_confirmed / apply_* — планировщик не меняет аккаунт (golden rule #3). Очистка лишь
ОТКЛОНЯЕТ (reject, с аудитом) старые pending-черновики: они не подтверждались → SDK не звался →
деньги не тратились, отклонить безопасно.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ads.client import DRAFT_ACCOUNT_ID, build_client
from confirm.store import ConfirmStore
from core.config import settings
from core.context import request_scope
from core.errors import capture_exception
from core.logging import log
from db.models import Proposal, UserSettings
from db.session import Session
from reports.period import last_n_days
from reports.queries import fetch_totals
from reports.service import build_account_report_async, summary_text
from scheduler.anomaly import detect_anomalies

REPORT_WINDOW_DAYS = 7  # окно планового отчёта
ANOMALY_WINDOW_DAYS = 7  # окно сравнения для аномалий (текущие N дн. vs предыдущие N дн.)
PROPOSAL_TTL_HOURS = 24  # сколько живёт неподтверждённый черновик до авто-отклонения


def _recipients() -> set[int]:
    """Кому слать: доверенные whitelisted-пользователи (операторы бота)."""
    return set(settings.whitelist)


async def _broadcast(bot, text: str, **kw) -> None:
    for chat_id in _recipients():
        try:
            await bot.send_message(chat_id, text, **kw)
        except Exception as e:  # один недоступный чат не должен ронять рассылку
            log.warning("scheduler: не доставлено в %s: %s: %s", chat_id, type(e).__name__, e)


async def run_scheduled_report(bot) -> None:
    """Плановый отчёт (последние N дн.) — рассылка whitelisted-пользователям. READ-ONLY."""
    with request_scope("scheduler:report"):  # §15: корреляция логов джобы по request_id
        if not _recipients():
            log.info("scheduler: получателей нет (whitelist пуст) — пропуск планового отчёта")
            return
        try:
            client = build_client()
            period = last_n_days(REPORT_WINDOW_DAYS)
            report = await build_account_report_async(client, DRAFT_ACCOUNT_ID, period)
        except Exception as e:  # сеть/доступ/SDK — не валим планировщик, но фиксируем (§15)
            await capture_exception(e, where="scheduler:report")
            return
        await _broadcast(bot, "🗓 Плановый отчёт\n\n" + summary_text(report))


async def _thresholds_by_chat(chat_ids: set[int]) -> dict[int, dict | None]:
    """Пороги аномалий per-chat из UserSettings.alert_thresholds (JSON). Чата нет в таблице →
    None (detect_anomalies возьмёт DEFAULT_THRESHOLDS). Один запрос на всех получателей.
    READ-ONLY: только чтение настроек, аккаунт не трогается (golden rule #3)."""
    if not chat_ids:
        return {}
    async with Session() as s:
        rows = (
            await s.execute(
                select(UserSettings.chat_id, UserSettings.alert_thresholds).where(
                    UserSettings.chat_id.in_(chat_ids)
                )
            )
        ).all()
    return {cid: thr for cid, thr in rows}


def _format_alerts(alerts) -> str:
    return (
        f"🔔 <b>Аномалии</b> (за {ANOMALY_WINDOW_DAYS} дн. к предыдущему периоду):\n"
        + "\n".join("• " + a.message for a in alerts)
        + "\n\n<i>Это только сигнал — сам я ничего не меняю. Реши и дай команду.</i>"
    )


async def run_anomaly_check(bot) -> None:
    """Сравнение последних N дн. с предыдущими; алерт при росте расхода/падении конверсий.
    Пороги берутся per-chat из UserSettings.alert_thresholds (иначе — дефолтные). READ-ONLY:
    только уведомление, никаких изменений аккаунта (golden rule #3)."""
    with request_scope("scheduler:anomaly"):  # §15: корреляция логов джобы по request_id
        recipients = _recipients()
        if not recipients:
            return
        try:
            client = build_client()
            period = last_n_days(ANOMALY_WINDOW_DAYS)
            cur = await asyncio.to_thread(fetch_totals, client, DRAFT_ACCOUNT_ID, period)
            prev = await asyncio.to_thread(
                fetch_totals, client, DRAFT_ACCOUNT_ID, period.previous()
            )
        except Exception as e:  # сеть/доступ/SDK — фиксируем для триажа (§15), планировщик жив
            await capture_exception(e, where="scheduler:anomaly")
            return
        thresholds = await _thresholds_by_chat(recipients)
        # Пороги per-chat → алерты считаем для каждого получателя отдельно (метрики аккаунта общие).
        for chat_id in recipients:
            alerts = detect_anomalies(cur, prev, thresholds.get(chat_id))
            if not alerts:
                continue
            try:
                await bot.send_message(chat_id, _format_alerts(alerts), parse_mode="HTML")
            except Exception as e:  # один недоступный чат не должен ронять остальные
                log.warning(
                    "scheduler: алерт не доставлен в %s: %s: %s", chat_id, type(e).__name__, e
                )


async def cleanup_stale_proposals(
    *, now: datetime | None = None, ttl_hours: int = PROPOSAL_TTL_HOURS
) -> int:
    """Просроченные pending-черновики → reject (с аудитом). Возвращает число отклонённых.

    Сравнение возраста — в Python (а не в SQL), чтобы корректно работать и на SQLite (наивный
    UTC), и на Postgres (tz-aware): наивный created_at трактуем как UTC."""
    with request_scope("scheduler:cleanup"):  # §15: корреляция логов джобы по request_id
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=ttl_hours)
        store = ConfirmStore()
        async with Session() as s:
            rows = (
                (await s.execute(select(Proposal).where(Proposal.status == "pending")))
                .scalars()
                .all()
            )
            stale: list[tuple[str, int]] = []
            for p in rows:
                created = p.created_at
                if created is None:
                    continue
                if created.tzinfo is None:  # SQLite хранит наивный UTC
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    stale.append((p.confirmation_id, p.chat_id))
        for cid, chat_id in stale:
            await store.reject(cid, chat_id=chat_id)  # pending→rejected + audit (одноразово)
        if stale:
            log.info("scheduler: отклонено просроченных черновиков: %d", len(stale))
        return len(stale)
