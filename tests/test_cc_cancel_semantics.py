"""W5 (живой тест 2026-07-06): «✖ Отмена»/'/cancel' в визарде §19 с накопленной работой —
диалог «сохранить/удалить/вернуться» вместо безвозвратного abandon. Пустой черновик — прежний
быстрый abandon без диалога. Черновик после «сохранить» резюмится через /newcampaign.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from db.session import init_db  # noqa: E402


class FakeMessage:
    def __init__(self, text="", chat_id=100):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text="", **kw):
        self.edits.append((text, kw))
        return self

    async def delete(self):
        pass


class FakeCallbackQuery:
    def __init__(self, message, uid=100):
        self.message = message
        self.from_user = SimpleNamespace(id=uid)
        self.answers: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self, data=None):
        self._d, self._state = dict(data or {}), None

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)

    async def set_state(self, s=None):
        self._state = s

    async def get_state(self):
        return self._state

    async def clear(self):
        self._d, self._state = {}, None


async def _content_draft(chat: int) -> str:
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.patch(
        sid,
        lambda st: (
            st.__setitem__("settings", {"campaign_name": "Уганда · авто · Search"}),
            st["keywords"].update({"list": ["kw1", "kw2"], "verified": True}),
        ),
        expected_chat_id=chat,
    )
    await bm.CDRAFTS.set_step(sid, 2, expected_chat_id=chat)
    return sid


@pytest.mark.asyncio
async def test_nav_cancel_with_content_draft_shows_dialog_keeps_active():
    await init_db()
    chat = 94_001
    sid = await _content_draft(chat)
    state = FakeState({"cc_session": sid})
    msg = FakeMessage(chat_id=chat)
    await bm.on_nav_cancel(FakeCallbackQuery(msg, uid=chat), state)
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.status == "active"  # НЕ abandoned — только диалог
    # диалог с тремя кнопками cc:exit_*
    mk = next(kw.get("reply_markup") for _, kw in msg.answers if kw.get("reply_markup"))
    cbs = [b.callback_data for row in mk.inline_keyboard for b in row]
    assert any("exit_keep" in c for c in cbs)
    assert any("exit_drop" in c for c in cbs)
    assert any("exit_stay" in c for c in cbs)


@pytest.mark.asyncio
async def test_nav_cancel_with_empty_draft_abandons_without_dialog():
    await init_db()
    chat = 94_002
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    state = FakeState({"cc_session": sid})
    msg = FakeMessage(chat_id=chat)
    await bm.on_nav_cancel(FakeCallbackQuery(msg, uid=chat), state)
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.status == "abandoned"  # пустой черновик — прежний быстрый путь


@pytest.mark.asyncio
async def test_cancel_cmd_with_content_draft_shows_dialog():
    await init_db()
    chat = 94_003
    sid = await _content_draft(chat)
    state = FakeState({"cc_session": sid})
    msg = FakeMessage("/cancel", chat_id=chat)
    await bm.cancel_cmd(msg, state)
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.status == "active"
    assert any(kw.get("reply_markup") for _, kw in msg.answers)  # диалог показан


@pytest.mark.asyncio
async def test_exit_keep_soft_exits_and_resume_offered():
    await init_db()
    chat = 94_004
    sid = await _content_draft(chat)
    state = FakeState({"cc_session": sid})
    msg = FakeMessage(chat_id=chat)
    await bm.cc_exit_keep(FakeCallbackQuery(msg, uid=chat), state)
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.status == "active"  # черновик жив
    assert await state.get_state() is None and (await state.get_data()) == {}  # FSM закрыт
    # /newcampaign после soft-exit предлагает продолжить
    entry_msg = FakeMessage(chat_id=chat)
    await bm._cc_entry(entry_msg, FakeState())
    assert any(kw.get("reply_markup") for _, kw in entry_msg.answers), "резюм-оффер не показан"
    snap2 = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap2.status == "active"


@pytest.mark.asyncio
async def test_exit_drop_abandons_draft():
    await init_db()
    chat = 94_005
    sid = await _content_draft(chat)
    state = FakeState({"cc_session": sid})
    msg = FakeMessage(chat_id=chat)
    await bm.cc_exit_drop(FakeCallbackQuery(msg, uid=chat), state)
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.status == "abandoned"


@pytest.mark.asyncio
async def test_exit_drop_works_even_if_fsm_cleared():
    """Menu-guard между диалогом и кнопкой чистит FSM — «удалить» всё равно добивает черновик."""
    await init_db()
    chat = 94_006
    sid = await _content_draft(chat)
    state = FakeState()  # FSM уже пуст
    msg = FakeMessage(chat_id=chat)
    await bm.cc_exit_drop(FakeCallbackQuery(msg, uid=chat), state)
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.status == "abandoned"


@pytest.mark.asyncio
async def test_exit_stay_rerenders_current_stage():
    await init_db()
    chat = 94_007
    sid = await _content_draft(chat)
    state = FakeState({"cc_session": sid})
    msg = FakeMessage(chat_id=chat)
    await bm.cc_exit_stay(FakeCallbackQuery(msg, uid=chat), state)
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.status == "active" and snap.current_step == 2
    assert msg.answers  # этап перерисован


def test_wizard_keyboards_cancel_reachable_by_exit_dialog():
    """Класс-гард: у всех cc_*-клавиатур кнопка «Отмена» пакуется в nav:cancel — единственный
    колбэк, который перехватывает _maybe_cc_exit_dialog. Новая клавиатура с иным cancel-путём
    молча вернула бы безвозвратный abandon."""
    from bot import keyboards as kb

    factories = [
        kb.cc_settings_kb,
        kb.cc_kw_kb,
        kb.cc_kw_verify_kb,
        kb.cc_kw_confirm_kb,
        kb.cc_assets_kb,
        kb.cc_skip_kb,
        kb.cc_final_kb,
    ]
    for factory in factories:
        markup = factory()
        cbs = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
        cancels = [cd for t, cd in cbs if "Отмена" in t or "Cancel" in t]
        assert cancels, f"{factory.__name__}: нет кнопки Отмена"
        assert all(cd.startswith("nav:") for cd in cancels), (
            f"{factory.__name__}: cancel мимо nav:cancel → диалог W5 не сработает"
        )
