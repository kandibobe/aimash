"""Точка входа Telegram-бота (aiogram 3.x). Скелет — наполняется в фазе 0.

Whitelist chat_id, inline-кнопки подтверждения, передача свободного текста в агент.
"""
from __future__ import annotations

import asyncio

from core.config import settings
from core.logging import log, setup_logging


async def main() -> None:
    setup_logging()
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN не задан — заполни .env (фаза 0).")
        return
    log.info("Aimash bot: скелет. Хендлеры + whitelist + confirm-гейт — фаза 0/1.")
    # TODO(фаза 0): Dispatcher, whitelist-middleware, хендлеры команд/текста,
    #               inline ✅ Подтвердить / ❌ Отмена, интеграция agent.loop + confirm.gate.


if __name__ == "__main__":
    asyncio.run(main())
