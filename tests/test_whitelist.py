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
