"""Единая точка обработки исключения (§15): «поймать → залогировать (редактированно, с request_id)
→ СОХРАНИТЬ ErrorEvent для триажа». Зовут глобальный error-хендлер бота (on_error) и
scheduler-джобы. Никогда не бросает (наблюдаемость не должна ронять рантайм).

Связь с остальной телеметрией:
  • log.error(exc_info=...) пишет редактированный traceback в логи И уходит в Sentry
    (LoggingIntegration на event_level=ERROR), если включён SENTRY_DSN — без правок здесь;
  • request_id берётся из core.context (его ставит TraceMiddleware/request_scope) и сшивает
    запись ошибки со всеми логами этого апдейта; его же возвращаем, чтобы показать пользователю
    как «код инцидента» (он не секрет);
  • в БД (error_events) и в логи попадает ТОЛЬКО редактированный текст (golden rule #5).
"""

from __future__ import annotations

import traceback

from core.context import get_context, reset_context, set_context, stashed_context
from core.logging import log, redact_text

_TB_MAX = 8000  # потолок длины traceback в БД (защита error_events от распухания)
_MSG_MAX = 2000


async def capture_exception(exc: BaseException, *, where: str) -> str:
    """Залогировать исключение (exc_info → лог + Sentry, редактированно) и сохранить ErrorEvent
    для /diag. Возвращает request_id текущего контекста («код инцидента» для пользователя).
    Best-effort: персист в БД обёрнут в try — сбой записи НЕ ломает обработку ошибки (лог уже есть).
    `where` — точка перехвата (напр. 'handler', 'scheduler:report')."""
    # aiogram-путь: on_error бежит уже ПОСЛЕ сброса contextvar в TraceMiddleware.finally, поэтому
    # get_context() тут вернул бы дефолт '-'. Если исключение несёт снимок (stash_context_on) и
    # текущий контекст пуст — восстанавливаем его на время лога/персиста, чтобы и лог-строка
    # (ContextFilter), и error_events, и «код инцидента» несли реальный request_id/chat_id, а не '-'.
    snap = stashed_context(exc)
    token = None
    if snap is not None and get_context().request_id == "-":
        token = set_context(
            request_id=snap.request_id,
            chat_id=snap.chat_id,
            operation=snap.operation,
            customer_id=snap.customer_id,
        )
    try:
        ctx = get_context()
        # request_id/chat попадут в строку лога автоматически (core.logging.ContextFilter).
        log.error("ошибка в %s: %s", where, type(exc).__name__, exc_info=exc)
        try:
            await _persist(
                exc,
                where=where,
                request_id=ctx.request_id,
                chat_id=ctx.chat_id,
                customer_id=ctx.customer_id,
            )
        except Exception:  # noqa: BLE001 — персист не критичен; ошибка уже залогирована/в Sentry
            log.warning("error-capture: ErrorEvent не сохранён (%s)", type(exc).__name__)
        return ctx.request_id
    finally:
        if token is not None:
            reset_context(token)


async def _persist(
    exc: BaseException,
    *,
    where: str,
    request_id: str,
    chat_id: int | None,
    customer_id: str | None = None,
) -> None:
    """Записать редактированный снимок ошибки в error_events (для /diag и пост-разбора)."""
    from db.models import ErrorEvent
    from db.session import Session

    tb = redact_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    async with Session() as s:
        s.add(
            ErrorEvent(
                request_id=request_id[:16],
                chat_id=chat_id,
                customer_id=(customer_id or None),
                where=where[:160],
                exc_type=type(exc).__name__[:100],
                message=redact_text(str(exc))[:_MSG_MAX],
                traceback=tb[:_TB_MAX],
            )
        )
        await s.commit()
