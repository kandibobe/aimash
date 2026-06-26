"""Тесты анти-спам middleware (ТЗ §12). Управляемые «часы» → детерминированно, без sleep."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.throttle import ThrottleMiddleware  # noqa: E402


class FakeMessage:
    def __init__(self, chat_id: int = 7):
        self.chat = SimpleNamespace(id=chat_id)
        self.replies: list = []

    async def answer(self, text: str = "", **kw):
        self.replies.append(text)


def _counter_handler(state):
    async def _h(event, data):
        state["n"] += 1
        return "ok"

    return _h


async def test_burst_allowed_then_blocked_then_refilled():
    clock = {"t": 1000.0}
    mw = ThrottleMiddleware(rate=0.5, burst=3, clock=lambda: clock["t"])
    state = {"n": 0}
    handler = _counter_handler(state)
    msg = FakeMessage()

    for _ in range(3):  # всплеск burst=3 — пропускаем
        await mw(handler, msg, {})
    assert state["n"] == 3

    await mw(handler, msg, {})  # 4-е сразу — заблокировано (токенов нет)
    assert state["n"] == 3

    clock["t"] += 2.1  # пополнение: 1/rate = 2с → ≥1 токен
    await mw(handler, msg, {})
    assert state["n"] == 4


async def test_blocked_sends_one_warning_within_cooldown():
    clock = {"t": 0.0}
    mw = ThrottleMiddleware(rate=0.01, burst=1, warn_cooldown=5.0, clock=lambda: clock["t"])
    state = {"n": 0}
    handler = _counter_handler(state)
    msg = FakeMessage()

    await mw(handler, msg, {})  # съели единственный токен
    await mw(handler, msg, {})  # блок → предупреждение
    await mw(handler, msg, {})  # блок снова, но в пределах cooldown → без повторного предупреждения
    assert state["n"] == 1
    assert len(msg.replies) == 1  # ровно одно «слишком часто»


async def test_non_message_event_passes_through():
    mw = ThrottleMiddleware(burst=1, clock=lambda: 0.0)
    state = {"n": 0}
    handler = _counter_handler(state)
    event = SimpleNamespace()  # ни .chat, ни .message → _chat_id=None → не троттлим

    for _ in range(5):
        await mw(handler, event, {})
    assert state["n"] == 5


async def test_separate_chats_have_independent_buckets():
    clock = {"t": 0.0}
    mw = ThrottleMiddleware(rate=0.1, burst=2, clock=lambda: clock["t"])
    state = {"n": 0}
    handler = _counter_handler(state)
    a, b = FakeMessage(chat_id=1), FakeMessage(chat_id=2)

    for _ in range(2):
        await mw(handler, a, {})
    await mw(handler, a, {})  # a исчерпал burst → блок
    for _ in range(2):
        await mw(handler, b, {})  # b со своим бакетом — пропускается
    assert state["n"] == 4  # 2 (a) + 2 (b); 3-е у a отброшено
