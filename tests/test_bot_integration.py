"""E2E-тесты бот-слоя: on_text → proposal → store → confirm/cancel → execute → audit.

Закрывает дыру из аудита: «нет интеграционных тестов confirm OK/NO роундтрипа на уровне
хендлеров». Импортируем реальные хендлеры bot.main, реальный ConfirmStore на temp SQLite
(conftest), а LLM (handle_command) и исполнение (execute_confirmed) подменяем. Фейки aiogram —
локальные (Message/CallbackQuery/FSM/Bot), без сети/Telegram.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

import bot.main as bm  # noqa: E402
from bot.callbacks import ConfirmCB  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from db.models import AuditLog  # noqa: E402
from db.session import Session, init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ── Локальные фейки aiogram ──────────────────────────────────────────────────────
class FakeBot:
    async def send_chat_action(self, *a, **k):
        pass


class FakeMessage:
    def __init__(self, text: str = "", chat_id: int = 100, bot=None):
        self.text = text
        self.chat = type("C", (), {"id": chat_id})()
        self.bot = bot
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def answer_document(self, doc, **kw):
        self.answers.append(("<doc>", kw))
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


class FakeFSM:
    def __init__(self):
        self._d: dict = {}

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)

    async def set_state(self, *a, **k):
        pass

    async def clear(self):
        self._d = {}


def _proposal_handler(cid: str):
    async def _h(text, chat_id):
        return {
            "type": "proposal",
            "operation": "resume_campaign",
            "params": {"campaign": "X"},
            "summary": "Возобновить кампанию X",
            "confirmation_id": cid,
        }

    return _h


async def _audit_statuses(cid: str) -> list[str]:
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.confirmation_id == cid).order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
    return [r.status for r in rows]


# ── confirm OK роундтрип ─────────────────────────────────────────────────────────
async def test_on_text_proposal_then_confirm_ok():
    await init_db()
    cid = uuid.uuid4().hex
    store = ConfirmStore()

    msg = FakeMessage("возобнови X", chat_id=101, bot=FakeBot())
    with patched(bm, "handle_command", _proposal_handler(cid)):
        await bm.on_text(msg, FakeFSM())
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "pending"
    assert snap.user_initiated is True  # доверенный слой ставит True
    assert bm._LAST_PENDING.get(101) == cid

    async def fake_exec(s, c):  # имитирует контракт execute_confirmed: claim → finalize
        assert await s.claim(c, operation="resume_campaign") is not None
        await s.finalize(c, result={"applied": True})
        return {"applied": True}

    cq = FakeCallbackQuery(FakeMessage(chat_id=101, bot=FakeBot()))
    with patched(bm, "execute_confirmed", fake_exec):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid))

    assert (await store.get_confirmed(cid)).status == "applied"
    statuses = await _audit_statuses(cid)
    assert "confirmed" in statuses and "applied" in statuses
    assert cq.message.edits  # сообщение отредактировано (кнопки убраны → «Готово»)


# ── confirm NO ───────────────────────────────────────────────────────────────────
async def test_on_text_proposal_then_cancel():
    await init_db()
    cid = uuid.uuid4().hex
    store = ConfirmStore()
    msg = FakeMessage("возобнови X", chat_id=102, bot=FakeBot())
    with patched(bm, "handle_command", _proposal_handler(cid)):
        await bm.on_text(msg, FakeFSM())

    cq = FakeCallbackQuery(FakeMessage(chat_id=102))
    await bm.on_cancel(cq, ConfirmCB(action="no", cid=cid))

    assert (await store.get_confirmed(cid)).status == "rejected"
    assert "rejected" in await _audit_statuses(cid)
    assert bm._LAST_PENDING.get(102) is None  # очищен


# ── устаревший/неизвестный cid: алерт, execute НЕ вызывается ──────────────────────
async def test_confirm_stale_cid_alerts_and_skips_execute():
    await init_db()
    calls = {"n": 0}

    async def fake_exec(s, c):
        calls["n"] += 1
        return {}

    cq = FakeCallbackQuery(FakeMessage(chat_id=103))
    with patched(bm, "execute_confirmed", fake_exec):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid="bogus-unknown"))
    assert calls["n"] == 0  # без подтверждённого черновика SDK-путь не трогаем
    assert cq.answers and cq.answers[-1][1] is True  # show_alert=True


# ── capability-guard: текстовый ответ агента → НЕ создаём черновик/кнопки ─────────
async def test_on_text_text_decline_creates_no_proposal():
    await init_db()

    async def _decline(text, chat_id):
        return {"type": "text", "text": "Операция «X» пока не поддерживается."}

    msg = FakeMessage("сделай X", chat_id=104, bot=FakeBot())
    with patched(bm, "handle_command", _decline):
        await bm.on_text(msg, FakeFSM())
    assert msg.answers and "не поддерживается" in msg.answers[-1][0]
    # ответ без inline-кнопок (reply_markup не передан)
    assert "reply_markup" not in msg.answers[-1][1]
    assert bm._LAST_PENDING.get(104) is None  # черновик не создан


# ── ошибка исполнения: failed + audit failed ─────────────────────────────────────
async def test_confirm_execute_failure_records_failed():
    await init_db()
    cid = uuid.uuid4().hex
    store = ConfirmStore()
    msg = FakeMessage("возобнови X", chat_id=105, bot=FakeBot())
    with patched(bm, "handle_command", _proposal_handler(cid)):
        await bm.on_text(msg, FakeFSM())

    async def fake_exec_fail(s, c):
        raise RuntimeError("sdk boom")

    cq = FakeCallbackQuery(FakeMessage(chat_id=105))
    with patched(bm, "execute_confirmed", fake_exec_fail):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid))

    assert (await store.get_confirmed(cid)).status == "failed"
    assert "failed" in await _audit_statuses(cid)


# ── ТЗ §5: большой список ключей в черновике → .xlsx-вложение, маленький → инлайн ──
async def test_big_keyword_proposal_attaches_xlsx():
    await init_db()
    cid = uuid.uuid4().hex
    msg = FakeMessage(bot=FakeBot())
    params = {
        "campaign": "Search",
        "keywords": [f"kw{i}" for i in range(30)],  # > KW_INLINE_MAX
        "match_type": "phrase",
    }
    await bm._present_proposal(
        msg, chat_id=201, operation="add_keywords", params=params, summary="raw dict", cid=cid
    )
    assert any(a[0] == "<doc>" for a in msg.answers)  # .xlsx вложение
    assert any(a[1].get("reply_markup") for a in msg.answers)  # сообщение с кнопками ✅/❌
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap is not None and "фразовое" in snap.summary and "{" not in snap.summary


async def test_small_keyword_proposal_inline_no_doc():
    await init_db()
    cid = uuid.uuid4().hex
    msg = FakeMessage(bot=FakeBot())
    params = {
        "campaign": "Search",
        "keywords": ["купить телефон", "смартфон"],
        "match_type": "broad",
    }
    await bm._present_proposal(
        msg, chat_id=202, operation="add_keywords", params=params, summary="raw", cid=cid
    )
    assert all(a[0] != "<doc>" for a in msg.answers)  # маленький список — без вложения
    assert any(a[1].get("reply_markup") for a in msg.answers)
