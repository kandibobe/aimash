"""A12: /diag «📎 Экспорт» отдаёт .txt-дамп с ПОЛНЫМИ трейсбеками и чужими customer_id — теперь
только админу (как соседняя ветка detail). Раньше export был открыт всем whitelisted и обходил
админ-гейт кнопки detail.

Драйвим on_diag_cb фейками aiogram; _is_admin подменяем. Класс-гард: не-админ закрыт для
чувствительных действий (export/detail), разрешён для {refresh/today/all}.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.handlers.commands as cmds  # noqa: E402
import bot.main as bm  # noqa: E402
from bot.callbacks import DiagCB  # noqa: E402
from bot.keyboards import diag_kb  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _admin(val: bool):
    async def _f(chat_id):
        return val

    return _f


class _FakeMessage:
    def __init__(self, chat_id=100):
        self.chat = type("C", (), {"id": chat_id})()
        self.bot = type("B", (), {})()
        self.answers: list = []
        self.docs: list = []

    async def answer(self, text="", **kw):
        self.answers.append(text)

    async def answer_document(self, *a, **k):
        self.docs.append(a)


class _FakeCallbackQuery:
    def __init__(self, chat_id=100, uid=100):
        self.message = _FakeMessage(chat_id)
        self.from_user = type("U", (), {"id": uid, "username": "op"})()
        self.answers: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


async def test_export_blocked_for_non_admin():
    cq = _FakeCallbackQuery()
    with patched(cmds, "_is_admin", _admin(False)):
        await cmds.on_diag_cb(cq, DiagCB(action="export"))
    assert cq.answers and cq.answers[-1][1] is True  # show_alert admin_only
    assert not cq.message.docs  # .txt-дамп НЕ отправлен


async def test_export_allowed_for_admin(monkeypatch):
    cq = _FakeCallbackQuery()

    async def _rows(**k):
        return []

    with (
        patched(cmds, "_is_admin", _admin(True)),
        patched(bm, "_load_error_events", _rows),
    ):
        await cmds.on_diag_cb(cq, DiagCB(action="export"))
    assert cq.message.docs  # админу дамп отправлен


@pytest.mark.parametrize("action", ["export", "detail"])
async def test_sensitive_actions_blocked_for_non_admin(action):
    cq = _FakeCallbackQuery()
    with patched(cmds, "_is_admin", _admin(False)):
        await cmds.on_diag_cb(cq, DiagCB(action=action, rid="x"))
    assert cq.answers and cq.answers[-1][1] is True  # admin_only alert


def test_diag_kb_hides_export_from_non_admin():
    kb_admin = diag_kb([], today=False, is_admin=True)
    kb_user = diag_kb([], today=False, is_admin=False)

    def _actions(markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    assert any("export" in (cd or "") for cd in _actions(kb_admin))  # админ видит экспорт
    assert not any("export" in (cd or "") for cd in _actions(kb_user))  # не-админ — нет
