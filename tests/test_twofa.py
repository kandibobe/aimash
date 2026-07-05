"""§12 2FA-гейт: PIN перед исполнением ОПАСНОЙ операции (opt-in, дефолт OFF, fail-closed).

Проверяем контракт _do_confirm × core.twofa × on_twofa_code:
- выключен ⇒ поведение как раньше (гейта нет);
- включён + опасная op + верный PIN ⇒ исполняется;
- включён + опасная op + неверный PIN ⇒ НЕ исполняется, черновик остаётся pending (повторить ✅);
- включён БЕЗ PIN ⇒ опасная op заблокирована (fail-closed, не fail-open);
- включён + НЕ-опасная op ⇒ исполняется без запроса кода.

Фейки aiogram — локальные (как tests/test_bot_integration.py), без сети/Telegram.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import SecretStr  # noqa: E402

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from bot.callbacks import ConfirmCB  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core import twofa  # noqa: E402
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
    """Включить 2FA с заданным PIN (или без PIN, если pin=''), вернуть исходные настройки."""
    with (
        patched(settings, "two_factor_enabled", enabled),
        patched(settings, "two_factor_pin", SecretStr(pin)),
    ):
        yield


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
        self.deleted = False

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text: str = "", **kw):
        self.edits.append((text, kw))
        return self

    async def delete(self):
        self.deleted = True


class FakeCallbackQuery:
    def __init__(self, message, data: str = "", uid: int = 100):
        self.message = message
        self.data = data
        self.from_user = type("U", (), {"id": uid, "username": "op"})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


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
    async def _exec(store, cid):  # контракт execute_confirmed: claim → finalize
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


# ── unit: core.twofa ──────────────────────────────────────────────────────────────
def test_twofa_disabled_never_required():
    with patched(settings, "two_factor_enabled", False):
        assert twofa.enabled() is False
        assert twofa.required_for("remove_campaign") is False
        assert twofa.is_ready() is False


def test_twofa_required_and_verify():
    with twofa_on("1357"):
        assert twofa.enabled() is True
        assert twofa.is_ready() is True
        assert twofa.required_for("remove_campaign") is True
        assert twofa.required_for("resume_campaign") is False  # не в two_factor_ops
        assert twofa.verify("1357") is True
        assert twofa.verify(" 1357 ") is True  # обрезаем пробелы
        assert twofa.verify("0000") is False
        assert twofa.verify("") is False


def test_twofa_enabled_without_pin_is_not_ready():
    with twofa_on("", enabled=True):  # включён, PIN пуст → fail-closed
        assert twofa.enabled() is True
        assert twofa.is_ready() is False  # блокирует опасные ops в гейте
        assert twofa.required_for("remove_campaign") is True  # гейт сработает и заблокирует
        assert twofa.verify("anything") is False


# ── интеграция: гейт в _do_confirm ────────────────────────────────────────────────
async def test_disabled_executes_without_code():
    await init_db()
    cid, chat = uuid.uuid4().hex, 5101
    bm._TWOFA_PENDING.pop(chat, None)
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "remove_campaign")
    counter = {"n": 0}
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat, bot=FakeBot()))
    with (
        patched(settings, "two_factor_enabled", False),
        patched(bm, "execute_confirmed", _fake_exec(counter)),
    ):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid), FakeFSM())
    assert counter["n"] == 1  # исполнено сразу (гейта нет)
    assert (await store.get_confirmed(cid)).status == "applied"
    assert chat not in bm._TWOFA_PENDING


async def test_enabled_dangerous_op_asks_code_then_executes_on_correct_pin():
    await init_db()
    cid, chat = uuid.uuid4().hex, 5102
    bm._TWOFA_PENDING.pop(chat, None)
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "remove_campaign")
    counter = {"n": 0}
    fsm = FakeFSM()
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat, bot=FakeBot()))
    with twofa_on("2468"), patched(bm, "execute_confirmed", _fake_exec(counter)):
        # 1) нажатие ✅ на опасной op → код запрошен, НЕ исполнено, черновик всё ещё pending
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid), fsm)
        assert counter["n"] == 0
        assert (await store.get_confirmed(cid)).status == "pending"
        assert bm._TWOFA_PENDING.get(chat, {}).get("cid") == cid
        assert await fsm.get_state() == bm.TwoFactor.awaiting_code
        # 2) неверный код → не исполнено, ожидание сохраняется
        await bm.on_twofa_code(FakeMessage("0000", chat_id=chat, bot=FakeBot()), fsm)
        assert counter["n"] == 0
        assert (await store.get_confirmed(cid)).status == "pending"
        assert chat in bm._TWOFA_PENDING
        # 3) верный код → исполнено, ожидание/состояние очищены
        await bm.on_twofa_code(FakeMessage("2468", chat_id=chat, bot=FakeBot()), fsm)
    assert counter["n"] == 1
    assert (await store.get_confirmed(cid)).status == "applied"
    assert chat not in bm._TWOFA_PENDING
    assert await fsm.get_state() is None


async def test_enabled_without_pin_blocks_dangerous_op():
    await init_db()
    cid, chat = uuid.uuid4().hex, 5103
    bm._TWOFA_PENDING.pop(chat, None)
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "remove_campaign")
    counter = {"n": 0}
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat, bot=FakeBot()))
    with twofa_on("", enabled=True), patched(bm, "execute_confirmed", _fake_exec(counter)):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid), FakeFSM())
    assert counter["n"] == 0  # fail-closed: без PIN опасная op заблокирована
    assert (await store.get_confirmed(cid)).status == "pending"  # черновик не сожжён
    assert chat not in bm._TWOFA_PENDING  # ожидание кода не заведено (нечего вводить)


async def test_enabled_safe_op_executes_without_code():
    await init_db()
    cid, chat = uuid.uuid4().hex, 5104
    bm._TWOFA_PENDING.pop(chat, None)
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "resume_campaign")  # НЕ в two_factor_ops
    counter = {"n": 0}
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat, bot=FakeBot()))
    with twofa_on("2468"), patched(bm, "execute_confirmed", _fake_exec(counter)):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid), FakeFSM())
    assert counter["n"] == 1  # безопасная op не требует кода
    assert (await store.get_confirmed(cid)).status == "applied"
    assert chat not in bm._TWOFA_PENDING


async def test_too_many_wrong_codes_aborts_but_keeps_draft():
    await init_db()
    cid, chat = uuid.uuid4().hex, 5105
    bm._TWOFA_PENDING.pop(chat, None)
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "remove_campaign")
    counter = {"n": 0}
    fsm = FakeFSM()
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat, bot=FakeBot()))
    with twofa_on("2468"), patched(bm, "execute_confirmed", _fake_exec(counter)):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid), fsm)
        for _ in range(bm._TWOFA_MAX_ATTEMPTS):
            await bm.on_twofa_code(FakeMessage("0000", chat_id=chat, bot=FakeBot()), fsm)
    assert counter["n"] == 0
    assert chat not in bm._TWOFA_PENDING  # ожидание сброшено
    assert await fsm.get_state() is None
    assert (await store.get_confirmed(cid)).status == "pending"  # черновик уцелел — повторить ✅
