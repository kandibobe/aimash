"""Офлайн-тесты write-слоя Фазы 1 (Блок A): новые мутации за двумя гейтами + capability-guard.

Закрывает дыру из аудита: «write-путь (resolve/service/store) не покрыт тестами».
Без живого Google Ads — SDK-исполнители (_*_via_sdk) подменяются monkeypatch'ем; БД — временный
SQLite (см. tests/conftest.py). Проверяем:
- каждый apply_* проходит ОБА гейта (замок аккаунта + confirm) и финализирует audit;
- ставки (деньги) — только user_initiated; чужой аккаунт/без подтверждения — отказ;
- длину ключевых слов считает КОД (golden rule #4) ДО вызова SDK;
- capability-guard: неподдержанную операцию (отложенный geo) отклоняем ДО кнопок и в execute_confirmed;
- store roundtrip: save → confirm → finalize пишет audit_log [confirmed]→[applied].
"""

from __future__ import annotations

import json
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

import ads.mutations as mut  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402


# ── Хелперы (зеркало test_safety_core, чтобы файл был самодостаточным) ───────────
@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@dataclass
class FakeProposal:
    operation: str
    status: str
    user_initiated: bool


class FakeStore:
    def __init__(self, proposal=None):
        self._p = proposal
        self.finalized = False
        self._claimed = False

    async def claim(self, confirmation_id, *, operation):
        # Зеркало ConfirmStore.claim: атомарно/одноразово, только confirmed + совпавшая операция.
        p = self._p
        if p is None or p.status != "confirmed" or p.operation != operation or self._claimed:
            return None
        self._claimed = True
        return p

    async def finalize(self, confirmation_id, *, result):
        self.finalized = True


class _FakeEnums:
    class CampaignStatusEnum:
        ENABLED = "ENABLED"
        PAUSED = "PAUSED"


