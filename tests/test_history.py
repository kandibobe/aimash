"""§2C: авто-память (db/history.py) + повтор (bot.on_recent_repeat). Реальный temp SQLite (conftest).

Проверяем: list_recent_applied отдаёт ТОЛЬКО applied, нужного чата, новые сверху, с фильтром по
operation и лимитом; last_applied; инертные ключи (_before) срезаны; повтор пере-собирает черновик
(pending, НЕ исполнен) через тот же confirm-гейт.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from bot.callbacks import RecentCB  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from db.history import last_applied, list_recent_applied  # noqa: E402
from db.models import Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402


async def _seed(chat_id: int, operation: str, params: dict, status: str) -> None:
    import uuid

    async with Session() as s:
        s.add(
            Proposal(
                confirmation_id=uuid.uuid4().hex,
                operation=operation,
                customer_id=DRAFT_ACCOUNT_ID,
                summary=f"{operation} summary",
                params=params,
                chat_id=chat_id,
                user_initiated=True,
                status=status,
            )
        )
        await s.commit()


# ── list_recent_applied ──────────────────────────────────────────────────────────
async def test_only_applied_right_chat_newest_first():
    await init_db()
    chat = 60_001
    await _seed(chat, "update_budget", {"campaign": "A", "mode": "set_to", "value": 10}, "applied")
    await _seed(chat, "pause_campaign", {"campaign": "B"}, "applied")
    await _seed(chat, "update_bid", {"campaign": "C"}, "failed")  # не applied
    await _seed(chat, "update_budget", {"campaign": "D"}, "rejected")  # не applied
    await _seed(60_002, "pause_campaign", {"campaign": "Z"}, "applied")  # другой чат

    rows = await list_recent_applied(chat, limit=10)
    assert [r.operation for r in rows] == [
        "pause_campaign",
        "update_budget",
    ]  # новые сверху (id desc)
    assert all(r.operation != "update_bid" for r in rows)  # failed исключён


async def test_operation_filter_and_limit():
    await init_db()
    chat = 60_003
    for i in range(4):
        await _seed(
            chat,
            "update_budget",
            {"campaign": f"C{i}", "mode": "set_to", "value": i + 1},
            "applied",
        )
    await _seed(chat, "pause_campaign", {"campaign": "P"}, "applied")

    only_budget = await list_recent_applied(chat, operation="update_budget", limit=10)
    assert len(only_budget) == 4 and all(r.operation == "update_budget" for r in only_budget)
    assert len(await list_recent_applied(chat, limit=2)) == 2  # лимит


async def test_inert_keys_stripped():
    await init_db()
    chat = 60_004
    await _seed(
        chat,
        "set_geo_location",
        {"campaign": "A", "locations": ["Киев"], "country_code": "UA", "_before": {"kind": "x"}},
        "applied",
    )
    a = await last_applied(chat, "set_geo_location")
    assert a is not None
    assert "_before" not in a.params  # инертный снимок срезан
    assert a.params["locations"] == ["Киев"]


async def test_last_applied_none_when_absent():
    await init_db()
    assert await last_applied(60_005, "update_budget") is None


# ── повтор: пере-собирает черновик через гейт (pending, НЕ исполнен) ─────────────────
class _FakeMessage:
    def __init__(self, chat_id: int):
        self.chat = type("C", (), {"id": chat_id})()
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self


class _FakeCQ:
    def __init__(self, message):
        self.message = message
        self.from_user = type("U", (), {"id": 1})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


async def test_recent_repeat_builds_pending_proposal():
    await init_db()
    chat = 60_010
    bm._LAST_PENDING.pop(chat, None)
    # set_geo_location — non-diffable (read_before=None без клиента) → офлайн-безопасно
    await _seed(
        chat,
        "set_geo_location",
        {"campaign": "Search Spring", "locations": ["Киев", "Львов"]},
        "applied",
    )
    await bm._send_recent(_FakeMessage(chat), chat)  # наполнить _RECENT_CACHE
    assert bm._RECENT_CACHE.get(chat)

    msg = _FakeMessage(chat)
    await bm.on_recent_repeat(_FakeCQ(msg), RecentCB(action="repeat", idx=0))
    cid = bm._LAST_PENDING.get(chat)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "set_geo_location"
    assert snap.status == "pending"  # НЕ исполнено — ждёт ✅
    assert snap.params["locations"] == ["Киев", "Львов"]


async def test_recent_repeat_stale_index_no_proposal():
    await init_db()
    chat = 60_011
    bm._RECENT_CACHE.pop(chat, None)
    bm._LAST_PENDING.pop(chat, None)
    msg = _FakeMessage(chat)
    await bm.on_recent_repeat(_FakeCQ(msg), RecentCB(action="repeat", idx=0))
    assert bm._LAST_PENDING.get(chat) is None  # ничего не создано
