"""§20 (Фаза B): confirm-гейт для memory-операций профиля + cross-domain инвариант.

- save/update/clear профиля проходят через ConfirmStore (pending→confirmed→claim→finalize) с audit;
- атомарный claim: второй execute того же confirmation_id — no-op (replay-защита);
- cross-domain: memory-op отвергается ads.service.execute_confirmed, ads-op — execute_confirmed_memory;
- TOCTOU re-check: execute_confirmed_memory перепроверяет read-замок и пер-пользовательский грант
  НА ИСПОЛНЕНИИ (доступ, отозванный между показом и ✅, блокирует запись; черновик не сжигается).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.service import execute_confirmed  # noqa: E402
from clients.execute import execute_confirmed_memory  # noqa: E402
from clients.store import ClientProfileStore  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.access import grant_account_access  # noqa: E402
from core.config import settings  # noqa: E402
from db.models import AuditLog  # noqa: E402
from db.session import Session, init_db  # noqa: E402


async def _make_confirmed(store, *, operation, customer_id, params, chat_id=500) -> str:
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=customer_id,
        params=params,
        summary="было→станет",
        chat_id=chat_id,
        user_initiated=True,
    )
    ok = await store.confirm(cid, chat_id=chat_id)
    assert ok is True
    return cid


async def _allow_memory_access(monkeypatch, cust: str, *, chat_id: int = 500) -> None:
    """Дать тесту доступ, который execute_confirmed_memory перепроверяет на исполнении:
    глобальный read-замок (env read-list) + пер-пользовательский грант (account_access)."""
    monkeypatch.setattr(settings, "google_ads_read_customer_ids", cust)
    await grant_account_access(chat_id, cust)


@pytest.mark.asyncio
async def test_profile_save_through_gate_writes_and_audits(monkeypatch):
    await init_db()
    cust = "2000000001"
    await _allow_memory_access(monkeypatch, cust)
    store = ConfirmStore()
    cid = await _make_confirmed(
        store,
        operation="profile_save",
        customer_id=cust,
        params={"customer_id": cust, "patch": {"brand": "Acme", "geo": "Kyiv"}, "source": "text"},
    )
    result = await execute_confirmed_memory(store, cid)
    assert result["created"] is True

    prof = await ClientProfileStore().get_by_account(cust)
    assert prof["brand"] == "Acme"

    async with Session() as s:
        applied = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.confirmation_id == cid, AuditLog.status == "applied")
            )
        ).scalar_one()
    assert applied == 1  # finalize записал audit applied


@pytest.mark.asyncio
async def test_replay_second_execute_is_noop(monkeypatch):
    await init_db()
    cust = "2000000002"
    await _allow_memory_access(monkeypatch, cust)
    store = ConfirmStore()
    cid = await _make_confirmed(
        store,
        operation="profile_save",
        customer_id=cust,
        params={"customer_id": cust, "patch": {"brand": "Once"}},
    )
    await execute_confirmed_memory(store, cid)
    with pytest.raises(PermissionError):  # claim второй раз → None → отказ (replay-защита)
        await execute_confirmed_memory(store, cid)


@pytest.mark.asyncio
async def test_clear_through_gate(monkeypatch):
    await init_db()
    cust = "2000000003"
    await _allow_memory_access(monkeypatch, cust)
    pstore = ClientProfileStore()
    await pstore.apply_upsert(cust, {"brand": "ToClear"}, operation="profile_save")
    store = ConfirmStore()
    cid = await _make_confirmed(
        store, operation="profile_clear", customer_id=cust, params={"customer_id": cust}
    )
    res = await execute_confirmed_memory(store, cid)
    assert res["cleared"] is True
    assert await pstore.get_by_account(cust) is None


@pytest.mark.asyncio
async def test_cross_domain_memory_op_rejected_by_ads_executor():
    await init_db()
    cust = "2000000004"
    store = ConfirmStore()
    cid = await _make_confirmed(
        store,
        operation="profile_save",
        customer_id=cust,
        params={"customer_id": cust, "patch": {"brand": "X"}},
    )
    # memory-op НЕ должна исполниться ads-путём (вне SUPPORTED_OPERATIONS, fail-closed)
    with pytest.raises(PermissionError):
        await execute_confirmed(store, cid)


@pytest.mark.asyncio
async def test_cross_domain_ads_op_rejected_by_memory_executor():
    await init_db()
    store = ConfirmStore()
    cid = await _make_confirmed(
        store,
        operation="update_budget",
        customer_id="7753643025",
        params={"campaign": "X", "mode": "set_to", "value": 10.0},
    )
    # ads-op НЕ должна исполниться memory-путём (вне MEMORY_OPERATIONS, fail-closed)
    with pytest.raises(PermissionError):
        await execute_confirmed_memory(store, cid)


# ── TOCTOU re-check доступа на исполнении (1F3): read-замок + пер-пользовательский грант ──
@pytest.mark.asyncio
async def test_memory_execute_rechecks_read_lock():
    """Пустой allow/read-list (conftest-дефолт = fail-closed) → PermissionError НА ИСПОЛНЕНИИ,
    причём ДО claim: черновик остаётся confirmed (не сожжён отказом доступа)."""
    await init_db()
    cust = "2000000005"
    store = ConfirmStore()
    cid = await _make_confirmed(
        store,
        operation="profile_save",
        customer_id=cust,
        params={"customer_id": cust, "patch": {"brand": "Blocked"}},
    )
    with pytest.raises(PermissionError):
        await execute_confirmed_memory(store, cid)
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "confirmed"  # claim не потрачен
    assert await ClientProfileStore().get_by_account(cust) is None  # запись не прошла


@pytest.mark.asyncio
async def test_memory_execute_rechecks_per_user_grant(monkeypatch):
    """Read-замок пройден (env read-list), но пер-пользовательского гранта нет (не-Draft) →
    PermissionError: грант, отозванный между показом и ✅, блокирует запись. Режим enforced —
    в auto пустая таблица дала бы legacy-проход (см. core.access)."""
    await init_db()
    cust = "2000000006"
    monkeypatch.setattr(settings, "account_access_mode", "enforced")
    monkeypatch.setattr(settings, "google_ads_read_customer_ids", cust)  # только read-замок
    store = ConfirmStore()
    cid = await _make_confirmed(
        store,
        operation="profile_save",
        customer_id=cust,
        params={"customer_id": cust, "patch": {"brand": "NoGrant"}},
    )
    with pytest.raises(PermissionError):
        await execute_confirmed_memory(store, cid)
    assert await ClientProfileStore().get_by_account(cust) is None


@pytest.mark.asyncio
async def test_memory_execute_draft_needs_only_read_lock(monkeypatch):
    """Draft доступен всем whitelisted без гранта (core.access) — memory-исполнение на Draft
    требует только глобального read-замка (allow-list)."""
    await init_db()
    draft = "7753643025"
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", draft)
    store = ConfirmStore()
    cid = await _make_confirmed(
        store,
        operation="profile_save",
        customer_id=draft,
        params={"customer_id": draft, "patch": {"brand": "DraftOK"}},
    )
    result = await execute_confirmed_memory(store, cid)
    assert result["created"] is True
    # Уборка: Draft-профиль общий для всей тест-сессии (temp SQLite) — не влияем на другие тесты
    # (test_client_wizard ожидает profile_save, а не update, на чистом Draft).
    await ClientProfileStore().apply_clear(draft)
