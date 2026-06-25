"""Тесты маршрутизации inline-колбэков bot.main: /campaigns пауза/возобновление (только
черновик, без исполнения), RSA-курация, легаси ok:-колбэк. Реальный ConfirmStore/SessionStore
на temp SQLite (conftest); сеть/SDK не трогаем.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from bot.callbacks import CampCB, RsaCB  # noqa: E402
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
    def __init__(self, chat_id: int = 100, bot=None):
        self.chat = type("C", (), {"id": chat_id})()
        self.bot = bot
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text: str = "", **kw):
        self.edits.append((text, kw))
        return self


class FakeCallbackQuery:
    def __init__(self, message, data: str = "", uid: int = 100):
        self.message = message
        self.data = data
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


# ── /campaigns: кнопка пауза создаёт ТОЛЬКО черновик (исполнение — после ✅) ───────
async def test_camp_pause_creates_pending_proposal_only():
    await init_db()
    chat_id = 201
    bm._CAMP_CACHE[chat_id] = [{"name": "BrandX", "status": "ENABLED"}]
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.camp_pause(cq, CampCB(action="pause", idx=0))

    cid = bm._LAST_PENDING.get(chat_id)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "pause_campaign"
    assert snap.status == "pending"  # НЕ исполнено — ждёт ✅
    assert snap.user_initiated is True


async def test_camp_resume_creates_pending_proposal():
    await init_db()
    chat_id = 202
    bm._CAMP_CACHE[chat_id] = [{"name": "BrandY", "status": "PAUSED"}]
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.camp_resume(cq, CampCB(action="resume", idx=0))
    snap = await ConfirmStore().get_confirmed(bm._LAST_PENDING[chat_id])
    assert snap.operation == "resume_campaign" and snap.status == "pending"


async def test_camp_mutate_stale_cache_alerts_no_proposal():
    await init_db()
    chat_id = 203
    bm._CAMP_CACHE.pop(chat_id, None)
    bm._LAST_PENDING.pop(chat_id, None)
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.camp_pause(cq, CampCB(action="pause", idx=0))
    assert cq.answers and cq.answers[-1][1] is True  # show_alert: список устарел
    assert bm._LAST_PENDING.get(chat_id) is None  # черновик не создан


# ── RSA-курация ──────────────────────────────────────────────────────────────────
async def _make_session(chat_id: int):
    return await bm.SESSIONS.create(
        chat_id=chat_id,
        customer_id=DRAFT_ACCOUNT_ID,
        campaign="C",
        ad_group_id="1",
        ad_group_name="AG",
        final_url="https://example.com",
        headlines=["заголовок", "второй", "третий"],
        descriptions=["описание один", "описание два"],
        brief={},
    )


async def test_rsa_approve_sets_state_and_rerenders():
    await init_db()
    sid = await _make_session(204)
    cq = FakeCallbackQuery(FakeMessage(chat_id=204))
    await bm.rsa_approve(cq, RsaCB(action="approve", cid=sid, kind="h", idx=0))
    assert cq.answers and cq.answers[-1][0] == "Одобрено"
    assert cq.message.edits  # шаг курации перерисован в том же сообщении


async def test_rsa_finalize_below_min_alerts():
    await init_db()
    sid = await _make_session(205)  # свежая сессия — 0 одобренных
    cq = FakeCallbackQuery(FakeMessage(chat_id=205))
    await bm.rsa_finalize(cq, RsaCB(action="finalize", cid=sid))
    assert cq.answers and cq.answers[-1][1] is True  # show_alert: ниже минимума
    assert bm._LAST_PENDING.get(205) is None  # create_rsa-черновик НЕ создан


async def test_rsa_session_stale_alerts():
    await init_db()
    cq = FakeCallbackQuery(FakeMessage(chat_id=206))
    await bm.rsa_approve(cq, RsaCB(action="approve", cid="no-such-session", kind="h", idx=0))
    assert cq.answers and cq.answers[-1][1] is True  # show_alert: сессия устарела


# ── Легаси ok:-колбэк маршрутизируется в confirm-путь ─────────────────────────────
async def test_legacy_ok_callback_routes_to_confirm():
    await init_db()
    cid = uuid.uuid4().hex
    await bm.STORE.save_proposal(
        confirmation_id=cid,
        operation="resume_campaign",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary="resume X",
        chat_id=207,
        user_initiated=True,
    )

    async def fake_exec(s, c):
        assert await s.claim(c, operation="resume_campaign") is not None
        await s.finalize(c, result={"applied": True})
        return {"applied": True}

    cq = FakeCallbackQuery(FakeMessage(chat_id=207), data=f"ok:{cid}")
    with patched(bm, "execute_confirmed", fake_exec):
        await bm.on_confirm_legacy(cq)
    assert (await ConfirmStore().get_confirmed(cid)).status == "applied"
