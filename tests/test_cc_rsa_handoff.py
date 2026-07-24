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


# ── §19.5.2: поэлементная курация в визарде (батч-ряд editall/regen/aslist) ───────
from contextlib import contextmanager  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _kb_actions(markup) -> set[str]:
    """Все callback_data-строки клавиатуры (плоско)."""
    out: set[str] = set()
    for row in markup.inline_keyboard:
        for b in row:
            if b.callback_data:
                out.add(b.callback_data)
    return out


async def _mk_wizard_session(chat: int, *, wizard: bool = True) -> tuple[str, str | None]:
    """Сессия курации: с cc_session (визард §19) или без (standalone /rsa)."""
    sid = None
    brief = {"topic": "авто"}
    if wizard:
        sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
        await bm.CDRAFTS.set_step(sid, 3)
        brief["cc_session"] = sid
    rsa_sid = await bm.SESSIONS.create(
        chat_id=chat,
        customer_id=DRAFT_ACCOUNT_ID,
        campaign="Кения Авто",
        ad_group_id="",
        ad_group_name="Кения Авто",
        final_url="https://shop.example/used",
        headlines=["Поддержанные авто", "Проверенные б/у авто", "Авто с гарантией"],
        descriptions=["Большой выбор авто с пробегом", "Гарантия и проверка перед покупкой"],
        brief=brief,
    )
    return rsa_sid, sid


@pytest.mark.asyncio
async def test_wizard_session_keyboard_has_batch_row():
    """Сессия визарда → карточка элемента несёт батч-ряд §19.5.2 (editall/regen/aslist)."""
    await init_db()
    session = await bm.SESSIONS.get((await _mk_wizard_session(7700103))[0])
    _text, kb = bm._rsa_render(session)
    acts = _kb_actions(kb)
    assert any("editall" in a for a in acts), acts
    assert any("regen" in a for a in acts), acts
    assert any("aslist" in a for a in acts), acts
    # поэлементные кнопки на месте (ТЗ §10): approve/refine/reject
    for need in ("approve", "refine", "reject"):
        assert any(f":{need}:" in a or a.startswith(f"rsa:{need}") for a in acts), (need, acts)


@pytest.mark.asyncio
async def test_standalone_session_keyboard_no_batch_row():
    """Сессия БЕЗ cc_session (/rsa) → батч-ряда визарда нет (list-UX /rsa не менялся)."""
    await init_db()
    session = await bm.SESSIONS.get((await _mk_wizard_session(7700104, wizard=False))[0])
    _text, kb = bm._rsa_render(session)
    acts = _kb_actions(kb)
    assert not any("editall" in a for a in acts), acts
    assert not any("regen" in a for a in acts), acts


@pytest.mark.asyncio
async def test_rsa_regen_replaces_set_as_pending_no_proposal():
    """🔁 Сгенерировать заново: новый набор в pending (курация заново), НИ одного proposal."""
    await init_db()
    chat = 7700105
    rsa_sid, _sid = await _mk_wizard_session(chat)

    class _Gen:
        headlines = [f"Новый заголовок {i}" for i in range(1, 6)]
        descriptions = ["Новое описание один", "Новое описание два"]

    async def _fake_gen(brief):
        return _Gen()

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_generate_rsa", _fake_gen):
        await bm.rsa_regen(cq, RsaCB(action="regen", cid=rsa_sid), FakeFSM())

    session = await bm.SESSIONS.get(rsa_sid)
    assert [e["text"] for e in session.headlines][:2] == ["Новый заголовок 1", "Новый заголовок 2"]
    assert all(e["state"] == "pending" for e in session.headlines + session.descriptions)
    assert session.can_finalize() is False  # ничего не одобрено — только курация
    assert await _count_mutation_proposals(chat) == 0


@pytest.mark.asyncio
async def test_rsa_editall_switches_to_list_ux():
    """✏️ Доработать всё: переключение в list-UX (RsaList.awaiting_edited + плейн-список)."""
    await init_db()
    chat = 7700106
    rsa_sid, _sid = await _mk_wizard_session(chat)

    states: list = []

    class RecFSM(FakeFSM):
        async def set_state(self, st, *a, **k):
            states.append(st)

    msg = FakeMessage(chat_id=chat)
    await bm.rsa_editall(FakeCallbackQuery(msg), RsaCB(action="editall", cid=rsa_sid), RecFSM())
    assert states and states[-1] is bm.RsaList.awaiting_edited
    # плейн-список для копирования отправлен (без клавиатуры)
    assert any("Поддержанные авто" in (t or "") for t, _kw in msg.answers)
    assert await _count_mutation_proposals(chat) == 0


@pytest.mark.asyncio
async def test_rsa_regen_foreign_chat_rejected():
    """Гард владения: чужой chat_id не может перегенерировать чужую сессию."""
    await init_db()
    rsa_sid, _sid = await _mk_wizard_session(7700107)
    cq = FakeCallbackQuery(FakeMessage(chat_id=999_999), uid=999_999)
    called = []

    async def _fake_gen(brief):  # не должен вызваться
        called.append(1)

    with patched(bm, "_generate_rsa", _fake_gen):
        await bm.rsa_regen(cq, RsaCB(action="regen", cid=rsa_sid), FakeFSM())
    assert not called
    assert cq.answers and cq.answers[0][1] is True  # show_alert=True (stale)
