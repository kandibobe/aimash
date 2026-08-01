from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

import bot.main as bm
from bot.handlers import HANDLER_MODULES
from bot.handlers import react_gateway


def test_only_lightweight_handlers_are_registered() -> None:
    assert HANDLER_MODULES == ("commands", "react_gateway")
    callbacks = [handler.callback.__name__ for handler in bm.dp.message.handlers]
    assert callbacks == ["start", "help_", "newcampaign_react", "on_text"]
    assert callbacks[-1] == "on_text"


class _State:
    def __init__(self) -> None:
        self.cleared = False

    async def clear(self) -> None:
        self.cleared = True


class _Message:
    def __init__(self, text: str = "покажи статистику") -> None:
        self.text = text
        self.chat = type("Chat", (), {"id": 42})()
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return self


@pytest.mark.asyncio
async def test_free_text_goes_directly_to_react(monkeypatch) -> None:
    message = _Message()
    state = _State()
    calls: list[tuple[str, int, dict]] = []

    async def within_budget(_message) -> bool:
        return False

    async def fake_handle(text: str, *, chat_id: int, context: dict):
        calls.append((text, chat_id, context))
        return {"type": "text", "text": "готово"}

    @asynccontextmanager
    async def no_typing(_message):
        yield

    monkeypatch.setattr(bm, "_llm_budget_or_reply", within_budget)
    monkeypatch.setattr(bm, "handle_command", fake_handle)
    monkeypatch.setattr(bm.ux, "typing_action", no_typing)

    await react_gateway.on_text(message, state)

    assert state.cleared is True
    assert calls == [
        ("покажи статистику", 42, {"last_campaign": "", "last_account": "", "history": []})
    ]
    assert message.answers[-1][0] == "готово"


@pytest.mark.asyncio
async def test_start_and_help_handlers_remain_available() -> None:
    callbacks = {handler.callback.__name__ for handler in bm.dp.message.handlers}
    assert {"start", "help_"} <= callbacks
