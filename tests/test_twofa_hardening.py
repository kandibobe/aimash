"""§12 2FA харденинг (A13+A14): косметика Telegram не сжигает черновик; перебор PIN ограничен.

A13: повторный cq.answer() на уже отвеченном/протухшем callback бросает TelegramBadRequest.
На re-entry 2FA (верный PIN → _do_confirm на ИСХОДНОМ ✅) это исключение раньше проходило ДО
try-блока execute и сжигало черновик confirmed-без-исполнения. Через _safe_answer — операция
исполняется несмотря на сбой answer.

A14: счётчик неудач PIN ПЕРСИСТЕНТЕН (bm._TWOFA_FAILS) — новый ✅ его НЕ обнуляет. После
_TWOFA_MAX_ATTEMPTS подряд — кулдаун (fail-closed: вход в 2FA-режим закрыт, опасная op заблокирована).

Фейки aiogram — локальные (как tests/test_twofa.py), без сети/Telegram.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from pydantic import SecretStr  # noqa: E402

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from bot.callbacks import ConfirmCB  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextmanager
def twofa_on(pin: str = "2468", *, enabled: bool = True):
    with (
        patched(settings, "two_factor_enabled", enabled),
        patched(settings, "two_factor_pin", SecretStr(pin)),
    ):
        yield


class FakeBot:
    async def send_chat_action(self, *a, **k):
        pass

    async def send_message(self, *a, **k):
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

    async def edit_text(self, text: str = "", **kw):
        self.edits.append((text, kw))
        return self

    async def delete(self):
        pass


class FakeCallbackQuery:
    """answer() опционально бросает TelegramBadRequest (эмуляция «query is too old»)."""

    def __init__(self, message, data: str = "", uid: int = 100, *, answer_raises: bool = False):
        self.message = message
        self.data = data
        self.from_user = type("U", (), {"id": uid, "username": "op"})()
        self.answers: list = []
        self._answer_raises = answer_raises

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))
        if self._answer_raises:
            raise TelegramBadRequest(
                method=None, message="query is too old and response timeout expired"
            )


class FakeFSM:
    def __init__(self):
        self._d: dict = {}
        self._state = None

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)

    async def set_state(self, s=None, *a, **k):
        self._state = s

    async def get_state(self):
        return self._state

    async def clear(self):
        self._d = {}
        self._state = None


def _fake_exec(counter: dict):
    async def _exec(store, cid):
        counter["n"] += 1
        snap = await store.get_confirmed(cid)
        assert await store.claim(cid, operation=snap.operation) is not None
        await store.finalize(cid, result={"applied": True})
        return {"applied": True}

    return _exec


async def _save_pending(store: ConfirmStore, cid: str, chat_id: int, operation: str) -> None:
    await store.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary=f"{operation} X",
        chat_id=chat_id,
        user_initiated=True,
    )


def _reset(chat: int) -> None:
    bm._TWOFA_PENDING.pop(chat, None)
    bm._TWOFA_FAILS.pop(chat, None)


# ── A13: TelegramBadRequest в cq.answer НЕ сжигает черновик ────────────────────────
async def test_stale_callback_answer_does_not_burn_draft_on_correct_pin():
    """Верный PIN → исполнение, даже если cq.answer протух (TelegramBadRequest). Без _safe_answer
    повторный answer на ИСХОДНОМ ✅ ронял бы _do_confirm ДО execute → confirmed-без-исполнения."""
    await init_db()
    cid, chat = uuid.uuid4().hex, 6201
    _reset(chat)
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "remove_campaign")
    counter = {"n": 0}
    fsm = FakeFSM()
    # cq, чей answer ВСЕГДА бросает TelegramBadRequest (как протухший callback на re-entry)
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat, bot=FakeBot()), answer_raises=True)
    with twofa_on("2468"), patched(bm, "execute_confirmed", _fake_exec(counter)):
        await bm.on_confirm(
            cq, ConfirmCB(action="ok", cid=cid), fsm
        )  # begin: answer бросит → swallow
        assert counter["n"] == 0
        await bm.on_twofa_code(FakeMessage("2468", chat_id=chat, bot=FakeBot()), fsm)  # верный PIN
    assert counter["n"] == 1  # исполнено несмотря на сбойный answer
    assert (await store.get_confirmed(cid)).status == "applied"  # черновик НЕ сожжён
    _reset(chat)


# ── A14: персистентный счётчик + локаут ────────────────────────────────────────────
async def test_new_confirm_tap_does_not_reset_fail_counter():
    """Ключевой инвариант A14: неудачи копятся ЧЕРЕЗ новые ✅. 2 неверных → новый ✅ → 1 неверный
    (3-й ПОДРЯД) обязан дать локаут (раньше новый ✅ обнулял бы счётчик → перебор бесконечен)."""
    await init_db()
    cid, chat = uuid.uuid4().hex, 6202
    _reset(chat)
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "remove_campaign")
    counter = {"n": 0}
    fsm = FakeFSM()
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat, bot=FakeBot()))
    with twofa_on("2468"), patched(bm, "execute_confirmed", _fake_exec(counter)):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid), fsm)
        await bm.on_twofa_code(FakeMessage("0000", chat_id=chat, bot=FakeBot()), fsm)  # fail 1
        await bm.on_twofa_code(FakeMessage("0000", chat_id=chat, bot=FakeBot()), fsm)  # fail 2
        assert bm._TWOFA_FAILS[chat]["fails"] == 2
        # новый ✅ (re-begin) — счётчик неудач НЕ должен обнулиться
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid), fsm)
        assert bm._TWOFA_FAILS[chat]["fails"] == 2  # пережил новый ✅
        await bm.on_twofa_code(
            FakeMessage("0000", chat_id=chat, bot=FakeBot()), fsm
        )  # fail 3 → локаут
    assert counter["n"] == 0
    assert bm._twofa_lock_remaining_s(chat) > 0  # выставлен кулдаун
    assert chat not in bm._TWOFA_PENDING  # ожидание снято
    assert (await store.get_confirmed(cid)).status == "pending"  # черновик уцелел
    _reset(chat)


async def test_locked_out_new_tap_denied_entry_op_stays_blocked():
    """Во время локаута новый ✅ на опасной op НЕ входит в 2FA-режим (fail-closed) и НЕ исполняет."""
    await init_db()
    cid, chat = uuid.uuid4().hex, 6203
    _reset(chat)
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "remove_campaign")
    counter = {"n": 0}
    fsm = FakeFSM()
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat, bot=FakeBot()))
    with twofa_on("2468"), patched(bm, "execute_confirmed", _fake_exec(counter)):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid), fsm)
        for _ in range(bm._TWOFA_MAX_ATTEMPTS):  # добираем до локаута
            await bm.on_twofa_code(FakeMessage("0000", chat_id=chat, bot=FakeBot()), fsm)
        assert bm._twofa_lock_remaining_s(chat) > 0
        # новый ✅ во время локаута → вход в 2FA закрыт, pending не заводится, op не исполнена
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid), fsm)
    assert counter["n"] == 0
    assert chat not in bm._TWOFA_PENDING  # в 2FA-режим не вошли
    assert (await store.get_confirmed(cid)).status == "pending"  # опасная op заблокирована
    _reset(chat)


async def test_correct_pin_resets_fail_counter():
    """Верный PIN снимает весь трекинг неудач/локаутов (чистый старт для следующих операций)."""
    await init_db()
    cid, chat = uuid.uuid4().hex, 6204
    _reset(chat)
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "remove_campaign")
    counter = {"n": 0}
    fsm = FakeFSM()
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat, bot=FakeBot()))
    with twofa_on("2468"), patched(bm, "execute_confirmed", _fake_exec(counter)):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid), fsm)
        await bm.on_twofa_code(FakeMessage("0000", chat_id=chat, bot=FakeBot()), fsm)  # fail 1
        assert bm._TWOFA_FAILS[chat]["fails"] == 1
        await bm.on_twofa_code(FakeMessage("2468", chat_id=chat, bot=FakeBot()), fsm)  # верный
    assert counter["n"] == 1
    assert chat not in bm._TWOFA_FAILS  # трекинг снят
    _reset(chat)
