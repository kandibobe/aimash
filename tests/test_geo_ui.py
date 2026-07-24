"""§3: флоу «гео-таргетинг кампании из меню» (меню → способ → текст → confirm-гейт). Проверяем:
клавиатуры, парсеры ввода, и что финал собирает ТОЛЬКО черновик set_geo_* (pending) — исполнение
лишь после ✅. Реальный ConfirmStore на temp SQLite (conftest); сеть/SDK не трогаем (set_geo_* не
diffable → read_before возвращает None без клиента)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot.callbacks import CampCB, GeoCB  # noqa: E402
from bot.keyboards import campaign_actions_kb, geo_mode_kb  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
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


# ── клавиатуры ─────────────────────────────────────────────────────────────────────
def test_campaign_actions_kb_has_geo_button():
    m = campaign_actions_kb(2, "ENABLED")
    geo = [
        CampCB.unpack(b.callback_data)
        for row in m.inline_keyboard
        for b in row
        if b.callback_data.startswith("camp:")
    ]
    assert any(c.action == "geo" and c.idx == 2 for c in geo)


def test_geo_mode_kb_has_loc_prox_and_back():
    mk = geo_mode_kb(5)
    geo = [
        GeoCB.unpack(b.callback_data)
        for row in mk.inline_keyboard
        for b in row
        if b.callback_data.startswith("geo:")
    ]
    assert {c.action for c in geo} == {"loc", "prox"}
    assert all(c.idx == 5 for c in geo)
    # «‹ Назад» возвращает в меню кампании через CampCB (а не отдельный geo-cancel)
    backs = [
        CampCB.unpack(b.callback_data)
        for row in mk.inline_keyboard
        for b in row
        if b.callback_data.startswith("camp:")
    ]
    assert any(c.action == "menu" and c.idx == 5 for c in backs)


# ── парсеры ввода ───────────────────────────────────────────────────────────────────
def test_parse_geo_locations_splits_and_strips():
    assert bm._parse_geo_locations("Киев, Львов,  Одесса\nХарьков") == [
        "Киев",
        "Львов",
        "Одесса",
        "Харьков",
    ]
    assert bm._parse_geo_locations("   ,  ,") == []  # только разделители → пусто


def test_parse_geo_proximity_variants():
    assert bm._parse_geo_proximity("Киев, 10") == ("Киев", 10.0)
    assert bm._parse_geo_proximity("Нью-Йорк | 25.5") == ("Нью-Йорк", 25.5)
    # последняя запятая — разделитель: город с запятой сохраняется
    assert bm._parse_geo_proximity("Киев, центр, 5") == ("Киев, центр", 5.0)
    assert bm._parse_geo_proximity("Киев") is None  # нет радиуса
    assert bm._parse_geo_proximity("Киев, abc") is None  # радиус не число
    assert bm._parse_geo_proximity(", 10") is None  # нет города


# ── полный флоу (локации): способ → текст → ТОЛЬКО черновик set_geo_location ─────────
async def test_geo_location_flow_builds_pending_set_geo_location():
    await init_db()
    chat_id = 30_801
    bm._CAMP_CACHE[chat_id] = [{"name": "Search Spring", "status": "ENABLED"}]

    # 1) способ «по локации» → состояние awaiting_locations + кампания в state-data
    state = FakeState()
    await on_mode(chat_id, "loc", 0, state)
    assert await state.get_state() == bm.Geo.awaiting_locations

    # 2) текст локаций → собран ТОЛЬКО черновик set_geo_location (pending)
    await bm.geo_locations(FakeMessage(chat_id=chat_id, text="Киев, Львов"), state)
    snap = await _last_snap(chat_id)
    assert snap.operation == "set_geo_location"
    assert snap.status == "pending"  # НЕ исполнено — ждёт ✅
    assert snap.params["campaign"] == "Search Spring"
    assert snap.params["locations"] == ["Киев", "Львов"]
    assert snap.params.get("country_code") == ""  # дефолт схемы ПУСТ (без биаса «UA»)


# ── полный флоу (радиус): способ → текст → ТОЛЬКО черновик set_geo_proximity ─────────
async def test_geo_proximity_flow_builds_pending_set_geo_proximity():
    await init_db()
    chat_id = 30_802
    bm._CAMP_CACHE[chat_id] = [{"name": "Local Plumber", "status": "ENABLED"}]

    state = FakeState()
    await on_mode(chat_id, "prox", 0, state)
    assert await state.get_state() == bm.Geo.awaiting_proximity

    await bm.geo_proximity(FakeMessage(chat_id=chat_id, text="Киев, 10"), state)
    snap = await _last_snap(chat_id)
    assert snap.operation == "set_geo_proximity"
    assert snap.status == "pending"
    assert snap.params["campaign"] == "Local Plumber"
    assert snap.params["city_name"] == "Киев"
    assert snap.params["radius_km"] == 10.0


# ── негативные ветки: ввод не создаёт черновик и не теряет состояние ─────────────────
async def test_geo_proximity_bad_input_keeps_state_no_proposal():
    await init_db()
    chat_id = 30_803
    bm._LAST_PENDING.pop(chat_id, None)
    state = FakeState()
    await state.set_state(bm.Geo.awaiting_proximity)
    await state.update_data(geo_campaign="X")
    msg = FakeMessage(chat_id=chat_id, text="без радиуса")
    await bm.geo_proximity(msg, state)
    assert bm._LAST_PENDING.get(chat_id) is None  # черновик не создан
    assert await state.get_state() == bm.Geo.awaiting_proximity  # остаёмся в состоянии
    assert msg.answers  # был ответ-подсказка


async def test_geo_missing_campaign_in_state_no_proposal():
    await init_db()
    chat_id = 30_804
    bm._LAST_PENDING.pop(chat_id, None)
    state = FakeState()
    await state.set_state(bm.Geo.awaiting_locations)  # state БЕЗ geo_campaign
    msg = FakeMessage(chat_id=chat_id, text="Киев")
    await bm.geo_locations(msg, state)
    assert bm._LAST_PENDING.get(chat_id) is None  # черновик не создан (gh stale)
    assert await state.get_state() is None  # состояние очищено


# ── helpers ─────────────────────────────────────────────────────────────────────────
async def on_mode(chat_id: int, action: str, idx: int, state: FakeState) -> None:
    await bm.on_geo_mode(
        FakeCallbackQuery(FakeMessage(chat_id=chat_id)), GeoCB(action=action, idx=idx), state
    )


async def _last_snap(chat_id: int):
    cid = bm._LAST_PENDING.get(chat_id)
    assert cid is not None
    return await ConfirmStore().get_confirmed(cid)
