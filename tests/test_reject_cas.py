"""B2: ConfirmStore.reject — атомарный CAS (pending→rejected), а не select-then-modify.

Гонка ❌/✅ (Telegram доставляет оба колбэка почти одновременно; на Postgres нет single-writer):
раньше reject мог прочитать status='pending' ДО коммита confirm и перезаписать уже confirmed/
executing черновик в 'rejected'. Теперь один UPDATE … WHERE status='pending' — гоночный переход
не совпадёт по WHERE. Реальный ConfirmStore на temp SQLite (conftest).
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from confirm.store import ConfirmStore  # noqa: E402
from db.models import AuditLog, Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402

DRAFT = "7753643025"
OWNER = 100


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


async def _status(cid: str) -> str:
    async with Session() as s:
        p = (await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))).scalar_one()
        return p.status


async def _reject_audit_count(cid: str) -> int:
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.confirmation_id == cid, AuditLog.status == "rejected"
                    )
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


async def test_reject_returns_true_on_pending_false_otherwise():
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)
    assert await store.reject(cid, chat_id=OWNER) is True
    assert await _status(cid) == "rejected"
    # повторный reject — уже не pending → False, без второй audit-строки
    assert await store.reject(cid, chat_id=OWNER) is False
    assert await _reject_audit_count(cid) == 1


async def test_reject_does_not_override_confirmed():
    """Ключевой B2: confirmed черновик reject НЕ перезаписывает (CAS проигрывает)."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)
    assert await store.confirm(cid, chat_id=OWNER) is True  # ✅ успел первым

    assert await store.reject(cid, chat_id=OWNER) is False  # ❌ опоздал
    assert await _status(cid) == "confirmed"  # применяемая мутация не помечена rejected
    assert await _reject_audit_count(cid) == 0  # без спурьёзной rejected-строки


async def test_concurrent_confirm_and_reject_exactly_one_wins():
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)
    conf, rej = await asyncio.gather(
        store.confirm(cid, chat_id=OWNER),
        store.reject(cid, chat_id=OWNER),
    )
    assert conf ^ rej  # ровно один True (XOR) — второй проиграл CAS
    final = await _status(cid)
    assert (final == "confirmed") if conf else (final == "rejected")


def test_all_status_transitions_are_cas():
    """Мета-гард B2: каждый метод ConfirmStore, меняющий статус, использует CAS
    (update(Proposal).where(Proposal.status ...)) — новый не-CAS переход валит тест."""
    transition_methods = [
        "confirm",
        "reject",
        "claim",
        "mark_needs_review",
        "mark_confirmed_failed",
    ]
    for name in transition_methods:
        src = inspect.getsource(getattr(ConfirmStore, name))
        assert "update(Proposal)" in src and "Proposal.status" in src, (
            f"ConfirmStore.{name} должен менять статус через CAS (update…where status), не "
            "select-then-modify (иначе гонка перезапишет чужой переход)"
        )
