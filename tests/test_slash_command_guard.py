"""N4: /команда во время активного визарда сворачивает визард → срабатывает Command-хендлер.

Раньше brief-state хендлеры (StateFilter матчит ЛЮБОЙ текст) съедали /templates/savetemplate/recent
и отвечали ошибкой формата /newsearch. Middleware обнуляет data['raw_state'] и мягко сворачивает
активный флоу; /cancel и /start исключены (свои хендлеры сами управляют состоянием).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402


class _State:
    def __init__(self):
        self.cleared = False
        self._data: dict = {}

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self.cleared = True


class _Msg:
    def __init__(self, text: str):
        self.text = text
        self.chat = type("C", (), {"id": 1})()
        self.bot = None


async def _run(text: str, raw_state):
    """Прогнать middleware; вернуть (data после, был ли вызван handler)."""
    mw = bm.SlashCommandExitsWizardMiddleware()
    state = _State()
    data = {"raw_state": raw_state, "state": state}
    called = {"n": 0}

    async def handler(event, d):
        called["n"] += 1
        return "ok"

    res = await mw(handler, _Msg(text), data)
    return data, called["n"], res, state


async def test_command_during_wizard_suspends_and_passes_through():
    data, n, res, state = await _run("/templates", "SearchWizard:awaiting_brief")
    assert n == 1 and res == "ok"  # настоящий хендлер команды всё равно вызывается
    assert data["raw_state"] is None  # визард-StateFilter больше не совпадёт → /templates сработает
    assert state.cleared is True  # лёгкий визард свёрнут


async def test_command_with_botname_and_args_normalized():
    data, n, _, state = await _run("/savetemplate@Aimash_bot имя", "RsaWizard:awaiting_brief")
    assert data["raw_state"] is None and state.cleared is True


async def test_cancel_is_exempt_not_suspended():
    data, n, _, state = await _run("/cancel", "ClientInfoWizard:awaiting_text")
    assert n == 1  # хендлер вызывается
    assert (
        data["raw_state"] == "ClientInfoWizard:awaiting_text"
    )  # НЕ сворачиваем (свой /cancel-хендлер)
    assert state.cleared is False


async def test_start_is_exempt():
    data, _, _, state = await _run("/start", "SearchWizard:awaiting_brief")
    assert data["raw_state"] == "SearchWizard:awaiting_brief" and state.cleared is False


async def test_plain_text_during_wizard_untouched():
    # обычный текст (не команда) middleware не трогает — его ловит brief-хендлер визарда
    data, n, _, state = await _run("туры на байкал", "SearchWizard:awaiting_brief")
    assert n == 1 and data["raw_state"] == "SearchWizard:awaiting_brief" and state.cleared is False


async def test_command_without_active_state_untouched():
    # нет активного визарда → middleware ничего не сворачивает
    data, n, _, state = await _run("/templates", None)
    assert n == 1 and data["raw_state"] is None and state.cleared is False


# ── B1 (живой тест 2026-07-07): сворачивание больше НЕ молчаливое ────────────────
async def test_command_with_cc_draft_sends_saved_note_after_handler(monkeypatch):
    """Команда посреди визарда §19: черновик остаётся active, а ПОСЛЕ ответа команды пользователь
    получает подсказку «черновик сохранён (шаг N/7)» — раньше сворачивание было молчаливым."""

    class _Draft:
        status = "active"
        current_step = 3
        session_id = "s1"
        wizard_state: dict = {}

    class _Store:
        async def get(self, sid, expected_chat_id=None):
            return _Draft()

    monkeypatch.setattr(bm, "CDRAFTS", _Store())
    mw = bm.SlashCommandExitsWizardMiddleware()
    state = _State()
    state._data = {"cc_session": "s1"}
    order: list[str] = []
    msg = _Msg("/report")

    async def answer(text, **kw):
        order.append(f"note:{text}")

    msg.answer = answer
    data = {"raw_state": "CreateCampaignWizard:keywords", "state": state}

    async def handler(event, d):
        order.append("handler")
        return "ok"

    res = await mw(handler, msg, data)
    assert res == "ok" and state.cleared is True and data["raw_state"] is None
    notes = [x for x in order if x.startswith("note:")]
    assert notes and "3/7" in notes[0]  # шаг черновика в подсказке
    assert order[0] == "handler"  # подсказка ПОСЛЕ ответа команды, не вместо него


async def test_note_failure_does_not_break_command(monkeypatch):
    """Сбой отправки подсказки не роняет уже выполненную команду (handler-результат доходит)."""

    class _Draft:
        status = "active"
        current_step = 2
        session_id = "s1"
        wizard_state: dict = {}

    class _Store:
        async def get(self, sid, expected_chat_id=None):
            return _Draft()

    monkeypatch.setattr(bm, "CDRAFTS", _Store())
    mw = bm.SlashCommandExitsWizardMiddleware()
    state = _State()
    state._data = {"cc_session": "s1"}
    msg = _Msg("/report")

    async def answer(text, **kw):
        raise RuntimeError("chat is dead")

    msg.answer = answer
    data = {"raw_state": "CreateCampaignWizard:keywords", "state": state}

    async def handler(event, d):
        return "ok"

    assert await mw(handler, msg, data) == "ok"