class _FakeClient:
    enums = _FakeEnums()


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ── apply_update_bid: ставка = деньги (оба гейта + user_initiated) ───────────────
async def test_apply_update_bid_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, bids):
        called["args"] = (customer_id, campaign_id, list(bids))
        return {"customer_id": customer_id, "campaign_id": campaign_id, "applied": True}

    store = FakeStore(FakeProposal("update_bid", "confirmed", user_initiated=True))
    with patched(mut, "_apply_bid_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_update_bid(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="7",
            bids=[("42", 1_500_000)],
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["args"][0] == DRAFT_ACCOUNT_ID and called["args"][1] == "7"
    assert store.finalized is True


async def test_apply_update_bid_blocked_when_not_user_initiated():
    store = FakeStore(FakeProposal("update_bid", "confirmed", user_initiated=False))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_bid(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="7",
                bids=[("42", 1_500_000)],
                confirmation_id="x",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (ставка не по команде)")
        except PermissionError:
            pass
    assert store.finalized is False


async def test_apply_update_bid_rejects_foreign_account():
    store = FakeStore(FakeProposal("update_bid", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_bid(
                customer_id="1234567890",
                campaign_id="7",
                bids=[("42", 1_500_000)],
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (чужой аккаунт)")
        except PermissionError:
            pass
    assert store.finalized is False


async def test_apply_update_bid_rejected_without_confirmation():
    store = FakeStore(proposal=None)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_bid(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="7",
                bids=[("42", 1_500_000)],
                confirmation_id="bogus",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (нет confirmation)")
        except PermissionError:
            pass


# ── apply_add_keywords / negatives: длину считает КОД, оба гейта ─────────────────
async def test_apply_add_keywords_happy_path():
    called = {}

    def fake(client, customer_id, ad_group_ids, keywords, match_type):
        called.update(
            customer_id=customer_id,
            ad_group_ids=list(ad_group_ids),
            keywords=list(keywords),
            match_type=match_type,
        )
        return {"applied": True, "count": len(ad_group_ids) * len(keywords)}

    store = FakeStore(FakeProposal("add_keywords", "confirmed", user_initiated=True))
    with patched(mut, "_add_keywords_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_add_keywords(
            customer_id=DRAFT_ACCOUNT_ID,
            ad_group_ids=["1", "2"],
            keywords=["  купить цветы  ", "доставка"],
            match_type="phrase",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["match_type"] == "phrase"
    assert called["keywords"][0] == "купить цветы"  # код обрезал пробелы
    assert store.finalized is True


async def test_apply_add_keywords_validates_length_before_sdk():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("add_keywords", "confirmed", user_initiated=True))
    with patched(mut, "_add_keywords_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_add_keywords(
                customer_id=DRAFT_ACCOUNT_ID,
                ad_group_ids=["1"],
                keywords=["а" * 81],  # >80 символов (кириллица = 1) → код отклоняет
                match_type="broad",
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (>80 символов)")
        except ValueError:
            pass
    assert calls["n"] == 0  # SDK не вызван
    assert store.finalized is False  # audit не финализирован


async def test_apply_add_negative_keywords_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, keywords, match_type):
        called.update(campaign_id=campaign_id, keywords=list(keywords), match_type=match_type)
        return {"applied": True, "count": len(keywords)}

    store = FakeStore(FakeProposal("add_negative_keywords", "confirmed", user_initiated=True))
    with patched(mut, "_add_negative_keywords_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_add_negative_keywords(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            keywords=["бесплатно"],
            match_type="broad",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["campaign_id"] == "23"
    assert store.finalized is True


# ── apply_resume_campaign: реюз статус-исполнителя со статусом ENABLED ───────────
async def test_apply_resume_campaign_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, status):
        called.update(customer_id=customer_id, campaign_id=campaign_id, status=status)
        return {"applied": True, "status": status}

    store = FakeStore(FakeProposal("resume_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_set_campaign_status_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_resume_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert res["applied"] is True
    assert called["status"] == "ENABLED"  # resume → ENABLED
    assert store.finalized is True


# ── apply_set_geo_proximity (A-geo): оба гейта, address-driven, без геокодинга ───
async def test_apply_set_geo_proximity_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, radius_km, address):
        called.update(campaign_id=campaign_id, radius_km=radius_km, address=dict(address))
        return {"applied": True, "radius_km": radius_km}

    store = FakeStore(FakeProposal("set_geo_proximity", "confirmed", user_initiated=True))
    with patched(mut, "_set_geo_proximity_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_set_geo_proximity(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            radius_km=10.0,
            address={"city_name": "Киев", "country_code": "UA"},
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["radius_km"] == 10.0
    assert called["address"]["city_name"] == "Киев"  # структурный адрес дошёл до SDK
    assert store.finalized is True


async def test_apply_set_geo_proximity_rejects_zero_radius_before_sdk():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("set_geo_proximity", "confirmed", user_initiated=True))
    with patched(mut, "_set_geo_proximity_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_set_geo_proximity(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                radius_km=0,  # код отклоняет ДО claim
                address={"city_name": "Киев", "country_code": "UA"},
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (радиус 0)")
        except ValueError:
            pass
    assert calls["n"] == 0  # SDK не вызван
    assert store.finalized is False


async def test_apply_set_geo_proximity_rejects_foreign_account():
    store = FakeStore(FakeProposal("set_geo_proximity", "confirmed", user_initiated=True))
    with (
        patched(mut, "_set_geo_proximity_via_sdk", lambda *a, **k: {"applied": True}),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        try:
            await mut.apply_set_geo_proximity(
                customer_id="1234567890",  # чужой → замок отклоняет
                campaign_id="23",
                radius_km=5,
                address={"city_name": "Киев", "country_code": "UA"},
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (чужой аккаунт)")
        except PermissionError:
            pass


async def test_apply_set_geo_proximity_validates_address_before_claim():
    """Пустой city_name → ValueError ДО claim (golden rule #4): SDK не зван, черновик не съеден."""
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("set_geo_proximity", "confirmed", user_initiated=True))
    with patched(mut, "_set_geo_proximity_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_set_geo_proximity(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                radius_km=5,
                address={"city_name": "", "country_code": "UA"},  # пустой город
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (пустой city_name)")
        except ValueError:
            pass
    assert calls["n"] == 0  # SDK не вызван
    assert store.finalized is False  # черновик не финализирован


# ── Валидатор длины ключевых слов (golden rule #4: код, кириллица = 1) ───────────
def test_assert_keyword_ok_counts_cyrillic_as_one():
    assert mut._assert_keyword_ok("  цветы  ") == "цветы"
    assert mut._assert_keyword_ok("а" * 80) == "а" * 80  # ровно 80 — ок
    for bad in ["а" * 81, "   ", "слово " * 11]:
        try:
            mut._assert_keyword_ok(bad)
            raise AssertionError(f"должно было упасть: {bad!r}")
        except ValueError:
            pass


# ── Резолвер: escape и пересчёт micros (используется для bid) ────────────────────
def test_gaql_escape():
    from ads.resolve import _gaql_escape

    assert _gaql_escape("O'Brien") == "O\\'Brien"
    assert _gaql_escape("a\\b") == "a\\\\b"


def test_compute_new_micros_modes():
    from ads.resolve import compute_new_micros

    assert compute_new_micros(1_000_000, "set_to", 3) == 3_000_000
    assert compute_new_micros(1_000_000, "increase_by_percent", 20) == 1_200_000
    assert compute_new_micros(1_000_000, "increase_by_amount", 2) == 3_000_000


# ── Capability-guard на уровне agent.loop: отказ ДО показа кнопок ────────────────
class _FakeFunc:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeCall:
    def __init__(self, name, arguments):
        self.function = _FakeFunc(name, arguments)


class _FakeMsg:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls
        self.content = content


def _fake_chat(name, arguments):
    async def _chat(messages, role=None, tools=None):
        return _FakeMsg(tool_calls=[_FakeCall(name, json.dumps(arguments))])

    return _chat


async def test_set_geo_proximity_now_supported_as_proposal():
    """A-geo активирован: set_geo_proximity со структурным адресом → черновик с кнопками."""
    import agent.loop as L

    fake = _fake_chat(
        "set_geo_proximity",
        {"campaign": "X", "city_name": "Киев", "country_code": "UA", "radius_km": 5},
    )
    with patched(L, "chat", fake):
        res = await L.handle_command("таргет в радиусе 5 км от Киева", chat_id=1)
    assert res["type"] == "proposal"  # geo поддержан → НЕ отклоняется
    assert res["operation"] == "set_geo_proximity"


async def test_capability_guard_declines_unsupported_mutation(monkeypatch):
    """Capability-guard (механизм): объявленную в TOOLS, но НЕ в SUPPORTED_OPERATIONS мутацию
    агент отклоняет ДО кнопок. Симулируем «отложенную» операцию, временно убрав update_bid
    из SUPPORTED (loop импортирует SUPPORTED_OPERATIONS лениво → monkeypatch виден)."""
    import agent.loop as L
    import ads.service as svc

    monkeypatch.setattr(svc, "SUPPORTED_OPERATIONS", svc.SUPPORTED_OPERATIONS - {"update_bid"})
    fake = _fake_chat("update_bid", {"campaign": "X", "mode": "set_to", "value": 1.5})
    with patched(L, "chat", fake):
        res = await L.handle_command("ставка 1.5 в кампании X", chat_id=1)
    assert res["type"] == "text"  # НЕ proposal → кнопок не будет
    assert "не поддерживается" in res["text"]


async def test_capability_guard_allows_supported_bid_as_proposal():
    import agent.loop as L

    fake = _fake_chat("update_bid", {"campaign": "X", "mode": "set_to", "value": 1.5})
    with patched(L, "chat", fake):
        res = await L.handle_command("ставку до 1.5 в кампании X", chat_id=1)
    assert res["type"] == "proposal"
    assert res["operation"] == "update_bid"


# ── Capability-guard / defense-in-depth на уровне execute_confirmed ──────────────
async def test_execute_confirmed_rejects_unsupported_op():
    """Defense-in-depth: операцию вне SUPPORTED_OPERATIONS execute_confirmed отвергает даже при
    дыре в loop-гейте. set_bidding_strategy — реальная ещё не реализованная операция."""
    from ads.service import execute_confirmed

    cp = SimpleNamespace(
        operation="set_bidding_strategy",
        status="confirmed",
        params={"campaign": "X"},
    )

    class _S:
        async def get_confirmed(self, cid):
            return cp

    try:
        await execute_confirmed(_S(), "cid")
        raise AssertionError("ожидался PermissionError (операция не поддержана)")
    except PermissionError:
        pass


# ── Store roundtrip: save → confirm → finalize пишет audit [confirmed]→[applied] ─
async def test_store_roundtrip_writes_audit():
    from confirm.store import ConfirmStore
    from db.models import AuditLog
    from db.session import Session, init_db

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="add_keywords",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X", "keywords": ["цветы"], "match_type": "broad"},
        summary="add_keywords X: +1 ключ",
        chat_id=1,
        user_initiated=True,
    )
    assert (await store.get_confirmed(cid)).status == "pending"

    assert await store.confirm(cid, chat_id=1) is True
    assert (await store.get_confirmed(cid)).status == "confirmed"
    assert await store.confirm(cid, chat_id=1) is False  # одноразово

    # claim (как apply_* перед SDK): confirmed → executing, АТОМАРНО и ОДНОРАЗОВО.
    snap = await store.claim(cid, operation="add_keywords")
    assert snap is not None and snap.status == "executing"
    assert await store.claim(cid, operation="add_keywords") is None  # повтор заблокирован (replay)
    assert (await store.get_confirmed(cid)).status == "executing"

    await store.finalize(cid, result={"applied": True, "count": 1})
    assert (await store.get_confirmed(cid)).status == "applied"  # терминальный статус

    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.confirmation_id == cid).order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
    statuses = [r.status for r in rows]
    assert "confirmed" in statuses and "applied" in statuses
    assert all(r.customer_id == DRAFT_ACCOUNT_ID for r in rows)


# ── FIX 1: replay/double-spend заблокирован на реальном сторе (claim одноразов) ───
async def test_real_store_apply_is_single_use_replay_blocked():
    from confirm.store import ConfirmStore
    from db.session import init_db

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="resume_campaign",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary="resume X",
        chat_id=1,
        user_initiated=True,
    )
    assert await store.confirm(cid, chat_id=1) is True

    calls = {"n": 0}

    def fake(client, customer_id, campaign_id, status):
        calls["n"] += 1
        return {"applied": True, "status": getattr(status, "name", status)}

    with patched(mut, "_set_campaign_status_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res1 = await mut.apply_resume_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="5",
            confirmation_id=cid,
            confirm_store=store,
            ads_client=_FakeClient(),
        )
        assert res1["applied"] is True
        # Повтор с тем же confirmation_id — claim вернёт None → PermissionError, SDK НЕ вызван.
        try:
            await mut.apply_resume_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="5",
                confirmation_id=cid,
                confirm_store=store,
                ads_client=_FakeClient(),
            )
            raise AssertionError("повторное выполнение должно быть заблокировано (replay)")
        except PermissionError:
            pass
    assert calls["n"] == 1  # SDK-исполнитель вызван РОВНО один раз (нет double-spend)
    assert (await store.get_confirmed(cid)).status == "applied"  # терминальный статус


# ── record_failure: статус и audit согласованы; терминальный applied не понижается ─
async def test_record_failure_terminalizes_confirmed_but_not_applied():
    from confirm.store import ConfirmStore
    from db.session import init_db

    await init_db()
    store = ConfirmStore()

    # (1) ошибка ДО claim (резолв имени): confirmed → failed (статус совпал с audit).
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X", "mode": "set_to", "value": 10},
        summary="b",
        chat_id=1,
        user_initiated=True,
    )
    await store.confirm(cid, chat_id=1)
    await store.record_failure(cid, error="resolve failed")
    assert (await store.get_confirmed(cid)).status == "failed"

    # (2) уже применённый (applied) НЕ понижается поздней записью ошибки.
    cid2 = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid2,
        operation="resume_campaign",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary="r",
        chat_id=1,
        user_initiated=True,
    )
    await store.confirm(cid2, chat_id=1)
    await store.claim(cid2, operation="resume_campaign")
    await store.finalize(cid2, result={"applied": True})
    assert (await store.get_confirmed(cid2)).status == "applied"
    await store.record_failure(cid2, error="late error")
    assert (await store.get_confirmed(cid2)).status == "applied"  # терминальный не понижен


# ── FIX 1: confirmation_id одной операции нельзя «переиграть» в другую (wrong-op) ─
async def test_apply_rejects_wrong_operation_confirmation():
    store = FakeStore(FakeProposal("add_keywords", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_bid(  # confirmation_id подтверждён для add_keywords, не bid
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="7",
                bids=[("42", 1_500_000)],
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (операция не совпадает)")
        except PermissionError:
            pass
    assert store.finalized is False


# ── FIX 2: user_initiated по умолчанию False (fail-closed), деньги — заблокированы ─
async def test_save_proposal_defaults_user_initiated_false():
    from confirm.store import ConfirmStore
    from db.session import init_db

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(  # БЕЗ user_initiated — должен лечь False
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X", "mode": "set_to", "value": 10},
        summary="budget X",
        chat_id=1,
    )
    snap = await store.get_confirmed(cid)
    assert snap.user_initiated is False


async def test_budget_blocked_when_default_user_initiated():
    # Полный путь: proposal без user_initiated (default False) → бюджет заблокирован гейтом.
    store = FakeStore(FakeProposal("update_budget", "confirmed", user_initiated=False))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_budget(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="1",
                new_budget_micros=50_000_000,
                confirmation_id="x",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (бюджет не по команде)")
        except PermissionError:
            pass
    assert store.finalized is False


# ── FIX 6: абсолютный потолок суммы у границы SDK (defense-in-depth поверх схемы) ─
async def test_apply_update_budget_rejects_absurd_amount():
    store = FakeStore(FakeProposal("update_budget", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_budget(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="1",
                new_budget_micros=mut.MAX_AMOUNT_MICROS + 1,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (сумма за потолком)")
        except ValueError:
            pass
    assert store.finalized is False


# ── pause_campaign: happy path (был вообще без теста) ────────────────────────────
async def test_apply_pause_campaign_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, status):
        called.update(customer_id=customer_id, campaign_id=campaign_id, status=status)
        return {"applied": True, "status": status}

    store = FakeStore(FakeProposal("pause_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_set_campaign_status_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_pause_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert res["applied"] is True
    assert called["status"] == "PAUSED"  # pause → PAUSED
    assert store.finalized is True


# ── Негатив-матрица: чужой аккаунт / без подтверждения для ВСЕХ apply_* ───────────
def _apply_case(op):
    """(apply_fn, kwargs без customer_id/confirm_store) для каждой поддержанной операции."""
    base = {"confirmation_id": "ok", "ads_client": _FakeClient()}
    if op == "update_budget":
        return mut.apply_update_budget, {
            "campaign_id": "1",
            "new_budget_micros": 50_000_000,
            **base,
        }
    if op == "update_bid":
        return mut.apply_update_bid, {"campaign_id": "7", "bids": [("42", 1_500_000)], **base}
    if op == "add_keywords":
        return mut.apply_add_keywords, {
            "ad_group_ids": ["1"],
            "keywords": ["цветы"],
            "match_type": "broad",
            **base,
        }
    if op == "add_negative_keywords":
        return mut.apply_add_negative_keywords, {
            "campaign_id": "7",
            "keywords": ["бесплатно"],
            "match_type": "broad",
            **base,
        }
    if op == "resume_campaign":
        return mut.apply_resume_campaign, {"campaign_id": "7", **base}
    if op == "pause_campaign":
        return mut.apply_pause_campaign, {"campaign_id": "7", **base}
    raise AssertionError(op)


_ALL_OPS = [
    "update_budget",
    "update_bid",
    "add_keywords",
    "add_negative_keywords",
    "resume_campaign",
    "pause_campaign",
]


async def test_all_apply_reject_foreign_account():
    for op in _ALL_OPS:
        fn, kw = _apply_case(op)
        store = FakeStore(FakeProposal(op, "confirmed", user_initiated=True))
        with allowed_ids(DRAFT_ACCOUNT_ID):
            try:
                await fn(customer_id="1234567890", confirm_store=store, **kw)
                raise AssertionError(f"{op}: чужой аккаунт должен падать PermissionError")
            except PermissionError:
                pass
        assert store.finalized is False, op


async def test_all_apply_reject_without_confirmation():
    for op in _ALL_OPS:
        fn, kw = _apply_case(op)
        store = FakeStore(proposal=None)  # нет подтверждённого черновика
        with allowed_ids(DRAFT_ACCOUNT_ID):
            try:
                await fn(customer_id=DRAFT_ACCOUNT_ID, confirm_store=store, **kw)
                raise AssertionError(f"{op}: без confirmation должен падать PermissionError")
            except PermissionError:
                pass
        assert store.finalized is False, op


# ── FIX: account-lock на уровне РЕЗОЛВЕРОВ (find_campaign_by_name / find_ad_groups) ─
def test_resolvers_reject_foreign_account():
    from ads.resolve import find_ad_groups, find_campaign_by_name

    with allowed_ids(DRAFT_ACCOUNT_ID):
        for fn in (find_campaign_by_name, find_ad_groups):
            try:
                fn(object(), "1234567890", "X")  # ensure_allowed до любого обращения к SDK
                raise AssertionError(f"{fn.__name__}: чужой аккаунт должен падать")
            except PermissionError:
                pass


# ── FIX 3: ensure_manager_allowed — обход MCC только настроенного менеджера ───────
def test_ensure_manager_allowed():
    from ads.client import ensure_manager_allowed

    prev = settings.google_ads_login_customer_id
    try:
        settings.google_ads_login_customer_id = ""  # не задан → fail-closed
        try:
            ensure_manager_allowed("123")
            raise AssertionError("ожидался PermissionError (login_customer_id пуст)")
        except PermissionError:
            pass

        settings.google_ads_login_customer_id = "9998887777"
        ensure_manager_allowed("999-888-7777")  # нормализация → совпало → ок
        try:
            ensure_manager_allowed("1112223333")
            raise AssertionError("ожидался PermissionError (чужой менеджер)")
        except PermissionError:
            pass
    finally:
        settings.google_ads_login_customer_id = prev


# ── execute_confirmed: fail-closed на None и статус != confirmed (defense-in-depth) ─
async def test_execute_confirmed_rejects_unconfirmed_and_missing():
    from ads.service import execute_confirmed

    class _S:
        def __init__(self, p):
            self._p = p

        async def get_confirmed(self, cid):
            return self._p

    # status=pending → PermissionError
    pending = SimpleNamespace(operation="update_budget", status="pending", params={})
    try:
        await execute_confirmed(_S(pending), "cid")
        raise AssertionError("ожидался PermissionError (не confirmed)")
    except PermissionError:
        pass

    # None → ValueError
    try:
        await execute_confirmed(_S(None), "cid")
        raise AssertionError("ожидался ValueError (черновик не найден)")
    except ValueError:
        pass
