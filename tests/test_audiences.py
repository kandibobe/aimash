"""Офлайн-тесты §3: аудитории — чтение доступных (user_list) + прикрепление к кампании за двумя
гейтами. Без живого Google Ads — SDK подменён фейком. Бэкенд-слой; выбор аудиторий в боте (визард)
подключается отдельно.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
from ads import read  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


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
        p = self._p
        if p is None or p.status != "confirmed" or p.operation != operation or self._claimed:
            return None
        self._claimed = True
        return p

    async def finalize(self, confirmation_id, *, result):
        self.finalized = True


_UL = "customers/1/userLists/55"
_AUD = "customers/1/audiences/77"


# ── Чтение доступных аудиторий (user_list) ───────────────────────────────────────
def test_list_audiences_reads_user_lists():
    class _GA:
        def search(self, customer_id, query):
            return [
                SimpleNamespace(
                    user_list=SimpleNamespace(
                        resource_name=_UL, name="Покупатели", size_for_display=1500
                    )
                )
            ]

    class _Client:
        def get_service(self, name):
            return _GA()

    with allowed_ids(DRAFT_ACCOUNT_ID):
        res = read.list_audiences(_Client(), DRAFT_ACCOUNT_ID)
    assert len(res) == 1 and res[0].name == "Покупатели"
    assert res[0].resource_name == _UL and res[0].size == 1500


def test_list_audiences_rejects_foreign_account():
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            read.list_audiences(object(), "1234567890")


# ── apply_attach_audience: оба гейта (не деньги → без user_initiated) ─────────────
async def test_apply_attach_audience_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, rns):
        called.update(campaign_id=campaign_id, rns=list(rns))
        return {"applied": True, "count": len(rns)}

    store = FakeStore(FakeProposal("attach_audience", "confirmed", user_initiated=True))
    with patched(mut, "_attach_audience_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_attach_audience(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            audience_resource_names=[_UL, _AUD],
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] and called["campaign_id"] == "23" and called["rns"] == [_UL, _AUD]
    assert store.finalized is True


async def test_apply_attach_audience_validates_before_claim():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("attach_audience", "confirmed", user_initiated=True))
    with patched(mut, "_attach_audience_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(ValueError):
            await mut.apply_attach_audience(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                audience_resource_names=["customers/1/campaigns/9"],  # не аудитория → ValueError
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
    assert calls["n"] == 0 and store.finalized is False


async def test_apply_attach_audience_rejects_foreign_and_no_confirmation():
    # чужой аккаунт
    store = FakeStore(FakeProposal("attach_audience", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            await mut.apply_attach_audience(
                customer_id="1234567890",
                campaign_id="23",
                audience_resource_names=[_UL],
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
    assert store.finalized is False
    # без подтверждения
    store2 = FakeStore(proposal=None)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            await mut.apply_attach_audience(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                audience_resource_names=[_UL],
                confirmation_id="bogus",
                confirm_store=store2,
                ads_client=object(),
            )
    assert store2.finalized is False


def test_attach_audience_via_sdk_branches_user_list_and_audience():
    captured = {}

    class _Cmp:
        def campaign_path(self, cid, campid):
            return f"customers/{cid}/campaigns/{campid}"

    class _Crit:
        def mutate_campaign_criteria(self, customer_id, operations):
            captured["ops"] = list(operations)
            return SimpleNamespace(
                results=[SimpleNamespace(resource_name=f"crit{i}") for i in range(len(operations))]
            )

    class _Client:
        def get_service(self, name):
            return _Cmp() if name == "CampaignService" else _Crit()

        def get_type(self, name):
            return SimpleNamespace(
                create=SimpleNamespace(
                    campaign=None,
                    user_list=SimpleNamespace(user_list=None),
                    audience=SimpleNamespace(audience=None),
                )
            )

    res = mut._attach_audience_via_sdk(_Client(), DRAFT_ACCOUNT_ID, "23", [_UL, _AUD])
    assert res["applied"] and res["count"] == 2
    ops = captured["ops"]
    assert ops[0].create.user_list.user_list == _UL  # user_list-ресурс → criterion.user_list
    assert ops[1].create.audience.audience == _AUD  # audience-ресурс → criterion.audience


def test_attach_audience_in_supported_operations():
    from ads.service import SUPPORTED_OPERATIONS

    assert "attach_audience" in SUPPORTED_OPERATIONS


def test_attach_audience_schema_validates_and_rejects():
    from agent.tools.schemas import AttachAudience

    ok = AttachAudience(campaign="X", audience_resource_names=[f"  {_UL}  ", _AUD])
    assert ok.audience_resource_names == [_UL, _AUD]  # обрезаны пробелы
    with pytest.raises(Exception):
        AttachAudience(campaign="X", audience_resource_names=["customers/1/campaigns/9"])  # не ауд.
    with pytest.raises(Exception):
        AttachAudience(campaign="X", audience_resource_names=[])  # пусто
