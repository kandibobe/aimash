"""3A: кнопки главного меню работают ВО ВРЕМЯ визардов (menu_guard) без потери работы.

Исходный баг: state-хендлеры визардов глотали подпись кнопки как ввод, если хендлер кнопки жил
в позже импортируемом модуле («ℹ️ Клиенты» во время визарда §19 уходила в LLM как описание
кампании). Гард регистрируется первым, мягко сворачивает флоу (черновик §19 остаётся active,
буфер §20 сбрасывается в confirm-черновик) и выполняет кнопку.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot import keyboards as kb  # noqa: E402
from db.session import init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeBot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeMessage:
    def __init__(self, chat_id: int, text: str):
        self.chat = SimpleNamespace(id=chat_id)
        self.text = text
        self.bot = FakeBot()
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append(text)
        return self


class FakeState:
    def __init__(self, state=None, data=None):
        self._state = state
        self._data = dict(data or {})

    async def get_state(self):
        return self._state

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, s):
        self._state = s

    async def clear(self):
        self._state = None
        self._data = {}


def test_guard_registered_before_state_handlers():
    """Инвариант порядка: btn_guard_menu — раньше state-хендлеров визардов (иначе кнопку съест
    ввод), on_text — последний (существующий инвариант не сломан)."""
    names = [h.callback.__name__ for h in bm.dp.message.handlers]
    assert "btn_guard_menu" in names
    gpos = names.index("btn_guard_menu")
    for state_handler in ("cc_settings_desc", "cli_accumulate_text", "gdn_brief"):
        if state_handler in names:
            assert gpos < names.index(state_handler), (
                f"btn_guard_menu должен стоять РАНЬШЕ {state_handler}"
            )
    assert names[-1] == "on_text"


async def test_button_interrupts_cc_wizard_preserves_draft(monkeypatch):
    """Кнопка «📊 Статистика» во время визарда §19: entry вызван, state очищен, черновик ЖИВ
    (active, возврат через «▶️ Продолжить»), пользователю — подсказка с шагом."""
    await init_db()
    chat_id = 91_001
    sid = await bm.CDRAFTS.create(chat_id=chat_id, customer_id=bm.DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 3, expected_chat_id=chat_id)

    called = {"n": 0}

    async def _fake_status(m):
        called["n"] += 1

    m = FakeMessage(chat_id, next(iter(kb.BTN_STATUS_ALL)))
    state = FakeState(state="CreateCampaignWizard:settings_desc", data={"cc_session": sid})
    with patched(bm, "_send_status", _fake_status):
        await bm.btn_guard_menu(m, state)

    assert called["n"] == 1  # кнопка сработала (раньше «съедалась» как ввод)
    assert await state.get_state() is None
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat_id)
    assert snap is not None and snap.status == "active"  # работа НЕ потеряна
    assert any("шаг 3/7" in a or "step 3/7" in a for a in m.answers)  # подсказка о возврате


async def test_btn_clients_during_wizard_dispatches(monkeypatch):
    """Исходный баг: «ℹ️ Клиенты» во время визарда уходила в LLM как описание кампании.
    Теперь — вызывается entry клиентов."""
    await init_db()
    called = {"n": 0}

    async def _fake_clients(m):
        called["n"] += 1

    m = FakeMessage(91_002, next(iter(kb.BTN_CLIENTS_ALL)))
    state = FakeState(state="CreateCampaignWizard:settings_desc", data={})
    with patched(bm, "_cli_present_accounts", _fake_clients):
        await bm.btn_guard_menu(m, state)
    assert called["n"] == 1


async def test_client_buffer_flushed_to_proposal(monkeypatch):
    """Буфер §20 при нажатии кнопки НЕ теряется: сбрасывается через _cli_extract_and_propose."""
    await init_db()
    chat_id = 91_003
    bm._CLI_TEXT_BUF[chat_id] = ["Kasi Motors — автодилер"]

    flushed = {}

    async def _fake_extract(bot, cid, cust, buf):
        flushed.update(cid=cid, cust=cust, buf=list(buf))
        return True

    called = {"n": 0}

    async def _fake_status(m):
        called["n"] += 1

    m = FakeMessage(chat_id, next(iter(kb.BTN_STATUS_ALL)))
    state = FakeState(
        state="ClientInfoWizard:awaiting_text",
        data={"cli_customer_id": bm.DRAFT_ACCOUNT_ID},
    )
    with (
        patched(bm, "_cli_extract_and_propose", _fake_extract),
        patched(bm, "_send_status", _fake_status),
    ):
        await bm.btn_guard_menu(m, state)

    assert flushed["buf"] == ["Kasi Motors — автодилер"]  # буфер ушёл в черновик, не в мусор
    assert bm._CLI_TEXT_BUF.get(chat_id) is None
    assert called["n"] == 1
    assert any("черновик" in a.lower() or "draft" in a.lower() for a in m.answers)


async def test_en_captions_matched(monkeypatch):
    """EN-подпись кнопки тоже перехватывается (BTN_*_ALL — оба языка)."""
    await init_db()
    called = {"n": 0}

    async def _fake_status(m):
        called["n"] += 1

    m = FakeMessage(91_004, kb.BTN_STATUS["en"])
    state = FakeState(state="KwWizard:awaiting_seeds", data={})
    with patched(bm, "_send_status", _fake_status):
        await bm.btn_guard_menu(m, state)
    assert called["n"] == 1


async def test_no_state_peaceful_path(monkeypatch):
    """state=None (мирный путь): гард просто выполняет кнопку — suspend не нужен, подсказок нет."""
    await init_db()
    called = {"n": 0}

    async def _fake_status(m):
        called["n"] += 1

    m = FakeMessage(91_005, kb.BTN_STATUS["ru"])
    state = FakeState(state=None)
    with patched(bm, "_send_status", _fake_status):
        await bm.btn_guard_menu(m, state)
    assert called["n"] == 1
    assert m.answers == []  # никаких «черновик сохранён» на мирном пути
