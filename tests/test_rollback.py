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


def test_reverse_keyword_bid_only_when_keyword_had_its_own_bid():
    """Ревизия волны (ДЕНЬГИ): у ключа могло не быть СВОЕЙ ставки — он наследовал ставку группы,
    и «было» в снимке = ставка группы. Откат через set_to завёл бы критерию собственную ставку:
    числом то же, состоянием другое (группа больше не управляет ключом) — это не откат, а тихая
    смена модели наследования. Предлагаем «↩️» только когда КАЖДЫЙ ключ имел свою ставку."""
    base = {"campaign": "К", "keyword": "ремонт"}
    own = {"kind": "keyword_bid", "before_micros": [500_000, 500_000], "own_bid": [True, True]}
    op, params = bm._reverse_spec("update_keyword_bid", base, own)
    assert op == "update_keyword_bid" and params["mode"] == "set_to" and params["value"] == 0.5

    # хоть один ключ наследовал ставку группы → отката не предлагаем
    inherited = {**own, "own_bid": [True, False]}
    assert bm._reverse_spec("update_keyword_bid", base, inherited) is None
    # старый снимок без флага (до ревизии) → тоже не предлагаем: молча не гадаем про чужие деньги
    legacy = {"kind": "keyword_bid", "before_micros": [500_000, 500_000]}
    assert bm._reverse_spec("update_keyword_bid", base, legacy) is None
    # флаг есть, но короче списка ставок → снимок бит, отказ (fail-closed)
    torn = {**own, "own_bid": [True]}
    assert bm._reverse_spec("update_keyword_bid", base, torn) is None


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


# ── Доп.2B: on_journal_rollback — ПЕРСИСТЕНТНЫЙ откат из /journal (снимок из БД) ─────
def _applied_snap(
    *, chat_id, operation="update_budget", params=None, customer_id=None, status="applied"
):
    """Снимок черновика как из ConfirmStore.get_confirmed (для монки-патча STORE)."""
    return SimpleNamespace(
        operation=operation,
        status=status,
        user_initiated=True,
        params=params if params is not None else {"campaign": "Летняя"},
        customer_id=customer_id if customer_id is not None else bm.DRAFT_ACCOUNT_ID,
        chat_id=chat_id,
    )


async def test_journal_rollback_mints_reverse_from_db(monkeypatch):
    """Клик «↩️» в /journal: снимок берём из БД по cid, реверс из params[_before], минтим
    ОБРАТНЫЙ черновик через _present_proposal (не исполняем сразу). Персистентно — не in-memory."""
    from bot.handlers.confirm_flow import on_journal_rollback

    chat_id = 8001
    presented: dict = {}

    async def fake_present(msg, **kw):
        presented.update(kw)

    snap = _applied_snap(
        chat_id=chat_id,
        params={"campaign": "Летняя", "_before": {"kind": "budget", "before_micros": 40_000_000}},
    )

    async def fake_get_confirmed(cid):
        return snap

    monkeypatch.setattr(bm, "_present_proposal", fake_present)
    monkeypatch.setattr(bm.STORE, "get_confirmed", fake_get_confirmed)
    cq = _Cq(chat_id)
    await on_journal_rollback(cq, SimpleNamespace(cid="abc123"))
    assert presented.get("operation") == "update_budget"
    assert presented["params"]["mode"] == "set_to" and presented["params"]["value"] == 40.0
    assert presented["customer_id"] == bm.DRAFT_ACCOUNT_ID


async def test_journal_rollback_foreign_chat_is_stale(monkeypatch):
    """Владение: cid чужого чата → generic-«устарело», НИЧЕГО не минтим (fail-closed)."""
    from bot.handlers.confirm_flow import on_journal_rollback

    called = {"present": 0}

    async def fake_present(*a, **k):
        called["present"] += 1

    snap = _applied_snap(chat_id=9999)  # владелец — другой чат

    monkeypatch.setattr(bm, "_present_proposal", fake_present)
    monkeypatch.setattr(bm.STORE, "get_confirmed", lambda cid: _acoro(snap))
    cq = _Cq(8002)  # кликает НЕ владелец
    await on_journal_rollback(cq, SimpleNamespace(cid="abc"))
    assert called["present"] == 0
    assert any(kw.get("show_alert") for _, kw in cq.answered)


async def test_journal_rollback_not_applied_is_stale(monkeypatch):
    """Откатываем ТОЛЬКО applied: статус confirmed/executing → generic-«устарело», без минта."""
    from bot.handlers.confirm_flow import on_journal_rollback

    called = {"present": 0}

    async def fake_present(*a, **k):
        called["present"] += 1

    snap = _applied_snap(chat_id=8003, status="confirmed")

    monkeypatch.setattr(bm, "_present_proposal", fake_present)
    monkeypatch.setattr(bm.STORE, "get_confirmed", lambda cid: _acoro(snap))
    cq = _Cq(8003)
    await on_journal_rollback(cq, SimpleNamespace(cid="abc"))
    assert called["present"] == 0
    assert any(kw.get("show_alert") for _, kw in cq.answered)


