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

from adcopy.validate import MIN_KEYWORD_COVERAGE, any_cta, count_flagged
from core import i18n
from core.logging import redact_text

ACTION_REFRESH_SEC = 4.0  # Telegram гасит chat action через ~5с → обновляем раньше
TG_LIMIT = 4096  # лимит длины сообщения Telegram
SAFE_LIMIT = 3500  # запас под HTML-обёртку/esc-расширение

# ── single-flight: «этот долгий колбэк уже выполняется» ────────────────────────────
# Колбэки НЕ троттлятся (bot/throttle.py лимитирует только сообщения — иначе ломался бы confirm-флоу),
# а конвейер генерации ключей идёт 20–40 с. Двойной тап по кнопке запускал ВТОРОЙ конвейер: две
# Google-таблицы, и ссылку на первую бот потом отвергал как «чужую» → ручная выверка менеджера
# терялась. Ключ — (тип операции, id сессии): параллельные сессии друг друга не блокируют.
# In-memory (бот однопроцессный, как и throttle/кэши); рестарт всё сбрасывает — это ок.
_INFLIGHT: set[tuple[str, object]] = set()


def acquire_flight(kind: str, key: object) -> bool:
    """True — захватили (можно выполнять); False — эта операция уже идёт (второй тап). Освобождать
    ОБЯЗАТЕЛЬНО в finally (release_flight), иначе кнопка залипнет до рестарта."""
    k = (kind, key)
    if k in _INFLIGHT:
        return False
    _INFLIGHT.add(k)
    return True


def release_flight(kind: str, key: object) -> None:
    _INFLIGHT.discard((kind, key))


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


def _err_hint(e: BaseException) -> str:
    """Короткая подсказка «что делать» для частых ВРЕМЕННЫХ ошибок (сеть/LLM); '' если подсказки
    нет. Срабатывает только на узнаваемых классах — валидационные ошибки (ValueError и т.п.)
    остаются без суффикса (их текст уже самодостаточен)."""
    text = str(e).lower()
    mod = type(e).__module__ or ""
    if isinstance(e, TimeoutError) or "timed out" in text or "timeout" in text:
        return i18n.t("hint_timeout")
    if isinstance(e, ConnectionError):
        return i18n.t("hint_network")
    if mod.startswith("openai") or "rate limit" in text or "rate_limit" in text:
        return i18n.t("hint_ratelimit")
    return ""


def err_text(e: BaseException) -> str:
    """Безопасный текст исключения для показа пользователю (golden rule #5): редактирует
    секрето-подобные подстроки. str(e) от google-ads/google.auth/OpenAI может нести токен/креды,
    а отправка в Telegram минует RedactionFilter логов — поэтому редактируем здесь. Plain text
    (без HTML-escape): для сообщений без parse_mode.

    GoogleAdsException → единый богатый humanize (§15: сообщения+коды+request_id вместо сырого
    дампа протобуфа), тот же, что на мутационном пути — чтобы read- и write-ошибки выглядели
    одинаково понятно. Дакт-тайпинг (failure.errors) — без жёсткого импорта google.ads.

    Для частых временных ошибок (сеть/LLM) добавляем короткую подсказку к действию (_err_hint)."""
    failure = getattr(e, "failure", None)
    if getattr(failure, "errors", None):
        from core.ads_errors import humanize_google_ads_error  # сам редактирует секреты

        return humanize_google_ads_error(e)
    return redact_text(f"{type(e).__name__}: {e}") + _err_hint(e)


# Локализованные фразы для частых типов ошибок валидации Pydantic (§UX: без сырого дампа/англ.).
# type ошибки Pydantic → (i18n-ключ фразы, поле в ctx с лимитом). None-поле = фраза без числа.
_VAL_RULE_KEYS: dict[str, tuple[str, str | None]] = {
    "less_than_equal": ("val_le", "le"),
    "greater_than_equal": ("val_ge", "ge"),
    "less_than": ("val_lt", "lt"),
    "greater_than": ("val_gt", "gt"),
    "string_too_long": ("val_too_long", "max_length"),
    "string_too_short": ("val_too_short", "min_length"),
    "missing": ("val_missing", None),
}


