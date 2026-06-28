"""Переиспользуемые UX-хелперы бота (aiogram 3.x). Чистый слой представления:
НИКАКИХ обращений к Google Ads/БД. Цель — чтобы каждая правка bot/main.py была 1-строчной.

- chat_action(event): «печатает…»/«отправляет документ…» на время долгого вызова
  (фон-таск переотправляет экшен, т.к. Telegram гасит его ~5с). Best-effort, не роняет хендлер.
- fmt_rsa_diagnostics(draft, ...): «✅ 12/15 заголовков (3 слишком длинных), 4/4 описаний».
- proposal_fits / send_proposal_text: длинный proposal (>лимита Telegram) уходит .txt-вложением,
  а короткое текстовое сообщение С кнопками ✅/❌ остаётся редактируемым (_do_confirm.edit_text).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram.enums import ChatAction
from aiogram.types import FSInputFile

from core.logging import redact_text

ACTION_REFRESH_SEC = 4.0  # Telegram гасит chat action через ~5с → обновляем раньше
TG_LIMIT = 4096  # лимит длины сообщения Telegram
SAFE_LIMIT = 3500  # запас под HTML-обёртку/esc-расширение


def _bot_and_chat(event: object) -> tuple[object, int] | None:
    """(bot, chat_id) из Message-подобного или CallbackQuery-подобного события (дакт-тайпинг —
    работает и с настоящими aiogram-объектами, и с фейками в тестах)."""
    chat = getattr(event, "chat", None)
    bot = getattr(event, "bot", None)
    if chat is not None and bot is not None:  # Message-like
        return bot, chat.id
    msg = getattr(event, "message", None)  # CallbackQuery-like
    if msg is not None:
        b = getattr(msg, "bot", None) or getattr(event, "bot", None)
        c = getattr(msg, "chat", None)
        if b is not None and c is not None:
            return b, c.id
    return None


@asynccontextmanager
async def chat_action(event: object, action: str = ChatAction.TYPING) -> AsyncIterator[None]:
    """Держит chat action на время `async with`-блока. Никогда не пробрасывает исключения
    отправки в хендлер (это «украшение», а не функциональность)."""
    bc = _bot_and_chat(event)
    if bc is None:
        yield
        return
    bot, chat_id = bc

    async def _loop() -> None:
        try:
            while True:
                try:
                    await bot.send_chat_action(chat_id, action)  # type: ignore[attr-defined]
                except Exception:  # сеть/Telegram — экшен необязателен
                    pass
                await asyncio.sleep(ACTION_REFRESH_SEC)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def err_text(e: BaseException) -> str:
    """Безопасный текст исключения для показа пользователю (golden rule #5): редактирует
    секрето-подобные подстроки. str(e) от google-ads/google.auth/OpenAI может нести токен/креды,
    а отправка в Telegram минует RedactionFilter логов — поэтому редактируем здесь. Plain text
    (без HTML-escape): для сообщений без parse_mode.

    GoogleAdsException → единый богатый humanize (§15: сообщения+коды+request_id вместо сырого
    дампа протобуфа), тот же, что на мутационном пути — чтобы read- и write-ошибки выглядели
    одинаково понятно. Дакт-тайпинг (failure.errors) — без жёсткого импорта google.ads."""
    failure = getattr(e, "failure", None)
    if getattr(failure, "errors", None):
        from core.ads_errors import humanize_google_ads_error  # сам редактирует секреты

        return humanize_google_ads_error(e)
    return redact_text(f"{type(e).__name__}: {e}")


def typing_action(event: object):  # noqa: ANN201 — возвращает async-CM
    return chat_action(event, ChatAction.TYPING)


def upload_action(event: object):  # noqa: ANN201
    return chat_action(event, ChatAction.UPLOAD_DOCUMENT)


# ── Диагностика генерации RSA ────────────────────────────────────────────────────
def _plural_ru(n: int, one: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    return one if d == 1 else many


def fmt_rsa_diagnostics(
    draft: object, n_headlines: int, n_descriptions: int, *, lang: str = "ru"
) -> str:
    """Сводка результата генерации из RsaDraft (дакт-тайпинг: .headlines/.descriptions/
    .dropped_headlines/.dropped_descriptions). Показывает, СКОЛЬКО набрано и сколько отброшено
    кодом за длину (golden rule #4) — раньше эти счётчики терялись."""
    got_h = len(getattr(draft, "headlines", []) or [])
    got_d = len(getattr(draft, "descriptions", []) or [])
    drop_h = int(getattr(draft, "dropped_headlines", 0) or 0)
    drop_d = int(getattr(draft, "dropped_descriptions", 0) or 0)
    if lang == "en":
        h = f"{got_h}/{n_headlines} headlines" + (f" ({drop_h} too long)" if drop_h else "")
        d = f"{got_d}/{n_descriptions} descriptions" + (f" ({drop_d} too long)" if drop_d else "")
        return f"✅ Generated {h}, {d}."
    h_word = _plural_ru(got_h, "заголовок", "заголовков")
    d_word = _plural_ru(got_d, "описание", "описаний")
    h = f"{got_h}/{n_headlines} {h_word}" + (f" ({drop_h} слишком длинных)" if drop_h else "")
    d = f"{got_d}/{n_descriptions} {d_word}" + (f" ({drop_d} слишком длинных)" if drop_d else "")
    return f"✅ Сгенерировано {h}, {d}."


# ── Длинные proposal: .txt-вложение, кнопки на отдельном текстовом сообщении ──────
def proposal_fits(rendered: str) -> bool:
    """Влезает ли отрендеренный текст черновика в одно сообщение (с запасом)."""
    return len(rendered) <= SAFE_LIMIT


async def send_proposal_text(
    message: object,
    *,
    full_text: str,
    header_html: str,
    reply_markup: object,
    parse_mode: object,
) -> None:
    """Длинный черновик: полный diff — .txt-вложением (без кнопок), затем короткое ТЕКСТОВОЕ
    сообщение с кнопками ✅/❌ (его правит _do_confirm через edit_text — у документа edit_text нет)."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="aimash_proposal_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(full_text)
        await message.answer_document(FSInputFile(path, filename="proposal.txt"))  # type: ignore[attr-defined]
    finally:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    await message.answer(header_html, reply_markup=reply_markup, parse_mode=parse_mode)  # type: ignore[attr-defined]


async def send_proposal_keywords_xlsx(
    message: object,
    *,
    keywords: list,
    match_type: str,
    action: str,
    header_html: str,
    reply_markup: object,
    parse_mode: object,
) -> None:
    """Большой список ключей/минус-слов в черновике (ТЗ §5): полный список — .xlsx-вложением,
    затем короткое ТЕКСТОВОЕ сообщение с кнопками ✅/❌ (его правит _do_confirm.edit_text)."""
    from keywords.export import write_keyword_list_xlsx

    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="aimash_kw_proposal_")
    os.close(fd)
    try:
        write_keyword_list_xlsx(list(keywords), str(match_type), action, path)
        await message.answer_document(FSInputFile(path, filename="keywords.xlsx"))  # type: ignore[attr-defined]
    finally:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    await message.answer(header_html, reply_markup=reply_markup, parse_mode=parse_mode)  # type: ignore[attr-defined]
