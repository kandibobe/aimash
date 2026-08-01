"""D9 (удобство 2026-07): нераспознанная слэш-команда → подсказка, НЕ отправка в LLM.

Агент трактовал бы «/фыва» как задачу и мог свободно нафантазировать (в т.ч. посторонний proposal).
Agent-first on_text зарегистрирован первым, но фильтром исключает slash-команды. Поэтому известные
команды доходят до своих handlers, а неизвестные — до on_unknown_command.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402


class _Msg:
    def __init__(self, text):
        self.text = text
        self.chat = SimpleNamespace(id=1)
        self.sent: list = []

    async def answer(self, text, **kw):
        self.sent.append(text)


def _handler_names() -> list[str]:
    return [h.callback.__name__ for h in bm.dp.message.handlers]


def test_react_catchall_does_not_capture_slash_commands():
    names = _handler_names()
    assert names.index("on_text") < names.index("on_unknown_command")
    react = next(h for h in bm.dp.message.handlers if h.callback.__name__ == "on_text")
    assert react.filters[0].callback(SimpleNamespace(text="/unknown")) is False


async def test_unknown_command_shows_hint():
    from bot.handlers.fallback import on_unknown_command

    m = _Msg("/фыва аргумент")
    await on_unknown_command(m)
    assert m.sent, "нет ответа на неизвестную команду"
    out = m.sent[0]
    assert "/фыва" in out  # эхо самой команды
    assert "/help" in out  # направляем в справку


async def test_hint_does_not_call_llm(monkeypatch):
    """Класс-гард: обработчик неизвестной команды НЕ трогает handle_command (LLM-путь)."""
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(bm, "handle_command", boom)
    from bot.handlers.fallback import on_unknown_command

    await on_unknown_command(_Msg("/unknownxyz"))
    assert called["n"] == 0  # в LLM не ушли