async def test_journal_rollback_missing_before_not_reversible(monkeypatch):
    """Снимка _before не хватает (реверс=None) → внятный «необратимо», без минта (не мёртвая кнопка)."""
    from bot.handlers.confirm_flow import on_journal_rollback

    called = {"present": 0}

    async def fake_present(*a, **k):
        called["present"] += 1

    snap = _applied_snap(chat_id=8004, params={"campaign": "Летняя"})  # без _before

    monkeypatch.setattr(bm, "_present_proposal", fake_present)
    monkeypatch.setattr(bm.STORE, "get_confirmed", lambda cid: _acoro(snap))
    cq = _Cq(8004)
    await on_journal_rollback(cq, SimpleNamespace(cid="abc"))
    assert called["present"] == 0
    assert any(kw.get("show_alert") for _, kw in cq.answered)


async def test_journal_rollback_unknown_cid_is_stale(monkeypatch):
    """cid не найден в БД (снимок None) → generic-«устарело», без минта."""
    from bot.handlers.confirm_flow import on_journal_rollback

    called = {"present": 0}

    async def fake_present(*a, **k):
        called["present"] += 1

    monkeypatch.setattr(bm, "_present_proposal", fake_present)
    monkeypatch.setattr(bm.STORE, "get_confirmed", lambda cid: _acoro(None))
    cq = _Cq(8005)
    await on_journal_rollback(cq, SimpleNamespace(cid="missing"))
    assert called["present"] == 0
    assert any(kw.get("show_alert") for _, kw in cq.answered)


def _acoro(value):
    """Обернуть значение в awaitable (для монки-патча async-методов лямбдой)."""

    async def _inner(*a, **k):
        return value

    return _inner()


# ── Доп.2B: пакетный load_proposals (без N+1 в /journal) ────────────────────────────
async def test_load_proposals_batch_roundtrip(monkeypatch):
    import uuid

    from confirm.store import ConfirmStore
    from core.config import settings
    from db.session import init_db

    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", bm.DRAFT_ACCOUNT_ID)
    store = ConfirmStore()
    cid1, cid2 = uuid.uuid4().hex, uuid.uuid4().hex
    for cid, camp in ((cid1, "A"), (cid2, "B")):
        await store.save_proposal(
            confirmation_id=cid,
            operation="update_budget",
            customer_id=bm.DRAFT_ACCOUNT_ID,
            params={"campaign": camp},
            summary="s",
            chat_id=101,
            user_initiated=True,
        )
    got = await store.load_proposals([cid1, cid2, "does-not-exist"])
    assert set(got) == {cid1, cid2}  # отсутствующий id просто не в словаре
    assert got[cid1].params["campaign"] == "A" and got[cid2].params["campaign"] == "B"
    assert await store.load_proposals([]) == {}  # пустой список → {}


async def test_send_journal_offers_rollback_only_for_own_reversible(monkeypatch):
    """Сквозной: /journal вешает «↩️ Откатить» ТОЛЬКО на свои applied+обратимые строки. Чужой чат
    и строки без снимка _before кнопки не получают (fail-closed + без мёртвых кнопок)."""
    import uuid

    from core.config import settings
    from db.session import init_db

    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", bm.DRAFT_ACCOUNT_ID)
    store = bm.STORE  # _send_journal читает именно этот инстанс
    chat_id = 4242

    async def _apply(cid, params, *, owner):
        await store.save_proposal(
            confirmation_id=cid,
            operation="update_budget",
            customer_id=bm.DRAFT_ACCOUNT_ID,
            params=params,
            summary="s",
            chat_id=owner,
            user_initiated=True,
        )
        assert await store.confirm(cid, chat_id=owner)
        assert await store.claim(cid, operation="update_budget") is not None
        await store.finalize(cid, result={"applied": True})

    reversible = uuid.uuid4().hex
    await _apply(
        reversible,
        {"campaign": "R", "_before": {"kind": "budget", "before_micros": 40_000_000}},
        owner=chat_id,
    )
    await _apply(uuid.uuid4().hex, {"campaign": "N"}, owner=chat_id)  # свой, но без _before
    await _apply(  # обратимый, но ЧУЖОЙ чат
        uuid.uuid4().hex,
        {"campaign": "F", "_before": {"kind": "budget", "before_micros": 10_000_000}},
        owner=7777,
    )

    sent: dict = {}

    class _M:
        chat = SimpleNamespace(id=chat_id)

        async def answer(self, text, **kw):
            sent["text"], sent["kw"] = text, kw

    await bm._send_journal(_M())
    kb = sent["kw"].get("reply_markup")
    assert kb is not None
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert len(buttons) == 1  # ровно одна: свой applied+обратимый
    assert reversible in buttons[0].callback_data  # JournalRollbackCB(cid=reversible) → "rbj:<cid>"
