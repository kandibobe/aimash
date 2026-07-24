"""Гард ВЛАДЕНИЯ confirm-гейта (мультиоператор §12/§Q4): подтвердить/отклонить черновик может
ТОЛЬКО его владелец (chat_id == proposal.chat_id). Чужой chat_id (утёкший confirmation_id) не
проходит — fail-closed, неотличимо от устаревшего. Реальный ConfirmStore на temp SQLite (conftest)."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from confirm.store import ConfirmStore  # noqa: E402
from db.session import init_db  # noqa: E402

DRAFT = "7753643025"
OWNER = 100
ATTACKER = 999


async def _mk(store: ConfirmStore, *, chat_id: int = OWNER) -> str:
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT,
        params={"campaign": "X"},
        summary="s",
        chat_id=chat_id,
        user_initiated=True,
    )
    return cid


async def test_confirm_rejects_foreign_chat_id():
    """Чужой chat_id НЕ подтверждает черновик владельца (rowcount=0 → False), статус остаётся pending."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)

    assert await store.confirm(cid, chat_id=ATTACKER) is False  # чужой — отказ
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "pending"  # не подтверждён

    assert await store.confirm(cid, chat_id=OWNER) is True  # владелец — ок
    snap = await store.get_confirmed(cid)
    assert snap.status == "confirmed"


async def test_reject_ignores_foreign_chat_id():
    """Чужой chat_id НЕ отклоняет черновик владельца; владелец отклоняет нормально."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)

    await store.reject(cid, chat_id=ATTACKER)  # чужой — no-op
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "pending"

    await store.reject(cid, chat_id=OWNER)  # владелец — отклонил
    snap = await store.get_confirmed(cid)
    assert snap.status == "rejected"
