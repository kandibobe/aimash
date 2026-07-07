"""D2 (удобство 2026-07): «↩️ Откатить» применённую обратимую операцию.

Откат НЕ исполняется сам — минтит ОБРАТНЫЙ черновик за тем же confirm-гейтом (proposal +
confirmation_id + user_initiated). Тесты: сборка реверса из снимка _before; одноразовость;
что клик лишь минтит pending (ничего не мутирует).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402


# ── _reverse_spec: детерминированная обратная операция из _before ─────────────────
def test_reverse_budget_sets_to_previous():
    before = {"kind": "budget", "before_micros": 40_000_000, "after_micros": 48_000_000}
    op, params = bm._reverse_spec("update_budget", {"campaign": "Летняя"}, before)
    assert op == "update_budget"
    assert params == {"campaign": "Летняя", "mode": "set_to", "value": 40.0}


def test_reverse_bid_only_when_uniform():
    # одинаковые ставки групп → set_to прежнюю
    uni = {"kind": "bid", "before_micros": [500_000, 500_000], "after_micros": [600_000, 600_000]}
    op, params = bm._reverse_spec("update_bid", {"campaign": "К"}, uni)
    assert op == "update_bid" and params["mode"] == "set_to" and params["value"] == 0.5
    # разные ставки групп → откат одним set_to невозможен → None (честно не предлагаем)
    mixed = {"kind": "bid", "before_micros": [500_000, 700_000], "after_micros": [600_000, 600_000]}
    assert bm._reverse_spec("update_bid", {"campaign": "К"}, mixed) is None


def test_reverse_status_opposite_operation():
    # была ENABLED → поставили на паузу → откат = resume
    assert bm._reverse_spec(
        "pause_campaign", {"campaign": "К"}, {"kind": "status", "before_status": "ENABLED"}
    ) == ("resume_campaign", {"campaign": "К"})
    # была PAUSED → возобновили → откат = pause
    assert bm._reverse_spec(
        "resume_campaign", {"campaign": "К"}, {"kind": "status", "before_status": "PAUSED"}
    ) == ("pause_campaign", {"campaign": "К"})


def test_reverse_ad_status_needs_group_and_ad():
    ok = bm._reverse_spec(
        "pause_ad",
        {"campaign": "К", "ad_group": "Г", "ad": "123"},
        {"kind": "status", "before_status": "ENABLED"},
    )
    assert ok == ("resume_ad", {"campaign": "К", "ad_group": "Г", "ad": "123"})
    # нет ad_group → None (не собираем неполную операцию)
    assert (
        bm._reverse_spec(
            "pause_ad", {"campaign": "К", "ad": "1"}, {"kind": "status", "before_status": "ENABLED"}
        )
        is None
    )


def test_reverse_rename_back():
    op, params = bm._reverse_spec(
        "update_campaign",
        {"campaign": "Старое", "new_name": "Новое"},
        {"kind": "name", "before_name": "Старое"},
    )
    # текущее имя кампании = Новое → переименовать обратно в Старое
    assert op == "update_campaign" and params == {"campaign": "Новое", "new_name": "Старое"}


def test_reverse_network_restores_previous_flag():
    op, params = bm._reverse_spec(
        "set_campaign_network",
        {"campaign": "К", "search_partners": True},
        {"kind": "network", "before_search_partners": False, "after_search_partners": True},
    )
    assert op == "set_campaign_network" and params == {"campaign": "К", "search_partners": False}


def test_reverse_none_without_before_or_campaign():
    assert bm._reverse_spec("update_budget", {"campaign": "К"}, None) is None
    assert bm._reverse_spec("update_budget", {}, {"kind": "budget", "before_micros": 1}) is None
    assert bm._reverse_spec("create_search_campaign", {"campaign": "К"}, {"kind": "x"}) is None


# ── handler on_rollback: минтит ОБРАТНЫЙ черновик (mint-only), одноразово ─────────
class _Msg:
    def __init__(self, chat_id):
        self.chat = SimpleNamespace(id=chat_id)
        self.sent: list = []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))


class _Cq:
    def __init__(self, chat_id):
        self.message = _Msg(chat_id)
        self.from_user = SimpleNamespace(id=chat_id, username="op")
        self.answered = []

    async def answer(self, *a, **kw):
        self.answered.append((a, kw))


async def test_rollback_click_mints_reverse_proposal_only(monkeypatch):
    from bot.handlers.confirm_flow import on_rollback

    chat_id = 7007
    presented: dict = {}

    async def fake_present(msg, **kw):
        presented.update(kw)

    monkeypatch.setattr(bm, "_present_proposal", fake_present)
    bm._ROLLBACK_CACHE[chat_id] = {
        "token": "tok1",
        "operation": "update_budget",
        "params": {"campaign": "Летняя", "mode": "set_to", "value": 40.0},
        "customer_id": bm.DRAFT_ACCOUNT_ID,
    }
    cq = _Cq(chat_id)
    await on_rollback(cq, SimpleNamespace(token="tok1"))
    assert presented.get("operation") == "update_budget"
    assert presented["params"]["mode"] == "set_to" and presented["params"]["value"] == 40.0
    assert presented["customer_id"] == bm.DRAFT_ACCOUNT_ID
    # одноразово: кэш снят
    assert chat_id not in bm._ROLLBACK_CACHE


async def test_rollback_stale_token(monkeypatch):
    from bot.handlers.confirm_flow import on_rollback

    chat_id = 7008
    called = {"present": 0}

    async def fake_present(*a, **k):
        called["present"] += 1

    monkeypatch.setattr(bm, "_present_proposal", fake_present)
    bm._ROLLBACK_CACHE[chat_id] = {"token": "current", "operation": "pause_campaign", "params": {}}
    cq = _Cq(chat_id)
    await on_rollback(cq, SimpleNamespace(token="OLD"))  # устаревший токен
    assert called["present"] == 0  # ничего не минтили
    assert any(kw.get("show_alert") for _, kw in cq.answered)  # показали stale-alert


def test_rollbackable_ops_are_diffable():
    """Класс-гард: каждая откатываемая операция снимает _before (иначе реверс собрать не из чего)."""
    from ads.service import _DIFFABLE_OPS

    assert bm._ROLLBACKABLE_OPS <= _DIFFABLE_OPS
