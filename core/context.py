"""Контекст наблюдаемости ОДНОГО запроса: корреляционный request_id (+ chat_id/operation),
который сшивает все лог-строки одного апдейта/джобы в единую цепочку (§15).

Зеркалит паттерн bot.i18n._LANG: единый contextvar + set/reset в middleware (TraceMiddleware на
Telegram-пути, request_scope в scheduler-джобах). core.logging.ContextFilter впрыскивает эти поля
в КАЖДУЮ лог-запись — поэтому существующие log.* (resilience, router, хендлеры, on_error) получают
request_id БЕЗ правок call-site. request_id — короткий (8 hex), НЕ секрет: его можно показать
пользователю как «код инцидента» для связи лога с обращением.

contextvars изолированы по asyncio-таске (aiogram гонит апдейты разными тасками) → request_id
одного пользователя не «протекает» в обработку другого.
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Iterator


@dataclass(frozen=True)
class ReqContext:
    request_id: str = "-"
    chat_id: int | None = None
    operation: str = "-"


_CTX: contextvars.ContextVar[ReqContext] = contextvars.ContextVar(
    "aimash_req_ctx", default=ReqContext()
)


def new_request_id() -> str:
    """Короткий корреляционный id (8 hex). НЕ секрет — безопасен в логах и в сообщении пользователю."""
    return uuid.uuid4().hex[:8]


def get_context() -> ReqContext:
    """Контекст ТЕКУЩЕГО запроса (дефолт — пустой '-' вне апдейта/джобы)."""
    return _CTX.get()


def set_context(
    *,
    request_id: str | None = None,
    chat_id: int | None = None,
    operation: str | None = None,
) -> contextvars.Token:
    """Слить переданные поля в текущий контекст; вернуть Token для reset (как i18n.set_current_lang).
    Непереданные (None) поля сохраняются из текущего контекста."""
    cur = _CTX.get()
    merged = replace(
        cur,
        request_id=cur.request_id if request_id is None else request_id,
        chat_id=cur.chat_id if chat_id is None else chat_id,
        operation=cur.operation if operation is None else operation,
    )
    return _CTX.set(merged)


def reset_context(token: contextvars.Token) -> None:
    """Снять контекст запроса (в finally middleware/джобы), вернув предыдущее значение."""
    _CTX.reset(token)


@contextmanager
def request_scope(operation: str, *, chat_id: int | None = None) -> Iterator[str]:
    """Открыть новый корреляционный scope (свежий request_id) на время блока — для путей БЕЗ
    Telegram-middleware (scheduler-джобы, скрипты). Возвращает request_id. Сброс в finally.

    contextvar.set держится сквозь await внутри блока (та же asyncio-таска), поэтому корректно
    оборачивает и асинхронное тело: `with request_scope('job'): await do()`."""
    token = set_context(request_id=new_request_id(), chat_id=chat_id, operation=operation)
    try:
        yield _CTX.get().request_id
    finally:
        reset_context(token)
