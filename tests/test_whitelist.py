"""Тесты WhitelistMiddleware (ТЗ §6/§12). Закрывает дыру аудита: whitelist был fail-OPEN при
пустом наборе. Теперь fail-CLOSED — пустой whitelist блокирует ВСЕХ (как ads.client.ensure_allowed).

Фейки aiogram — локальные (дакт-тайпинг: .chat.id для Message, .message.chat.id для callback).
settings.whitelist подменяем через нижележащее поле telegram_whitelist_chat_ids.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402


@contextmanager
def _whitelist(value: str):
    orig = bm.settings.telegram_whitelist_chat_ids
    bm.settings.telegram_whitelist_chat_ids = value
    try:
        yield
    finally:
        bm.settings.telegram_whitelist_chat_ids = orig


def _msg(chat_id: int, ctype: str | None = None):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id, type=ctype))


def _callback(chat_id: int, ctype: str | None = None):
    return SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=chat_id, type=ctype)))


def _counter():
    state = {"n": 0}

    async def handler(event, data):
        state["n"] += 1
        return "ok"

    return state, handler


def _msg_answerable(chat_id: int, ctype: str = "private", lang: str | None = None):
    """Message-фейк с .answer (записывает отправленное) + from_user.language_code — для проверки
    вежливого отказа не-whitelisted."""
    sent: list[str] = []

    async def answer(text, **kw):
        sent.append(text)

    ev = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=ctype),
        from_user=SimpleNamespace(language_code=lang),
        answer=answer,
    )
    return ev, sent


async def test_blocks_non_whitelisted_chat():
    mw = bm.WhitelistMiddleware()
    state, handler = _counter()
    with _whitelist("7"):
        assert await mw(handler, _msg(999), {}) is None  # чужой chat_id — дроп
        assert state["n"] == 0


async def test_allows_whitelisted_chat():
    mw = bm.WhitelistMiddleware()
    state, handler = _counter()
    with _whitelist("7,8"):
        await mw(handler, _msg(7), {})
        await mw(handler, _callback(8), {})  # callback тоже проходит whitelist
        assert state["n"] == 2


async def test_fail_closed_when_whitelist_empty():
    mw = bm.WhitelistMiddleware()
    state, handler = _counter()
    with _whitelist(""):  # пустой whitelist => блок ВСЕХ (а не fail-open)
        await mw(handler, _msg(7), {})
        await mw(handler, _callback(7), {})
        assert state["n"] == 0


async def test_blocks_event_without_chat():
    mw = bm.WhitelistMiddleware()
    state, handler = _counter()
    with _whitelist("7"):
        await mw(handler, SimpleNamespace(), {})  # ни .chat, ни .message → uid=None → блок
        assert state["n"] == 0


async def test_blocks_whitelisted_group_chat():
    """Whitelist по chat_id, а в группе chat_id один на всех → блокируем не-private чаты (даже с
    whitelisted id): иначе любой участник нажал бы ✅ и двинул деньги (golden rule #10)."""
    mw = bm.WhitelistMiddleware()
    state, handler = _counter()
    with _whitelist("7"):
        await mw(handler, _msg(7, "group"), {})  # whitelisted id, но групповой чат → блок
        await mw(handler, _callback(7, "supergroup"), {})
        assert state["n"] == 0
        await mw(handler, _msg(7, "private"), {})  # тот же id в private → проходит
        assert state["n"] == 1


# ── Вежливый отказ не-whitelisted (§UX): один раз на chat_id, private-only, замок цел ──
async def test_unlisted_private_gets_one_refusal_with_id():
    bm._WL_REFUSED.clear()
    mw = bm.WhitelistMiddleware()
    state, handler = _counter()
    ev, sent = _msg_answerable(999)
    with _whitelist("7"):
        await mw(handler, ev, {})  # 1-й контакт → отказ
        await mw(handler, ev, {})  # повтор → тишина (уже отказали)
    assert state["n"] == 0  # хендлер не вызван — замок не ослаблен
    assert len(sent) == 1  # ровно один отказ
    assert "999" in sent[0] and "⛔" in sent[0]  # с его chat_id


async def test_unlisted_group_stays_silent():
    bm._WL_REFUSED.clear()
    mw = bm.WhitelistMiddleware()
    state, handler = _counter()
    ev, sent = _msg_answerable(999, ctype="group")
    with _whitelist("7"):
        await mw(handler, ev, {})
    assert state["n"] == 0
    assert sent == []  # не-private → без отказа (whitelist по chat_id бессмыслен в группе)


async def test_unlisted_callback_stays_silent():
    bm._WL_REFUSED.clear()
    mw = bm.WhitelistMiddleware()
    state, handler = _counter()
    with _whitelist("7"):
        await mw(handler, _callback(999, "private"), {})  # callback (нет .chat/.answer) → тишина
    assert state["n"] == 0


async def test_unlisted_refusal_honors_telegram_lang_en():
    bm._WL_REFUSED.clear()
    mw = bm.WhitelistMiddleware()
    state, handler = _counter()
    ev, sent = _msg_answerable(999, lang="en-US")
    with _whitelist("7"):
        await mw(handler, ev, {})
    assert "not granted" in sent[0].lower()  # EN по language_code Telegram-клиента


async def test_unlisted_warning_logged_once(caplog):
    """1F10: WARNING «не в whitelist» пишется ОДИН раз на chat_id; повторы — debug (не шум)."""
    import logging

    mw = bm.WhitelistMiddleware()
    _state, handler = _counter()
    uid = 424242
    bm._WL_LOGGED.discard(uid)  # чистый старт для этого uid (set живёт на процесс)
    with _whitelist("7"), caplog.at_level(logging.WARNING, logger="aimash"):
        await mw(handler, _msg(uid), {})
        await mw(handler, _msg(uid), {})
        await mw(handler, _msg(uid), {})
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and str(uid) in r.getMessage()
    ]
    assert len(warnings) == 1, f"ожидался ровно 1 WARNING на chat_id, получено {len(warnings)}"
