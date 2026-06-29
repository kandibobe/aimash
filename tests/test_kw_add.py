"""§7: флоу «добавить подобранные ключи в кампанию» (research → кампания → тип соответствия →
confirm-гейт). Проверяем: серверный токен-стор, клавиатуры, и что финал собирает ТОЛЬКО черновик
add_keywords (pending, user_initiated) — исполнение лишь после ✅. Реальный ConfirmStore на temp
SQLite (conftest); сеть/SDK не трогаем (add_keywords не diffable → read_before без вызова)."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot.callbacks import KwAddCB  # noqa: E402
from bot.keyboards import kw_add_kb, match_type_kb  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from db.session import init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


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


# ── токен-стор ────────────────────────────────────────────────────────────────────
def test_kw_add_put_stores_and_returns_token():
    bm._KW_ADD.clear()
    token = bm._kw_add_put(["a", "b"], "src")
    assert token in bm._KW_ADD
    assert bm._KW_ADD[token]["keywords"] == ["a", "b"]
    assert bm._KW_ADD[token]["src"] == "src"


def test_kw_add_put_caps_size_evicting_oldest():
    bm._KW_ADD.clear()
    with patched(bm, "_KW_ADD_MAX", 3):
        toks = [bm._kw_add_put([f"k{i}"], "s") for i in range(5)]
    assert len(bm._KW_ADD) <= 3
    assert toks[-1] in bm._KW_ADD  # новейший на месте
    assert toks[0] not in bm._KW_ADD  # старейший вытеснен


# ── клавиатуры ─────────────────────────────────────────────────────────────────────
def test_kw_add_kb_single_start_button():
    m = kw_add_kb("TOK")
    btns = [b for row in m.inline_keyboard for b in row]
    assert len(btns) == 1
    cb = KwAddCB.unpack(btns[0].callback_data)
    assert cb.action == "start" and cb.token == "TOK"


def test_match_type_kb_has_three_match_types_plus_cancel():
    mt = match_type_kb("TOK")
    cbs = [KwAddCB.unpack(b.callback_data) for row in mt.inline_keyboard for b in row]
    assert {c.mt for c in cbs if c.action == "match"} == {"broad", "phrase", "exact"}
    assert any(c.action == "cancel" for c in cbs)
    assert all(c.token == "TOK" for c in cbs)


# ── полный флоу: research → кампания → тип → ТОЛЬКО черновик add_keywords ──────────
async def test_kw_add_flow_builds_pending_add_keywords_only():
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_701
    token = bm._kw_add_put(["купить телефон", "смартфон цена", "телефон купить"], "телефон")

    # 1) старт: ставит состояние awaiting_campaign + кладёт токен
    state = FakeState()
    await bm.on_kw_add_start(
        FakeCallbackQuery(FakeMessage(chat_id=chat_id)), KwAddCB(action="start", token=token), state
    )
    assert await state.get_state() == bm.KwAdd.awaiting_campaign

    # 2) название кампании → показываем выбор типа соответствия, кампания в сессии
    await bm.kw_add_campaign(FakeMessage(chat_id=chat_id, text="Search Spring"), state)
    assert bm._KW_ADD[token]["campaign"] == "Search Spring"

    # 3) тип соответствия → собран ТОЛЬКО черновик add_keywords (pending), токен израсходован
    await bm.on_kw_add_match(
        FakeCallbackQuery(FakeMessage(chat_id=chat_id)),
        KwAddCB(action="match", token=token, mt="phrase"),
    )
    cid = bm._LAST_PENDING.get(chat_id)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "add_keywords"
    assert snap.status == "pending"  # НЕ исполнено — ждёт ✅
    assert snap.user_initiated is True  # прямая команда человека
    assert snap.params["campaign"] == "Search Spring"
    assert snap.params["match_type"] == "phrase"
    assert len(snap.params["keywords"]) >= 1
    assert token not in bm._KW_ADD  # сессия израсходована


async def test_kw_add_match_with_stale_token_no_proposal():
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_702
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.on_kw_add_match(cq, KwAddCB(action="match", token="missing", mt="broad"))
    assert bm._LAST_PENDING.get(chat_id) is None  # черновик не создан
    assert cq.answers and cq.answers[-1][1] is True  # show_alert=True (stale)
