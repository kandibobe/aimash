"""AD.6: полный confirm-гейт держится на РЕАЛЬНОМ (не-Draft) аккаунте, когда мутации включены на
всё видимое (решение владельца 2026-07: сентинел `all`, Draft-only доктрина снята).

Гарантия владельца: «мутации никогда не происходят без спроса и подтверждения» — доказываем для
не-Draft аккаунта (Башня 5437782039 как обнаруженный дочерний), не только для Draft:
- прямая мутация БЕЗ валидного confirmation_id → PermissionError даже на включённом боевом аккаунте;
- полный путь proposal → confirm → execute_confirmed(мок SDK) исполняется на штампе черновика;
- execute_confirmed ЗАНОВО проходит ensure_allowed → выключение сентинела закрывает мутации даже
  для уже подтверждённого черновика;
- деньги (бюджет) на боевом всё равно требуют user_initiated;
- потолок видимости: аккаунт вне MCC немутируем даже при `all`.

Сентинельные ensure_allowed-инварианты — в tests/test_safety_core.py; тут — сквозной гейт.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.client as ac  # noqa: E402
import ads.mutations as mut  # noqa: E402
import ads.service as svc  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID, ensure_allowed  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402

CHILD = "5437782039"  # Башня — реальный дочерний под MCC клиента (обнаруживается обходом)


@contextmanager
def _allowed(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@contextmanager
def _visible_child(cid: str = CHILD):
    """Сделать cid ВИДИМЫМ боту (как обнаруженный обходом MCC дочерний) на время теста."""
    ac.set_discovered_read_children([cid])
    try:
        yield
    finally:
        ac.set_discovered_read_children([])  # не протекаем в другие тесты


# ── сентинел `all`: не-Draft видимый аккаунт становится мутабельным ────────────────────
def test_sentinel_all_makes_visible_child_mutable_but_not_foreign():
    with _allowed("all"), _visible_child():
        ensure_allowed(DRAFT_ACCOUNT_ID)  # Draft — всегда
        ensure_allowed(CHILD)  # видимый боевой — теперь тоже (prod-дефолт)
        with pytest.raises(PermissionError):
            ensure_allowed("1234567890")  # вне MCC/видимости — по-прежнему отказ (потолок)


# ── прямая мутация на включённом боевом БЕЗ подтверждения — отклонена ───────────────────
@pytest.mark.asyncio
async def test_direct_mutation_on_child_requires_confirmation(monkeypatch):
    """Даже когда мутации на боевом включены (`all`), прямой вызов apply_* с невалидным/пустым
    confirmation_id падает ДО SDK: гейт «нет мутации без подтверждения» держится на реальных деньгах."""
    await init_db()
    store = ConfirmStore()
    with _allowed("all"), _visible_child():
        with pytest.raises(PermissionError):  # ensure_allowed(CHILD) проходит → падает claim
            await mut.apply_pause_campaign(
                customer_id=CHILD,
                campaign_id="123",
                confirmation_id="bogus-never-confirmed",
                confirm_store=store,
                ads_client=object(),
            )


# ── полный путь proposal → confirm → execute на боевом аккаунте ─────────────────────────
async def _mk_confirmed(
    store: ConfirmStore,
    *,
    customer_id: str,
    user_initiated: bool = True,
    operation: str = "pause_campaign",
    params: dict | None = None,
    chat_id: int = 950,
) -> str:
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=customer_id,
        params=params or {"campaign": "X"},
        summary="s",
        chat_id=chat_id,
        user_initiated=user_initiated,
    )
    assert await store.confirm(cid, chat_id=chat_id)
    return cid


@pytest.mark.asyncio
async def test_full_gate_executes_on_confirmed_child_proposal(monkeypatch):
    """proposal(Башня) → confirm → execute_confirmed → apply_* получает штамп Башни (не Draft)."""
    await init_db()
    store = ConfirmStore()
    seen: dict = {}

    async def _fake_apply(**kw):
        seen.update(kw)
        return {"applied": True}

    class _Ref:
        id, status, name, budget_micros = "123", "ENABLED", "X", 1_000_000

    monkeypatch.setattr(mut, "apply_pause_campaign", _fake_apply)
    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", lambda c, cid_, name: _Ref())

    async def _fake_client(cid_=None):
        return object()

    monkeypatch.setattr(svc, "build_client_async", _fake_client)

    with _allowed("all"), _visible_child():
        cid = await _mk_confirmed(store, customer_id=CHILD)
        result = await svc.execute_confirmed(store, cid)
    assert result == {"applied": True}
    assert seen["customer_id"] == CHILD  # исполнено на боевом штампе черновика, не на Draft


@pytest.mark.asyncio
async def test_execute_rechecks_ensure_allowed_when_sentinel_off(monkeypatch):
    """Черновик на Башне ПОДТВЕРЖДЁН при `all`; затем сентинел снят (allowed=Draft) → execute_confirmed
    ЗАНОВО проходит ensure_allowed и ОТКАЗЫВАЕТ (claim не потрачен). Выключение доступа закрывает
    даже уже подтверждённые черновики."""
    await init_db()
    store = ConfirmStore()
    called = {"n": 0}

    async def _fake_apply(**kw):
        called["n"] += 1
        return {}

    monkeypatch.setattr(mut, "apply_pause_campaign", _fake_apply)

    with _visible_child():
        with _allowed("all"):
            cid = await _mk_confirmed(store, customer_id=CHILD)
        with _allowed(DRAFT_ACCOUNT_ID):  # сентинел снят → Башня больше не в наборе
            with pytest.raises(PermissionError):
                await svc.execute_confirmed(store, cid)
    assert called["n"] == 0
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "confirmed"  # одноразовый claim цел


@pytest.mark.asyncio
async def test_budget_on_child_still_requires_user_initiated(monkeypatch):
    """Деньги на боевом аккаунте: подтверждённый черновик БЕЗ user_initiated → apply_update_budget
    отклоняет (бюджет — только прямой командой пользователя, golden rule 3), даже при `all`."""
    await init_db()
    store = ConfirmStore()
    with _allowed("all"), _visible_child():
        cid = await _mk_confirmed(
            store,
            customer_id=CHILD,
            user_initiated=False,  # НЕ прямая команда
            operation="update_budget",
            params={"campaign_id": "123", "new_budget_micros": 5_000_000},
        )
        with pytest.raises(PermissionError):
            await mut.apply_update_budget(
                customer_id=CHILD,
                campaign_id="123",
                new_budget_micros=5_000_000,
                confirmation_id=cid,
                confirm_store=store,
                ads_client=object(),
            )
