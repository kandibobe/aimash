"""Commands retained by the lightweight Telegram surface."""

from __future__ import annotations

import bot.main as bm


@bm.dp.message(bm.CommandStart())
async def start(message: bm.Message) -> None:
    """Send the existing welcome banner, degrading to text when it is unavailable."""
    global_file_id = bm._welcome_file_id
    start_text = bm.i18n.t("start")
    if global_file_id or bm.WELCOME_IMG.exists():
        photo = global_file_id or bm.FSInputFile(bm.WELCOME_IMG)
        try:
            sent = await message.answer_photo(
                photo, caption=start_text, parse_mode=bm.ParseMode.HTML
            )
            if sent.photo:
                bm._welcome_file_id = sent.photo[-1].file_id
            return
        except Exception as exc:  # Telegram delivery is best-effort; keep /start responsive.
            bm.log.warning("welcome banner delivery failed (%s); sending text", type(exc).__name__)
            bm._welcome_file_id = None
    await message.answer(start_text, parse_mode=bm.ParseMode.HTML)


@bm.dp.message(bm.Command("help"))
async def help_(message: bm.Message) -> None:
    await bm._send_help(message)
