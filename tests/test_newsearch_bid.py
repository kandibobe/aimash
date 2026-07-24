"""A7: /newsearch — max CPC-ставка видна на карточке, валютно-верный дефолт, кнопка правки.

Раньше ставка была скрытым валютно-слепым хардкодом 500_000 micros (0.5): на UGX/JPY абсурд,
и менеджер жал ✅ на значении, которого не видел. Теперь: дефолт от валюты аккаунта
(core.limits.wizard_default_money_units), строка CPC в сводке, «✏️ Изменить ставку» минтит новый
черновик. Фейки aiogram — локальные (как tests/test_twofa*.py); SDK/сеть не трогаем.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.handlers.search_media as sm  # noqa: E402
import bot.main as bm  # noqa: E402
from core import texts  # noqa: E402
from bot.callbacks import SearchBidCB  # noqa: E402
from core.limits import wizard_default_money_units  # noqa: E402
from db.session import init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ── показ CPC на карточке (сводка create_search_campaign) ──────────────────────────
def test_summary_shows_cpc_with_currency_ru():
    out = texts.fmt_search_proposal_summary(
        "Бренд",
        "https://x.io",
        10,
        ["h1", "h2", "h3"],
        ["d1", "d2"],
        ["kw"],
        "phrase",
        cpc_units=0.35,
        currency="USD",
    )
    assert "CPC" in out and "0.35" in out and "USD" in out  # ставка видна с валютой


def test_summary_shows_cpc_en():
    out = texts.fmt_search_proposal_summary(
        "Brand",
        "https://x.io",
        10,
        ["h1", "h2", "h3"],
        ["d1", "d2"],
        ["kw"],
        "phrase",
        lang="en",
        cpc_units=1.5,
        currency="AUD",
    )
    assert "CPC" in out and "1.5" in out and "AUD" in out


def test_summary_without_cpc_omits_line():
    # обратная совместимость: без cpc_units строки ставки нет (напр. /clone не передаёт)
    out = texts.fmt_search_proposal_summary(
        "X", "https://x.io", 10, ["h1", "h2", "h3"], ["d1", "d2"], ["kw"], "phrase"
    )
    assert "CPC" not in out


# ── валютно-верный дефолт ставки (не хардкод 0.5) ─────────────────────────────────
def test_default_cpc_is_currency_aware_not_hardcoded():
    _, cpc_usd = wizard_default_money_units("USD")
    _, cpc_jpy = wizard_default_money_units("JPY")
    assert cpc_usd == 0.5  # USD — исторический дефолт
    assert cpc_jpy != 0.5 and cpc_jpy > 1  # JPY — не абсурдные пол-йены (валютно-верно)


# ── функциональный: показ черновика с CPC + правка ставки минтит новый ────────────
class _FakeBot:
    async def send_chat_action(self, *a, **k):
        pass


class _FakeMessage:
    def __init__(self, text="", chat_id=100):
        self.text = text
        self.chat = type("C", (), {"id": chat_id})()
        self.bot = _FakeBot()
        self.answers: list = []

    async def answer(self, text="", **kw):
        self.answers.append(text)
        return self

    async def answer_document(self, *a, **k):
        return self


class _FakeCallbackQuery:
    def __init__(self, message, uid=100):
        self.message = message
        self.from_user = type("U", (), {"id": uid, "username": "op"})()
        self.answers: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


class _FakeFSM:
    def __init__(self):
        self._state = None

    async def set_state(self, s=None, *a, **k):
        self._state = s

    async def get_state(self):
        return self._state

    async def clear(self):
        self._state = None


async def _present(chat: int, cpc_micros: int):
    msg = _FakeMessage(chat_id=chat)
    # активный аккаунт → Draft; валюту читаем как USD, чтобы не трогать сеть
    with (
        patched(bm, "_active_read_account", _aval("7753643025")),
        patched(sm, "_account_currency_safe", _aval("USD")),
    ):
        await sm._present_search_proposal(
            msg,
            chat,
            name="Бренд",
            url="https://x.io",
            budget_units=10,
            headlines=["h1", "h2", "h3"],
            descriptions=["d1", "d2"],
            keywords=["kw"],
            cpc_micros=cpc_micros,
            currency="USD",
            customer_id="7753643025",
        )
    return msg


def _aval(v):
    async def _f(*a, **k):
        return v

    return _f


async def test_present_stores_edit_context_and_shows_cpc():
    await init_db()
    msg = await _present(7301, 500_000)
    # карточка показана и содержит ставку
    assert any("0.5" in a for a in msg.answers)
    ctx = sm._BID_EDIT.get(7301)
    assert ctx and ctx["cpc_micros"] == 500_000 and ctx["currency"] == "USD"


async def test_edit_bid_reprompts_and_new_bid_remints_with_updated_cpc():
    await init_db()
    await _present(7302, 500_000)
    token = sm._BID_EDIT[7302]["token"]
    fsm = _FakeFSM()

    # «✏️ Изменить ставку» → просит новое значение, ставит state
    cq = _FakeCallbackQuery(_FakeMessage(chat_id=7302))
    await sm.search_edit_bid(cq, SearchBidCB(token=token), fsm)
    assert await fsm.get_state() == bm.SearchWizard.awaiting_bid

    # новое значение 0.9 → пере-собрали черновик с cpc 900_000, state снят
    with (
        patched(bm, "_active_read_account", _aval("7753643025")),
        patched(sm, "_account_currency_safe", _aval("USD")),
    ):
        await sm.search_new_bid(_FakeMessage("0.9", chat_id=7302), fsm)
    assert await fsm.get_state() is None
    assert sm._BID_EDIT[7302]["cpc_micros"] == 900_000  # ставка обновлена


async def test_edit_bid_stale_token_rejected():
    await init_db()
    await _present(7303, 500_000)
    fsm = _FakeFSM()
    cq = _FakeCallbackQuery(_FakeMessage(chat_id=7303))
    await sm.search_edit_bid(cq, SearchBidCB(token="wrong-token"), fsm)
    assert cq.answers and cq.answers[-1][1] is True  # show_alert=True (stale)
    assert await fsm.get_state() is None  # в режим ввода не вошли


async def test_new_bad_bid_keeps_state_for_retry():
    await init_db()
    await _present(7304, 500_000)
    fsm = _FakeFSM()
    await fsm.set_state(bm.SearchWizard.awaiting_bid)
    await sm.search_new_bid(_FakeMessage("не число", chat_id=7304), fsm)
    # осталось в состоянии ввода (пользователь пришлёт число ещё раз)
    assert await fsm.get_state() == bm.SearchWizard.awaiting_bid
