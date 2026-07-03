"""2A: исполнение привязано к proposal.customer_id (латентная ловушка мультиаккаунта закрыта).

execute_confirmed берёт аккаунт ИЗ ЧЕРНОВИКА (штамп доверенного входа) и заново проходит
ensure_allowed на исполнении: чужой/пустой штамп → PermissionError ДО SDK и ДО claim (одноразовый
черновик не сжигается). Сегодня штамп всегда Draft → поведение прежнее; при будущем расширении
ALLOWED_CEILING достаточно правки потолка + штампа (см. ads/client.py:28).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
import ads.service as svc  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402


async def _mk_confirmed(store: ConfirmStore, *, customer_id: str, chat_id: int = 900) -> str:
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="pause_campaign",
        customer_id=customer_id,
        params={"campaign": "X"},
        summary="s",
        chat_id=chat_id,
        user_initiated=True,
    )
    assert await store.confirm(cid, chat_id=chat_id)
    return cid


@pytest.mark.asyncio
async def test_execute_confirmed_uses_proposal_customer_id(monkeypatch):
    """Исполняемый cid == proposal.customer_id (не хардкод): apply_* получает штамп черновика."""
    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", DRAFT_ACCOUNT_ID)
    store = ConfirmStore()
    cid = await _mk_confirmed(store, customer_id=DRAFT_ACCOUNT_ID)

    seen: dict = {}

    async def _fake_apply(**kw):
        seen.update(kw)
        return {"applied": True}

    class _Ref:
        id = "123"
        status = "ENABLED"
        name = "X"
        budget_micros = 1_000_000

    monkeypatch.setattr(mut, "apply_pause_campaign", _fake_apply)
    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", lambda c, cid_, name: _Ref())

    async def _fake_client(cid_=None):
        return object()

    monkeypatch.setattr(svc, "build_client_async", _fake_client)

    result = await svc.execute_confirmed(store, cid)
    assert result == {"applied": True}
    assert seen["customer_id"] == DRAFT_ACCOUNT_ID  # штамп черновика, не константа в обход


@pytest.mark.asyncio
async def test_execute_confirmed_foreign_customer_id_denied(monkeypatch):
    """Чужой штамп (вне ALLOWED_CEILING) → PermissionError; apply_* НЕ вызван; черновик остаётся
    confirmed (claim не потрачен отказом замка)."""
    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", DRAFT_ACCOUNT_ID)
    store = ConfirmStore()
    cid = await _mk_confirmed(store, customer_id="1234567890")  # НЕ Draft

    called = {"n": 0}

    async def _fake_apply(**kw):
        called["n"] += 1
        return {}

    monkeypatch.setattr(mut, "apply_pause_campaign", _fake_apply)

    with pytest.raises(PermissionError):
        await svc.execute_confirmed(store, cid)
    assert called["n"] == 0
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "confirmed"  # одноразовый claim цел


@pytest.mark.asyncio
async def test_execute_confirmed_empty_stamp_fail_closed(monkeypatch):
    """Пустой/битый штамп НЕ откатывается молча на Draft — fail-closed PermissionError."""
    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", DRAFT_ACCOUNT_ID)
    store = ConfirmStore()
    cid = await _mk_confirmed(store, customer_id="мусор-без-цифр"[:0] or "x")  # нормализуется в ''

    with pytest.raises(PermissionError):
        await svc.execute_confirmed(store, cid)


@pytest.mark.asyncio
async def test_read_before_defaults_to_draft(monkeypatch):
    """Регрессия: read_before без customer_id читает Draft (текущие вызовы не меняются)."""
    seen: dict = {}

    class _Ref:
        id = "1"
        status = "ENABLED"
        name = "X"
        budget_micros = 5_000_000

    def _fake_find(client, cid, name):
        seen["cid"] = cid
        return _Ref()

    async def _fake_client(cid=None):
        seen["client_cid"] = cid
        return object()

    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", _fake_find)
    monkeypatch.setattr(svc, "build_client_async", _fake_client)

    out = await svc.read_before("pause_campaign", {"campaign": "X"})
    assert out == {"kind": "status", "before_status": "ENABLED"}
    assert seen["cid"] == DRAFT_ACCOUNT_ID
    assert seen["client_cid"] == DRAFT_ACCOUNT_ID


@pytest.mark.asyncio
async def test_read_before_honors_customer_id(monkeypatch):
    """read_before(customer_id=…) читает переданный аккаунт (мультиаккаунт-готовность)."""
    seen: dict = {}

    class _Ref:
        id = "1"
        status = "PAUSED"
        name = "X"
        budget_micros = 5_000_000

    monkeypatch.setattr(
        svc.resolve, "find_campaign_by_name", lambda c, cid, name: seen.update(cid=cid) or _Ref()
    )

    async def _fake_client(cid=None):
        return object()

    monkeypatch.setattr(svc, "build_client_async", _fake_client)

    out = await svc.read_before("pause_campaign", {"campaign": "X"}, customer_id="676-404-0266")
    assert out == {"kind": "status", "before_status": "PAUSED"}
    assert seen["cid"] == "6764040266"  # нормализовано
