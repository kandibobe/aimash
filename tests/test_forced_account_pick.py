"""§8: быстрые пути при НЕвыбранном аккаунте и нескольких живых показывают ПИКЕР, а не пустой Draft.

_require_read_account — единая развилка быстрых путей (/report N, /export, /sheets, /campaigns,
NL-статистика): пин Draft / один живой (авто-дефолт) / ноль живых → прежний одношаговый путь;
NULL-выбор + >1 живого → подсказка pick_live_account_first + flow-пикер, чтение НЕ выполняется.
Мутационный замок не затрагивается (тесты замка — tests/test_safety_core.py).
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402


class _FakeMsg:
    def __init__(self, chat_id: int = 778001):
        self.answers: list = []
        self.chat = SimpleNamespace(id=chat_id)

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))


async def test_require_returns_active_nondraft_without_picker(monkeypatch):
    async def _fake_active(_chat):
        return "1112223334"

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    msg = _FakeMsg()
    assert await bm._require_read_account(msg, "report") == "1112223334"
    assert msg.answers == []  # одношаговый путь, никакого пикера


async def test_require_draft_not_pending_returns_draft(monkeypatch):
    """Пин Draft/ноль живых (pending=False) → прежнее поведение: Draft без пикера."""
    import core.access as ca

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_pending(_chat):
        return False

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    msg = _FakeMsg()
    assert await bm._require_read_account(msg, "report") == bm.DRAFT_ACCOUNT_ID
    assert msg.answers == []


async def test_require_pending_shows_report_picker_and_stops(monkeypatch):
    import core.access as ca

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_pending(_chat):
        return True

    started: list = []

    async def _fake_picker(m, target):
        started.append(target)

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    monkeypatch.setattr(bm, "_start_report_picker", _fake_picker)
    msg = _FakeMsg()
    assert await bm._require_read_account(msg, "report") is None  # чтение НЕ выполняется
    assert started == ["report"]
    assert msg.answers, "должна уйти подсказка pick_live_account_first"


async def test_require_pending_campaigns_flow_uses_campaigns_picker(monkeypatch):
    import core.access as ca

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_pending(_chat):
        return True

    started: list = []

    async def _fake_camp_picker(m):
        started.append("campaigns")

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    monkeypatch.setattr(bm, "_start_campaigns_picker", _fake_camp_picker)
    msg = _FakeMsg()
    assert await bm._require_read_account(msg, "campaigns") is None
    assert started == ["campaigns"]


async def test_send_campaigns_stops_on_pending_picker(monkeypatch):
    """/campaigns при pending: список НЕ читается (нет SDK-вызова), якорь не трогается."""
    import core.access as ca

    chat = 778002

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_pending(_chat):
        return True

    async def _fake_camp_picker(m):
        pass

    async def _boom(*a, **kw):  # чтение кампаний не должно случиться
        raise AssertionError("list_campaigns не должен вызываться при pending-пикере")

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    monkeypatch.setattr(bm, "_start_campaigns_picker", _fake_camp_picker)
    monkeypatch.setattr(bm, "run_ads_read_call", _boom)
    bm._CAMP_ACCT.pop(chat, None)
    msg = _FakeMsg(chat)
    await bm._send_campaigns(msg, chat)
    assert chat not in bm._CAMP_ACCT  # якорь не выставлен — чтения не было


async def test_dispatch_need_account_shows_status_picker(monkeypatch):
    """Ветка need_account из agent-loop: одно сообщение с подсказкой + клавиатурой (target=status)."""
    rows = [
        SimpleNamespace(id=bm.DRAFT_ACCOUNT_ID, name="Draft", currency="", status="ENABLED"),
        SimpleNamespace(id="1112223334", name="Башня", currency="UAH", status="ENABLED"),
        SimpleNamespace(id="9998887776", name="DARIAL", currency="USD", status="ENABLED"),
    ]

    async def _fake_rows(_chat):
        return rows

    async def _fake_last(_chat):
        return None

    monkeypatch.setattr(bm, "_read_account_rows", _fake_rows)
    monkeypatch.setattr(bm, "_last_account", _fake_last)
    msg = _FakeMsg()
    await bm._dispatch_command_result(msg, {"type": "need_account"}, None)
    assert len(msg.answers) == 1
    text, kw = msg.answers[0]
    assert kw.get("reply_markup") is not None  # пикер приложен к самой подсказке
    assert "status" in str(kw["reply_markup"])  # target пикера — статистика после тапа


async def test_do_read_returns_need_account_when_pending(monkeypatch):
    """NL get_stats БЕЗ аккаунта при pending → {'type': 'need_account'} (бот-слой рисует пикер)."""
    import agent.loop as al
    import core.access as ca

    async def _pending(_chat):
        return True

    monkeypatch.setattr(ca, "account_choice_pending", _pending)
    res = await al._do_read("get_stats", {}, chat_id=1)
    assert res == {"type": "need_account"}


async def test_do_read_with_explicit_account_skips_pending(monkeypatch):
    """Аккаунт назван в запросе → pending НЕ проверяется, обычный резолв (тут — not found)."""
    import agent.loop as al
    import core.access as ca

    async def _pending(_chat):
        raise AssertionError("account_choice_pending не должен вызываться при явном аккаунте")

    async def _resolve(_chat, _arg):
        raise LookupError("нет такого")

    monkeypatch.setattr(ca, "account_choice_pending", _pending)
    monkeypatch.setattr(ca, "resolve_read_account", _resolve)
    res = await al._do_read("get_stats", {"account": "Башня"}, chat_id=1)
    assert res["type"] == "text"
