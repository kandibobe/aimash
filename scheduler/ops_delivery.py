"""Scheduler adapter for durable operational notifications.

Only Telegram is wired in the current runtime.  Other channels remain injected transports; rows for
an unconfigured channel retry and dead-letter visibly instead of being acknowledged as delivered.
"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta

from core.config import settings
from core.logging import log
from operations.outbox import DeliveryResult, deliver_outbox, enqueue_due_escalations
from operations.routing import AlertRouter, Notification
from scheduler import transport

_CHAT_ID_RE = re.compile(r"-?[1-9][0-9]{4,19}")
_TELEGRAM_TEXT_LIMIT = 4096


class TelegramConfigTransport:
    """Resolve a route's env reference at send time without logging or persisting its value."""

    def __init__(self, bot) -> None:
        self._bot = bot

    async def send(self, *, destination_ref: str, notification: Notification) -> None:
        raw = os.getenv(destination_ref, "").strip()
        if _CHAT_ID_RE.fullmatch(raw) is None:
            raise RuntimeError("telegram destination reference is missing or invalid")
        text = (
            f"<b>{html.escape(notification.severity.upper())}: "
            f"{html.escape(notification.title)}</b>\n{html.escape(notification.body)}"
        )[:_TELEGRAM_TEXT_LIMIT]
        await transport.send_bot_message(
            self._bot,
            int(raw),
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def run_notification_delivery(
    bot,
    *,
    now: datetime | None = None,
) -> DeliveryResult:
    """Enqueue due incidents and drain one leased batch; no Ads mutation is reachable here."""
    enqueued = await enqueue_due_escalations(
        now=now,
        critical_after=timedelta(minutes=settings.incident_critical_escalation_minutes),
        warning_after=timedelta(minutes=settings.incident_warning_escalation_minutes),
        cooldown=timedelta(minutes=settings.incident_escalation_cooldown_minutes),
    )
    router = AlertRouter({"telegram": TelegramConfigTransport(bot)})
    result = await deliver_outbox(
        router,
        now=now,
        lease_for=timedelta(seconds=settings.notification_outbox_lease_seconds),
        max_attempts=settings.notification_outbox_max_attempts,
        base_delay=timedelta(seconds=settings.notification_outbox_base_retry_seconds),
    )
    if enqueued or result.claimed:
        level = log.error if result.dead or result.lost_lease else log.info
        level(
            "notification outbox: enqueued=%d claimed=%d delivered=%d retry=%d dead=%d cancelled=%d lost_lease=%d",
            len(enqueued),
            result.claimed,
            result.delivered,
            result.retrying,
            result.dead,
            result.cancelled,
            result.lost_lease,
        )
    return result
