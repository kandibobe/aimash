"""Telegram-бот Aimash (aiogram 3.x).

- whitelist по chat_id (TELEGRAM_WHITELIST_CHAT_IDS);
- свободный текст → агент-цикл (agent.loop.handle_command);
- read → показывает статистику; mutation → черновик «было→станет» + inline ✅/❌ (выполнение — Фаза 1);
- ничего не меняется без подтверждения «да».
"""
from __future__ import annotations

import asyncio

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from agent.loop import handle_command
from core.config import settings
from core.logging import log, setup_logging

# Очередь черновиков в памяти (Фаза 1 заменит на БД proposals + audit_log)
PENDING: dict[str, dict] = {}

dp = Dispatcher()


class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data):
        uid = None
        if isinstance(event, Message):
            uid = event.chat.id
        elif isinstance(event, CallbackQuery) and event.message:
            uid = event.message.chat.id
        wl = settings.whitelist
        if wl and uid not in wl:
            log.warning("заблокирован chat_id %s (не в whitelist)", uid)
            return
        return await handler(event, data)


@dp.message(CommandStart())
async def start(m: Message) -> None:
    await m.answer(
        "Aimash на связи. Пиши команды по-русски: «покажи статистику», "
        "«повысь бюджет кампании X на 20%». Любое изменение — только после твоего «да»."
    )


@dp.message(Command("help"))
async def help_(m: Message) -> None:
    await m.answer(
        "Я читаю Google Ads и предлагаю изменения. Перед любым изменением показываю "
        "«было → станет» и жду подтверждения. Просто пиши текстом."
    )


def _kb(cid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"ok:{cid}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"no:{cid}"),
    ]])


@dp.message(F.text)
async def on_text(m: Message) -> None:
    res = await handle_command(m.text, chat_id=m.chat.id)
    t = res.get("type")
    if t == "proposal":
        cid = res["confirmation_id"]
        PENDING[cid] = res
        await m.answer(f"Изменение (черновик):\n{res['summary']}\n\nПодтвердить?", reply_markup=_kb(cid))
    elif t == "clarify":
        await m.answer("❓ " + res["question"])
    elif t == "read":
        s = res.get("stats", {})
        await m.answer(
            f"📊 Аккаунт {res['account']} за {res['days']} дн.:\n"
            f"показы {s.get('impressions')}, клики {s.get('clicks')}, "
            f"расход {s.get('cost')}, конверсии {s.get('conversions')}"
        )
    else:
        await m.answer(res.get("text", "(пусто)"))


@dp.callback_query(F.data.startswith("ok:"))
async def confirm(cq: CallbackQuery) -> None:
    cid = cq.data[3:]
    p = PENDING.pop(cid, None)
    if not p:
        await cq.answer("Черновик не найден или устарел")
        return
    # Фаза 1: реальное выполнение через ads/mutations (с проверкой confirmation_id) + audit_log.
    await cq.message.edit_text(f"✅ Подтверждено:\n{p['summary']}\n\n(выполнение операции — Фаза 1)")
    await cq.answer("Подтверждено")


@dp.callback_query(F.data.startswith("no:"))
async def cancel(cq: CallbackQuery) -> None:
    PENDING.pop(cq.data[3:], None)
    await cq.message.edit_text("❌ Отменено")
    await cq.answer("Отменено")


async def main() -> None:
    setup_logging()
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN пуст — добавь в .env (токен у @BotFather).")
        return
    if not settings.whitelist:
        log.warning("whitelist пуст — бот будет отвечать ВСЕМ. Добавь TELEGRAM_WHITELIST_CHAT_IDS в .env.")
    dp.message.outer_middleware(WhitelistMiddleware())
    dp.callback_query.outer_middleware(WhitelistMiddleware())
    bot = Bot(settings.telegram_bot_token)
    log.info("Aimash bot запущен (polling).")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
