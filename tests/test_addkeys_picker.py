"""D3 (удобство 2026-07): /addkeys — пикер кампаний активного аккаунта (текст-ввод остаётся).

Пикер — удобство; сбой/пусто/крупный аккаунт ⇒ текст-подсказка. Выбор кнопкой = тот же хвост,
что и ввод названия текстом (сбор ввода ДО confirm-гейта — ничего не мутируется на клик).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402


class _State:
    def __init__(self):
        self.data: dict = {}
        self.state = None
        self.cleared = False

    async def clear(self):
        self.cleared = True
        self.data = {}
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


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

    async def answer(self, *a, **kw):
        pass


_CAMPS = [
    {"name": "Летняя распродажа", "id": "10", "status": "ENABLED"},
    {"name": "Зимняя коллекция", "id": "20", "status": "PAUSED"},
]


async def test_addkeys_start_shows_picker_when_campaigns_available(monkeypatch):
    from bot.handlers.keywords_flow import addkeys_start

    async def fake_load(chat_id):
        return list(_CAMPS)

    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_load)
    chat_id = 5150
    m = _Msg(chat_id)
    st = _State()
    await addkeys_start(m, st)
    assert st.state is bm.KwAdd.awaiting_campaign
    token = st.data["kw_add_token"]
    assert bm._KW_ADD_CAMP_CACHE[chat_id] == _CAMPS  # кэш заполнен для резолва idx→имя
    markup = m.sent[-1][1].get("reply_markup")
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Летняя" in t for t in labels) and any(
        "Отмена" in t or "Cancel" in t for t in labels
    )
    assert token  # сессия ключей заведена


async def test_addkeys_start_text_fallback_when_no_campaigns(monkeypatch):
    from bot.handlers.keywords_flow import addkeys_start

    async def fake_empty(chat_id):
        return []

    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_empty)
    m = _Msg(5151)
    st = _State()
    await addkeys_start(m, st)
    # текст-подсказка (не list-вариант); флоу по-прежнему ждёт название текстом
    assert m.sent and st.state is bm.KwAdd.awaiting_campaign


async def test_pick_campaign_button_sets_campaign_and_advances(monkeypatch):
    from bot.handlers.keywords_flow import on_kw_add_pick_campaign

    chat_id = 5152
    token = bm._kw_add_put([], "manual")  # своя сессия (без кандидатов) → попросит прислать список
    bm._KW_ADD_CAMP_CACHE[chat_id] = list(_CAMPS)
    cq = _Cq(chat_id)
    st = _State()
    await on_kw_add_pick_campaign(cq, SimpleNamespace(action="camp", token=token, idx=1), st)
    assert bm._KW_ADD[token]["campaign"] == "Зимняя коллекция"  # idx=1 → вторая кампания
    assert st.state is bm.KwAdd.awaiting_keywords  # перешли к приёму списка ключей
    bm._KW_ADD.pop(token, None)


async def test_pick_campaign_stale_cache(monkeypatch):
    from bot.handlers.keywords_flow import on_kw_add_pick_campaign

    chat_id = 5153
    bm._KW_ADD_CAMP_CACHE.pop(chat_id, None)
    alerted = {"n": 0}

    class _CqAlert(_Cq):
        async def answer(self, *a, **kw):
            if kw.get("show_alert"):
                alerted["n"] += 1

    st = _State()
    await on_kw_add_pick_campaign(
        _CqAlert(chat_id), SimpleNamespace(action="camp", token="x", idx=0), st
    )
    assert alerted["n"] == 1  # stale-alert, без перехода
    assert st.state is None


async def test_text_fallback_still_advances(monkeypatch):
    """Ввод названия текстом (без пикера) по-прежнему ведёт к приёму ключей — фолбэк не сломан."""
    from bot.handlers.keywords_flow import kw_add_campaign

    token = bm._kw_add_put([], "manual")
    st = _State()
    st.data = {"kw_add_token": token}
    m = _Msg(5154)
    m.text = "Ручная кампания"
    await kw_add_campaign(m, st)
    assert bm._KW_ADD[token]["campaign"] == "Ручная кампания"
    assert st.state is bm.KwAdd.awaiting_keywords
    bm._KW_ADD.pop(token, None)