def _val_rule(err: dict) -> str:
    """Одна ошибка Pydantic → короткая локализованная фраза правила. Незнакомый тип →
    редактированный msg самого Pydantic (fallback, англ., но без служебного хвоста [type=…])."""
    spec = _VAL_RULE_KEYS.get(err.get("type", ""))
    if spec is None:
        return redact_text(str(err.get("msg", "")))
    key, ctx_field = spec
    if ctx_field is None:
        return i18n.t(key)
    limit = (err.get("ctx") or {}).get(ctx_field, "?")
    return i18n.t(key, n=limit)


def humanize_validation(e: BaseException) -> str:
    """Человекочитаемая ошибка валидации Pydantic вместо сырого многострочного дампа
    («N validation errors for X … [type=…, input_value=…]»), который непонятен пользователю и
    всегда по-английски. Собираем «поле: правило» по e.errors() (первые 6). Не-Pydantic
    исключение → обычный безопасный err_text. Возвращает ТЕЛО — call-site оборачивает его в
    i18n-шаблон err_validate."""
    errs = getattr(e, "errors", None)
    if not callable(errs):
        return err_text(e)
    try:
        items = list(e.errors())  # type: ignore[call-arg]
    except Exception:  # noqa: BLE001 — не Pydantic-подобное → безопасный путь
        return err_text(e)
    if not items:
        return err_text(e)
    lines = []
    for it in items[:6]:
        loc = ".".join(str(p) for p in it.get("loc", ()) if p != "__root__") or "?"
        lines.append(f"• {loc}: {_val_rule(it)}")
    more = len(items) - 6
    if more > 0:
        lines.append(i18n.t("val_more", n=more))
    return "\n".join(lines)


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
    draft: object, n_headlines: int, n_descriptions: int, *, lang: str | None = None
) -> str:
    """Сводка результата генерации из RsaDraft (дакт-тайпинг: .headlines/.descriptions/
    .dropped_headlines/.dropped_descriptions). Показывает, СКОЛЬКО набрано и сколько отброшено
    кодом за длину (golden rule #4) — раньше эти счётчики терялись. lang=None → язык запроса
    (contextvar i18n.current_lang), чтобы EN-пользователь видел EN без проброса lang вызывающим."""
    lang = i18n.current_lang() if lang is None else lang
    got_h = len(getattr(draft, "headlines", []) or [])
    got_d = len(getattr(draft, "descriptions", []) or [])
    drop_h = int(getattr(draft, "dropped_headlines", 0) or 0)
    drop_d = int(getattr(draft, "dropped_descriptions", 0) or 0)
    # §10: редакторская политика (КАПС/пунктуация/повторы) — считает КОД, не промпт. Advisory:
    # подсвечиваем рискованные тексты ДО запуска, не блокируя (редактура Google нечёткая).
    all_texts = [
        *(getattr(draft, "headlines", []) or []),
        *(getattr(draft, "descriptions", []) or []),
    ]
    flagged = count_flagged(all_texts)
    # §10 best practice: хотя бы ОДИН призыв к действию (CTA) в наборе. Advisory (эвристика по
    # лексикону), не блокирует — просто подсказка «добавьте CTA», если его нигде не нашли.
    no_cta = bool(all_texts) and not any_cta(all_texts)
    # §10: покрытие ключей в заголовках. keyword_coverage=1.0 ⇒ ключей не было ИЛИ все покрыты —
    # молчим. Ф3: ниже порога генератор уже ПЫТАЛСЯ починить (догенерация заголовков с ключами);
    # если после ремонта всё ещё низко — честно предупреждаем. Порог — MIN_KEYWORD_COVERAGE, один
    # и тот же у генерации, аудита и этой диагностики (иначе бот советует не то, что делает).
    cov = getattr(draft, "keyword_coverage", 1.0)
    low_cov = isinstance(cov, (int, float)) and cov < MIN_KEYWORD_COVERAGE
    if lang == "en":
        h = f"{got_h}/{n_headlines} headlines" + (f" ({drop_h} too long)" if drop_h else "")
        d = f"{got_d}/{n_descriptions} descriptions" + (f" ({drop_d} too long)" if drop_d else "")
        out = f"✅ Generated {h}, {d}."
        if flagged:
            out += (
                f"\n⚠️ {flagged} text(s) may fail ad review (caps / punctuation / repeats)"
                " — check before launch."
            )
        if no_cta:
            out += "\n💡 No clear call-to-action found — consider adding one (Buy, Order, Call…)."
        if low_cov:
            out += (
                f"\n💡 Low keyword coverage in headlines ({int(cov * 100)}%)"
                " — keywords are best placed in headlines."
            )
        return out
    h_word = _plural_ru(got_h, "заголовок", "заголовков")
    d_word = _plural_ru(got_d, "описание", "описаний")
    h = f"{got_h}/{n_headlines} {h_word}" + (f" ({drop_h} слишком длинных)" if drop_h else "")
    d = f"{got_d}/{n_descriptions} {d_word}" + (f" ({drop_d} слишком длинных)" if drop_d else "")
    out = f"✅ Сгенерировано {h}, {d}."
    if flagged:
        out += (
            f"\n⚠️ {flagged} текст(ов) под риском модерации (КАПС / пунктуация / повторы)"
            " — проверьте перед запуском."
        )
    if no_cta:
        out += "\n💡 Призыв к действию не найден — добавьте (Купить, Заказать, Звоните…)."
    if low_cov:
        out += (
            f"\n💡 Низкое покрытие ключей в заголовках ({int(cov * 100)}%)"
            " — ключи лучше включать в заголовки."
        )
    return out


