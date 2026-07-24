"""Универсальная навигация мастеров (Часть 1): «‹ Назад» + «✖ Отмена» на каждом шаге FSM.

Проверяем: чистую клавиатуру nav_kb (cancel-only / back+cancel / EN-метки); общий on_nav_cancel
(чистит state, НЕ создаёт черновик, возвращает меню); и главное — что retry-подсказка после
НЕВАЛИДНОГО ввода снова несёт навигацию (исходный баг «застрял без Назад»). Шимы — как в
tests/test_geo_ui.py; ConfirmStore на temp SQLite (conftest); сеть/SDK не трогаем.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot.callbacks import CampCB, NavCB  # noqa: E402
from bot.keyboards import nav_kb  # noqa: E402
from db.session import init_db  # noqa: E402


class FakeMessage:
    def __init__(self, chat_id: int = 100, text: str = "", bot=None):
        self.chat = type("C", (), {"id": chat_id})()
        self.text = text
        self.bot = bot
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text: str = "", **kw):
        return self


class FakeCallbackQuery:
    def __init__(self, message, uid: int = 100):
        self.message = message
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self):
        self._data: dict = {}
        self._state = None

    async def clear(self):
        self._data = {}
        self._state = None

    async def set_state(self, s):
        self._state = s

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)

    async def get_state(self):
        return self._state


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ── nav_kb: чистая клавиатура (без БД/Ads) ───────────────────────────────────────────
def test_nav_kb_cancel_only_when_no_back():
    cbs = _callbacks(nav_kb())
    assert len(cbs) == 1
    assert NavCB.unpack(cbs[0]).action == "cancel"


def test_nav_kb_has_back_and_cancel():
    cbs = _callbacks(nav_kb(CampCB(action="menu", idx=3)))
    backs = [CampCB.unpack(c) for c in cbs if c.startswith("camp:")]
    assert any(c.action == "menu" and c.idx == 3 for c in backs)
    assert any(c.startswith("nav:") and NavCB.unpack(c).action == "cancel" for c in cbs)


def test_nav_kb_en_labels():
    texts = [
        b.text
        for row in nav_kb(CampCB(action="menu", idx=0), lang="en").inline_keyboard
        for b in row
    ]
    assert any("Back" in t for t in texts)
    assert any("Cancel" in t for t in texts)


def test_nav_cancel_callback_is_short():
    # 64-байтовый лимит callback_data — nav:cancel тривиально влезает.
    assert len(NavCB(action="cancel").pack().encode("utf-8")) <= 64


# ── on_nav_cancel: чистит state, НЕ создаёт черновик, возвращает меню ─────────────────
async def test_on_nav_cancel_clears_state_and_no_proposal():
    await init_db()
    chat_id = 30_901
    bm._LAST_PENDING.pop(chat_id, None)
    state = FakeState()
    await state.set_state(bm.Geo.awaiting_locations)
    await state.update_data(geo_campaign="X")
    msg = FakeMessage(chat_id=chat_id)
    await bm.on_nav_cancel(FakeCallbackQuery(msg), state)
    assert await state.get_state() is None  # FSM очищен
    assert bm._LAST_PENDING.get(chat_id) is None  # черновик НЕ создан
    # reply-меню пришло НОВЫМ сообщением (ReplyKeyboardMarkup нельзя через edit_text)
    assert any("reply_markup" in kw for _, kw in msg.answers)


# ── главный баг: retry после невалидного ввода СНОВА несёт навигацию ──────────────────
async def test_kw_add_empty_keeps_nav_cancel():
    await init_db()
    chat_id = 30_902
    state = FakeState()
    await state.set_state(bm.KwAdd.awaiting_campaign)
    await state.update_data(kw_add_token="tok")
    msg = FakeMessage(chat_id=chat_id, text="   ")  # пустое имя → retry
    await bm.kw_add_campaign(msg, state)
    assert await state.get_state() == bm.KwAdd.awaiting_campaign  # остаёмся в шаге
    mk = msg.answers[-1][1].get("reply_markup")
    assert mk is not None and any(c.startswith("nav:") for c in _callbacks(mk))


async def test_geo_locations_empty_keeps_back_and_cancel():
    await init_db()
    chat_id = 30_903
    state = FakeState()
    await state.set_state(bm.Geo.awaiting_locations)
    await state.update_data(geo_campaign="X", geo_idx=0)  # geo_idx → Back-цель
    msg = FakeMessage(chat_id=chat_id, text="  ,  ,")  # пустые локации → retry
    await bm.geo_locations(msg, state)
    assert await state.get_state() == bm.Geo.awaiting_locations
    cbs = _callbacks(msg.answers[-1][1]["reply_markup"])
    assert any(c.startswith("nav:") for c in cbs)  # Отмена
    assert any(c.startswith("camp:") for c in cbs)  # Назад → меню кампании


async def test_geo_proximity_bad_keeps_back_and_cancel():
    await init_db()
    chat_id = 30_904
    state = FakeState()
    await state.set_state(bm.Geo.awaiting_proximity)
    await state.update_data(geo_campaign="X", geo_idx=2)
    msg = FakeMessage(chat_id=chat_id, text="без радиуса")
    await bm.geo_proximity(msg, state)
    assert await state.get_state() == bm.Geo.awaiting_proximity
    cbs = _callbacks(msg.answers[-1][1]["reply_markup"])
    assert any(c.startswith("nav:") for c in cbs)
    backs = [CampCB.unpack(c) for c in cbs if c.startswith("camp:")]
    assert any(c.action == "menu" and c.idx == 2 for c in backs)


async def test_geo_nav_kb_cancel_only_without_idx():
    # geo_idx отсутствует в state → Back-цели нет, остаётся только «✖ Отмена».
    state = FakeState()
    await state.set_state(bm.Geo.awaiting_locations)
    mk = await bm._geo_nav_kb(state)
    cbs = _callbacks(mk)
    assert all(not c.startswith("camp:") for c in cbs)
    assert any(c.startswith("nav:") for c in cbs)
