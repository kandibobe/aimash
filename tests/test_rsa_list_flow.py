"""§10 list-UX: round-trip присланного списка → SessionStore.replace_all (всё approved) →
_build_rsa_proposal (валидный create_rsa), и что неверное КОЛИЧЕСТВО оставляет менеджера в шаге."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from core.texts import parse_rsa_paste  # noqa: E402
from db.session import init_db  # noqa: E402


class FakeMessage:
    def __init__(self, chat_id: int = 100, text: str = ""):
        self.chat = type("C", (), {"id": chat_id})()
        self.text = text
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self


class FakeState:
    def __init__(self):
        self._data: dict = {}
        self._state = None

    async def clear(self):
        self._data, self._state = {}, None

    async def set_state(self, s):
        self._state = s

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)

    async def get_state(self):
        return self._state


async def _make_session(chat_id: int) -> str:
    return await bm.SESSIONS.create(
        chat_id=chat_id,
        customer_id=bm.DRAFT_ACCOUNT_ID,
        campaign="Search Spring",
        ad_group_id="111",
        ad_group_name="AG",
        final_url="https://example.com",
        headlines=["Купить окна", "Окна ПВХ Киев", "Установка окон"],
        descriptions=["Гарантия на все окна пять лет.", "Бесплатный замер и доставка."],
    )


async def test_replace_all_then_build_create_rsa_proposal():
    await init_db()
    chat_id = 41_001
    sid = await _make_session(chat_id)

    edited = (
        "ЗАГОЛОВКИ (≤30):\n"
        "1. Окна недорого  [14/30]\n"
        "2. Окна ПВХ Киев  [12/30]\n"
        "3. Монтаж окон  [11/30]\n"
        "4. Замер бесплатно  [15/30]\n"
        "\n"
        "ОПИСАНИЯ (≤90):\n"
        "1. Гарантия пять лет на все окна.  [30/90]\n"
        "2. Доставка и монтаж под ключ.  [27/90]\n"
    )
    h, d = parse_rsa_paste(edited)
    session = await bm.SESSIONS.replace_all(sid, h, d, expected_chat_id=chat_id)
    assert session is not None
    # всё approved (менеджер прислал финальный набор)
    assert all(e["state"] == "approved" for e in session.headlines)
    assert all(e["state"] == "approved" for e in session.descriptions)

    cid, op, params, summary = bm._build_rsa_proposal(session)
    assert op == "create_rsa"
    assert params["headlines"] == h
    assert params["descriptions"] == d
    assert params["ad_group_id"] == "111"


async def test_rsa_list_edited_bad_count_stays_in_state_no_proposal():
    await init_db()
    chat_id = 41_002
    sid = await _make_session(chat_id)
    state = FakeState()
    await state.set_state(bm.RsaList.awaiting_edited)
    await state.update_data(cid=sid)

    # только 1 заголовок → ниже минимума (3) → ошибка, остаёмся в шаге, черновик НЕ минтуется
    bad = "ЗАГОЛОВКИ (≤30):\n1. Только один\n\nОПИСАНИЯ (≤90):\n1. Одно описание тут."
    await bm.rsa_list_edited(FakeMessage(chat_id=chat_id, text=bad), state)
    assert bm._LAST_PENDING.get(chat_id) is None
    assert await state.get_state() == bm.RsaList.awaiting_edited