# ── Длинные proposal: .txt-вложение, кнопки на отдельном текстовом сообщении ──────
def proposal_fits(rendered: str) -> bool:
    """Влезает ли отрендеренный текст черновика в одно сообщение (с запасом)."""
    return len(rendered) <= SAFE_LIMIT


def split_by_lines(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Разбить длинный текст на куски ≤limit по границам СТРОК (не рвём строку/HTML-тег посередине).
    Для /mcc и подобных построчных сводок: каждый кусок — самостоятельное сообщение. Одиночная
    строка длиннее limit (редко) отдаётся как есть (Telegram сам отклонит — но не рвём разметку)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit and buf:
            chunks.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks


# 2.8: пауза между чанками многосерийного сообщения (Telegram flood-limits ~1 msg/s на чат) и
# потолок повторов на TelegramRetryAfter (не зависаем навсегда, если лимит держится).
CHUNK_SEND_PAUSE_S = 0.35
_RETRY_AFTER_MAX_ATTEMPTS = 3


async def answer_with_flood_retry(message: object, text: str, **kw) -> None:
    """2.8: message.answer с обработкой flood-control (429/TelegramRetryAfter): подождать
    retry_after и повторить (до _RETRY_AFTER_MAX_ATTEMPTS попыток). Раньше крупный /mcc-дайджест
    по 7 аккаунтам ронял хвост чанков сырой ошибкой. Прочие исключения — наружу (как раньше)."""
    import asyncio

    from aiogram.exceptions import TelegramRetryAfter

    for attempt in range(_RETRY_AFTER_MAX_ATTEMPTS):
        try:
            await message.answer(text, **kw)  # type: ignore[attr-defined]
            return
        except TelegramRetryAfter as e:
            if attempt == _RETRY_AFTER_MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)


async def send_html_chunks(message: object, text: str, limit: int = SAFE_LIMIT) -> None:
    """Отправить длинный HTML-текст несколькими сообщениями (parse_mode=HTML), деля по строкам.
    2.8: между чанками — пауза, каждый send — с retry на flood-control (429)."""
    import asyncio

    from aiogram.enums import ParseMode

    chunks = split_by_lines(text, limit)
    for i, chunk in enumerate(chunks):
        if i:  # пауза только МЕЖДУ чанками (одиночное сообщение — без задержки, как раньше)
            await asyncio.sleep(CHUNK_SEND_PAUSE_S)
        await answer_with_flood_retry(message, chunk, parse_mode=ParseMode.HTML)


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


async def send_text_document(message: object, *, text: str, filename: str) -> None:
    """Отправить произвольный ТЕКСТ .txt-вложением в ИНТЕРАКТИВНОМ хендлере (есть message).
    Используется экспортом журнала ошибок (/diag «📎 Экспорт») и т.п. Временный файл — в finally."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="aimash_export_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        await message.answer_document(FSInputFile(path, filename=filename))  # type: ignore[attr-defined]
    finally:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


async def send_bot_document(bot: object, chat_id: int, *, text: str, filename: str) -> None:
    """Отправить ТЕКСТ .txt-вложением из SCHEDULER-контекста (нет message, только bot). Аналог
    send_text_document для плановых джоб (еженедельный дайджест). Временный файл — в finally.
    Вызывающий сам ловит исключения per-recipient (один недоступный чат не валит рассылку)."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="aimash_digest_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        await bot.send_document(chat_id, FSInputFile(path, filename=filename))  # type: ignore[attr-defined]
    finally:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


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
