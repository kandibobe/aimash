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

    async def get_confirmed(self, confirmation_id):
        return self._p

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


async def test_capability_guard_declines_deferred_geo_before_proposal():
    import agent.loop as L

    fake = _fake_chat("set_geo_proximity", {"campaign": "X", "location": "Киев", "radius_km": 5})
    with patched(L, "chat", fake):
        res = await L.handle_command("таргет в радиусе 5 км от Киева", chat_id=1)
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
async def test_execute_confirmed_rejects_deferred_geo():
    from ads.service import execute_confirmed

    cp = SimpleNamespace(
        operation="set_geo_proximity",
        status="confirmed",
        params={"campaign": "X", "location": "Киев", "radius_km": 5},
    )

    class _S:
        async def get_confirmed(self, cid):
            return cp

    try:
        await execute_confirmed(_S(), "cid")
        raise AssertionError("ожидался PermissionError (geo отложен, A-geo)")
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

    await store.finalize(cid, result={"applied": True, "count": 1})

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
