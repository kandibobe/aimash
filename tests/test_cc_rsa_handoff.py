"""§19 Этап 3/4: handoff курации RSA в черновик визарда + пропуск изображений.

Курация принадлежит визарду (brief.cc_session) → rsa_finalize пишет утверждённые тексты в
campaign_drafts и переходит к Этапу 4, НЕ минтуя create_rsa proposal (чистота confirm-гейта).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from bot.callbacks import CcCB, RsaCB  # noqa: E402
from db.models import Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402


class FakeMessage:
    def __init__(self, text: str = "", chat_id: int = 100):
        self.text = text
        self.chat = type("C", (), {"id": chat_id})()
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text: str = "", **kw):
        self.edits.append((text, kw))
        return self


class FakeCallbackQuery:
    def __init__(self, message, uid: int = 100):
        self.message = message
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


class FakeFSM:
    def __init__(self, data: dict | None = None):
        self._d = dict(data or {})

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)

    async def set_state(self, *a, **k):
        pass

    async def clear(self):
        self._d = {}


async def _count_mutation_proposals(chat_id: int) -> int:
    """Исполняемые proposal'ы чата (исключая rsa_curation — это сессия курации, не мутация)."""
    async with Session() as s:
        return int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(Proposal)
                    .where(Proposal.chat_id == chat_id, Proposal.operation != "rsa_curation")
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_rsa_finalize_handoff_writes_draft_no_proposal():
    await init_db()
    chat = 7700101
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 3)
    # сессия курации, принадлежащая визарду (cc_session в brief)
    rsa_sid = await bm.SESSIONS.create(
        chat_id=chat,
        customer_id=DRAFT_ACCOUNT_ID,
        campaign="Кения Авто",
        ad_group_id="",
        ad_group_name="Кения Авто",
        final_url="https://shop.example/used",
        headlines=["Поддержанные авто", "Проверенные б/у авто", "Авто с гарантией"],
        descriptions=["Большой выбор авто с пробегом", "Гарантия и проверка перед покупкой"],
        brief={"topic": "авто", "cc_session": sid},
    )
    await bm.CDRAFTS.patch(
        sid, lambda st: st["ad"].__setitem__("rsa_session_id", rsa_sid), expected_chat_id=chat
    )
    # утверждаем все валидные → can_finalize
    await bm.SESSIONS.approve_all_valid(rsa_sid, expected_chat_id=chat)

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    fsm = FakeFSM({"cc_session": sid})
    await bm.rsa_finalize(cq, RsaCB(action="finalize", cid=rsa_sid), fsm)

    snap = await bm.CDRAFTS.get(sid)
    assert snap.current_step == 4  # перешли к изображениям
    assert len(snap.wizard_state["ad"]["headlines"]) >= 3
    assert len(snap.wizard_state["ad"]["descriptions"]) >= 2
    assert await _count_mutation_proposals(chat) == 0  # НИ create_rsa, ни иного proposal


@pytest.mark.asyncio
async def test_stage4_skip_advances_without_proposal():
    await init_db()
    chat = 7700102
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 4)
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    fsm = FakeFSM({"cc_session": sid})
    await bm.cc_skip(cq, CcCB(action="skip"), fsm)
    snap = await bm.CDRAFTS.get(sid)
    assert snap.wizard_state["images"]["skipped"] is True
    assert snap.current_step == 5  # ушли к Этапу 5 (следующая фаза)
    assert await _count_mutation_proposals(chat) == 0
