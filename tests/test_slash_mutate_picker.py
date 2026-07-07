"""D4 (удобство 2026-07): /pause и /resume без аргумента → пикер кампаний (по статусу).

Пикер показывает ENABLED для паузы / PAUSED для возобновления; клик минтит proposal за
confirm-гейтом (тот же хвост, что ввод имени). Ввод имени командой остаётся фолбэком.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot.callbacks import SlashMutCB  # noqa: E402
from bot.keyboards import slash_mutate_campaigns_kb  # noqa: E402


class _Msg:
    def __init__(self, chat_id):
        self.chat = SimpleNamespace(id=chat_id)
        self.sent: list = []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))


class _Cq:
    def __init__(self, chat_id):
        self.message = _Msg(chat_id)
        self.from_user = SimpleNamespace(id=chat_id, username="op")
        self.alerts = 0

    async def answer(self, *a, **kw):
        if kw.get("show_alert"):
            self.alerts += 1


_CAMPS = [
    {"name": "Активная 1", "id": "1", "status": "ENABLED"},
    {"name": "Пауза 1", "id": "2", "status": "PAUSED"},
    {"name": "Активная 2", "id": "3", "status": "ENABLED"},
]


async def test_pause_no_arg_shows_only_enabled(monkeypatch):
    async def fake_load(chat_id):
        return list(_CAMPS)

    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_load)
    chat_id = 6001
    m = _Msg(chat_id)
    await bm._slash_mutate(m, SimpleNamespace(args=""), "pause_campaign")
    cached = bm._SLASH_MUT_CACHE[chat_id]
    assert [c["name"] for c in cached] == ["Активная 1", "Активная 2"]  # только ENABLED
    markup = m.sent[-1][1]["reply_markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert not any("Пауза" in t for t in labels)  # приостановленные не предлагаем к паузе


async def test_resume_no_arg_shows_only_paused(monkeypatch):
    async def fake_load(chat_id):
        return list(_CAMPS)

    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_load)
    chat_id = 6002
    m = _Msg(chat_id)
    await bm._slash_mutate(m, SimpleNamespace(args=""), "resume_campaign")
    assert [c["name"] for c in bm._SLASH_MUT_CACHE[chat_id]] == ["Пауза 1"]


async def test_no_arg_text_hint_when_no_matching(monkeypatch):
    """Нет кампаний нужного статуса (или сбой чтения) → прежняя текст-подсказка, без пикера."""

    async def fake_empty(chat_id):
        return []

    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_empty)
    chat_id = 6003
    m = _Msg(chat_id)
    await bm._slash_mutate(m, SimpleNamespace(args=""), "pause_campaign")
    assert chat_id not in bm._SLASH_MUT_CACHE
    assert m.sent  # подсказка показана


async def test_pick_button_mints_proposal(monkeypatch):
    from bot.handlers.campaigns_menu import on_slash_mutate_pick

    presented: dict = {}

    async def fake_present(message, chat_id, operation, name):
        presented.update(chat_id=chat_id, operation=operation, name=name)

    monkeypatch.setattr(bm, "_slash_mutate_present", fake_present)
    chat_id = 6004
    bm._SLASH_MUT_CACHE[chat_id] = list(_CAMPS)
    cq = _Cq(chat_id)
    await on_slash_mutate_pick(cq, SlashMutCB(op="pause_campaign", idx=2))
    assert presented == {"chat_id": chat_id, "operation": "pause_campaign", "name": "Активная 2"}


async def test_pick_button_stale(monkeypatch):
    from bot.handlers.campaigns_menu import on_slash_mutate_pick

    called = {"n": 0}

    async def fake_present(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(bm, "_slash_mutate_present", fake_present)
    chat_id = 6005
    bm._SLASH_MUT_CACHE.pop(chat_id, None)
    cq = _Cq(chat_id)
    await on_slash_mutate_pick(cq, SlashMutCB(op="pause_campaign", idx=0))
    assert called["n"] == 0 and cq.alerts == 1  # stale-alert, ничего не минтили


def test_kb_emits_op_and_global_idx():
    markup = slash_mutate_campaigns_kb(_CAMPS, "resume_campaign")
    picks = [
        SlashMutCB.unpack(b.callback_data)
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("smut")
    ]
    assert all(p.op == "resume_campaign" for p in picks)
    assert [p.idx for p in picks] == [0, 1, 2]  # глобальные индексы кэша
