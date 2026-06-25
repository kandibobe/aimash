"""Telegram-бот Aimash (aiogram 3.x).

- whitelist по chat_id (TELEGRAM_WHITELIST_CHAT_IDS);
- свободный текст → агент-цикл (agent.loop.handle_command);
- read → показывает статистику; mutation → черновик «было→станет» (в БД) + inline ✅/❌;
- на «да» → реальное выполнение через ads.service за confirm-гейтом + audit; ничего без «да».
"""

from __future__ import annotations

import asyncio

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from ads.client import DRAFT_ACCOUNT_ID
from ads.service import execute_confirmed
from agent.loop import handle_command
from confirm.store import ConfirmStore
from core.config import settings
from core.logging import log, setup_logging
from db.session import init_db

STORE = ConfirmStore()  # черновики + audit в БД (SQLite dev), вместо очереди в памяти

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
        "«было → станет» и жду подтверждения. Просто пиши текстом.\n"
        "Доступно: бюджет, ставка (CPC), ключевые слова, минус-слова, пауза/возобновление.\n"
        "/campaigns — список кампаний (точные имена для команд).\n"
        "/report [7|30|90|MTD] — сводка по аккаунту за период (по умолчанию 30 дн.).\n"
        "/export [7|30|90|MTD] — глубокий отчёт .xlsx (разбивки по кампаниям/группам/ключам/…)."
    )


@dp.message(Command("campaigns"))
async def campaigns_(m: Message) -> None:
    """Read-only список кампаний. Полезно: имя кампании теперь обязательно для ставки/ключей."""
    try:
        from ads.client import build_client
        from ads.read import list_campaigns

        client = build_client()
        camps = await asyncio.to_thread(list_campaigns, client, DRAFT_ACCOUNT_ID)
    except Exception as e:  # сеть/доступ/SDK
        await m.answer(f"⚠️ Не удалось получить кампании: {type(e).__name__}: {e}")
        return
    if not camps:
        await m.answer("Кампаний нет.")
        return
    lines = "\n".join(f"• {c['name']} — {c['status']}" for c in camps)
    await m.answer(f"Кампании аккаунта {DRAFT_ACCOUNT_ID}:\n{lines}")


def _period_from_arg(arg: str | None):
    """Аргумент команды (7/30/90/MTD) → Period; по умолчанию 30 дн. Бросает ValueError."""
    from reports.period import from_preset

    return from_preset((arg or "30").strip() or "30")


@dp.message(Command("report"))
async def report_(m: Message, command: CommandObject) -> None:
    """Read-only сводка по аккаунту за период (итоги + сравнение + топ-кампании)."""
    try:
        period = _period_from_arg(command.args)
    except ValueError as e:
        await m.answer(f"⚠️ {e}")
        return
    try:
        from ads.client import build_client
        from reports.service import build_account_report, summary_text

        client = build_client()
        report = await asyncio.to_thread(build_account_report, client, DRAFT_ACCOUNT_ID, period)
    except Exception as e:  # сеть/доступ/SDK
        await m.answer(f"⚠️ Не удалось построить отчёт: {type(e).__name__}: {e}")
        return
    await m.answer(summary_text(report))


@dp.message(Command("export"))
async def export_(m: Message, command: CommandObject) -> None:
    """Глубокий отчёт .xlsx (разбивки ТЗ §9) вложением. Read-only."""
    import os
    import tempfile

    try:
        period = _period_from_arg(command.args)
    except ValueError as e:
        await m.answer(f"⚠️ {e}")
        return
    await m.answer("Готовлю .xlsx-отчёт…")
    path: str | None = None
    try:
        from ads.client import build_client
        from reports.service import build_account_report
        from reports.xlsx import write_report_xlsx

        client = build_client()
        report = await asyncio.to_thread(build_account_report, client, DRAFT_ACCOUNT_ID, period)
        fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="aimash_report_")
        os.close(fd)
        await asyncio.to_thread(write_report_xlsx, report, path)
        fname = f"aimash_{DRAFT_ACCOUNT_ID}_{period.date_from}_{period.date_to}.xlsx"
        await m.answer_document(FSInputFile(path, filename=fname))
    except Exception as e:  # сеть/доступ/SDK/openpyxl
        await m.answer(f"⚠️ Не удалось сформировать отчёт: {type(e).__name__}: {e}")
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _kb(cid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"ok:{cid}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"no:{cid}"),
            ]
        ]
    )


@dp.message(F.text)
async def on_text(m: Message) -> None:
    res = await handle_command(m.text, chat_id=m.chat.id)
    t = res.get("type")
    if t == "proposal":
        cid = res["confirmation_id"]
        await STORE.save_proposal(
            confirmation_id=cid,
            operation=res["operation"],
            customer_id=DRAFT_ACCOUNT_ID,
            params=res.get("params", {}),
            summary=res["summary"],
            chat_id=m.chat.id,
            # on_text — это входящее сообщение whitelisted-пользователя ⇒ прямая команда человека.
            # Провенанс проставляет доверенный слой (бот), не агент/модель (golden rule #3).
            user_initiated=True,
        )
        await m.answer(
            f"Изменение (черновик):\n{res['summary']}\n\nПодтвердить?", reply_markup=_kb(cid)
        )
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
    chat_id = cq.message.chat.id if cq.message else cq.from_user.id
    if not await STORE.confirm(cid, chat_id=chat_id):
        await cq.answer("Черновик не найден или устарел")
        return
    await cq.answer("Выполняю…")
    try:
        result = await execute_confirmed(STORE, cid)
        await cq.message.edit_text(f"✅ Выполнено:\n{result}")
    except Exception as e:  # доступ/резолв/SDK
        await STORE.record_failure(cid, error=str(e))
        await cq.message.edit_text(f"⚠️ Не удалось выполнить: {type(e).__name__}: {e}")


@dp.callback_query(F.data.startswith("no:"))
async def cancel(cq: CallbackQuery) -> None:
    chat_id = cq.message.chat.id if cq.message else cq.from_user.id
    await STORE.reject(cq.data[3:], chat_id=chat_id)
    await cq.message.edit_text("❌ Отменено")
    await cq.answer("Отменено")


async def main() -> None:
    setup_logging()
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN пуст — добавь в .env (токен у @BotFather).")
        return
    if not settings.whitelist:
        log.warning(
            "whitelist пуст — бот будет отвечать ВСЕМ. Добавь TELEGRAM_WHITELIST_CHAT_IDS в .env."
        )
    await init_db()
    dp.message.outer_middleware(WhitelistMiddleware())
    dp.callback_query.outer_middleware(WhitelistMiddleware())
    bot = Bot(token)
    log.info("Aimash bot запущен (polling).")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
