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
    customer_id: str | None = (
        None  # §8: какой аккаунт обрабатывается (для per-account триажа/тегов)
    )


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
    customer_id: str | None = None,
) -> contextvars.Token:
    """Слить переданные поля в текущий контекст; вернуть Token для reset (как i18n.set_current_lang).
    Непереданные (None) поля сохраняются из текущего контекста."""
    cur = _CTX.get()
    merged = replace(
        cur,
        request_id=cur.request_id if request_id is None else request_id,
        chat_id=cur.chat_id if chat_id is None else chat_id,
        operation=cur.operation if operation is None else operation,
        customer_id=cur.customer_id if customer_id is None else customer_id,
    )
    return _CTX.set(merged)


def reset_context(token: contextvars.Token) -> None:
    """Снять контекст запроса (в finally middleware/джобы), вернув предыдущее значение."""
    _CTX.reset(token)


# Атрибут, под которым снимок req-контекста прикрепляется к исключению. Нужен для aiogram-пути:
# глобальный on_error бежит УЖЕ ПОСЛЕ того, как TraceMiddleware.finally сбросил contextvar (он
# внутри update-уровневой ErrorsMiddleware) → get_context() там вернул бы дефолт '-'. Прикрепив
# снимок к самому исключению, даём capture_exception восстановить реальный request_id/chat_id.
_EXC_CTX_ATTR = "_aimash_req_ctx"


def stash_context_on(exc: BaseException) -> None:
    """Прикрепить снимок ТЕКУЩЕГО req-контекста к исключению (в except middleware, пока contextvar
    ещё жив) — чтобы обработчик, бегущий уже вне scope, восстановил «код инцидента», а не дефолт '-'.
    Best-effort: неустановимый атрибут (экзотическое C-исключение) не должен ронять обработку."""
    try:
        exc._aimash_req_ctx = _CTX.get()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — прикрепление контекста необязательно
        pass


def stashed_context(exc: BaseException) -> "ReqContext | None":
    """Снимок req-контекста, прикреплённый stash_context_on (или None, если его нет)."""
    ctx = getattr(exc, _EXC_CTX_ATTR, None)
    return ctx if isinstance(ctx, ReqContext) else None


@contextmanager
def request_scope(
    operation: str, *, chat_id: int | None = None, customer_id: str | None = None
) -> Iterator[str]:
    """Открыть новый корреляционный scope (свежий request_id) на время блока — для путей БЕЗ
    Telegram-middleware (scheduler-джобы, скрипты). Возвращает request_id. Сброс в finally.

    contextvar.set держится сквозь await внутри блока (та же asyncio-таска), поэтому корректно
    оборачивает и асинхронное тело: `with request_scope('job'): await do()`."""
    token = set_context(
        request_id=new_request_id(), chat_id=chat_id, operation=operation, customer_id=customer_id
    )
    try:
        yield _CTX.get().request_id
    finally:
        reset_context(token)
