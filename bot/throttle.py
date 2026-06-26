"""Анти-спам на стороне бота (ТЗ §12: «Rate limiting на стороне бота»).

Token-bucket по chat_id: до `burst` сообщений подряд, дальше — пополнение `rate` токенов/сек.
Троттлим ТОЛЬКО сообщения (команды/свободный текст → дорогой LLM-путь и нагрузка на Google Ads);
inline-колбэки (подтверждения ✅/❌) НЕ троттлим, чтобы не ломать confirm-флоу. Превышение —
мягкое предупреждение не чаще `warn_cooldown` (без спама ответами) и дроп апдейта.

Регистрируется как outer-middleware ПОСЛЕ whitelist (сначала отсекаем чужих, потом лимитируем
своих). Состояние — in-memory (теряется при рестарте, для анти-спама это ок).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


def _chat_id(event: TelegramObject) -> int | None:
    """chat_id из Message-подобного события (дакт-тайпинг — работает и с фейками в тестах)."""
    chat = getattr(event, "chat", None)
    if chat is not None:
        return getattr(chat, "id", None)
    msg = getattr(event, "message", None)  # на случай регистрации на колбэках
    if msg is not None:
        return getattr(getattr(msg, "chat", None), "id", None)
    return None


class ThrottleMiddleware(BaseMiddleware):
    def __init__(
        self,
        rate: float = 0.7,  # токенов/сек устойчиво (~1 сообщение за 1.4с)
        burst: int = 5,  # допустимый всплеск подряд
        warn_cooldown: float = 4.0,  # не чаще раза в N сек напоминаем «слишком часто»
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.rate = rate
        self.burst = float(burst)
        self.warn_cooldown = warn_cooldown
        self._clock = clock or time.monotonic
        self._tokens: dict[int, float] = {}
        self._last: dict[int, float] = {}
        self._warned: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat_id = _chat_id(event)
        if chat_id is None:
            return await handler(event, data)

        now = self._clock()
        last = self._last.get(chat_id, now)
        tokens = min(self.burst, self._tokens.get(chat_id, self.burst) + (now - last) * self.rate)
        self._last[chat_id] = now

        if tokens < 1.0:
            self._tokens[chat_id] = tokens
            warned = self._warned.get(chat_id)  # None ⇒ ещё не предупреждали → предупреждаем
            answer = getattr(event, "answer", None)
            if callable(answer) and (warned is None or now - warned >= self.warn_cooldown):
                self._warned[chat_id] = now
                try:
                    await answer("⏳ Слишком часто — подожди пару секунд.")
                except Exception:  # предупреждение необязательно
                    pass
            return  # дроп: апдейт не доходит до хендлера

        self._tokens[chat_id] = tokens - 1.0
        return await handler(event, data)
