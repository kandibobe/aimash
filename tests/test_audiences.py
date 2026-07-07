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


# ── C7: чтение ПРИКРЕПЛЁННЫХ аудиторий кампании (для 🗑 detach в UI) ──────────────
def test_list_attached_audiences_maps_names_and_falls_back_to_id():
    class _GA:
        def search(self, customer_id, query):
            if "FROM campaign_criterion" in query:
                assert "!= 'REMOVED'" in query  # снятые критерии не показываем
                return [
                    SimpleNamespace(
                        campaign_criterion=SimpleNamespace(user_list=SimpleNamespace(user_list=_UL))
                    ),
                    SimpleNamespace(
                        campaign_criterion=SimpleNamespace(
                            user_list=SimpleNamespace(user_list="customers/1/userLists/999")
                        )
                    ),
                ]
            return [  # list_audiences: имя есть только у _UL
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
        res = read.list_attached_audiences(_Client(), DRAFT_ACCOUNT_ID, "42")
    assert [a.resource_name for a in res] == [_UL, "customers/1/userLists/999"]
    assert res[0].name == "Покупатели"  # имя из карты list_audiences
    assert "999" in res[1].name  # закрытый/неизвестный список — честный фолбэк на id


async def test_detach_button_mints_pending_proposal_only(monkeypatch):
    """C7: 🗑 у прикреплённой аудитории минтит ТОЛЬКО pending-черновик detach_audience
    (исполнение — после ✅ через confirm-гейт; ничего не выполняется на клик)."""
    import bot.main as bm
    from bot.handlers.campaigns_menu import on_audience_detach

    chat_id = 909
    bm._CAMP_CACHE[chat_id] = [{"name": "Кампания X", "id": "42", "status": "ENABLED"}]
    bm._AUD_DET_CACHE[chat_id] = [SimpleNamespace(resource_name=_UL, name="Покупатели")]
    presented: dict = {}

    async def fake_present(msg, **kw):
        presented.update(kw)

    monkeypatch.setattr(bm, "_present_proposal", fake_present)

    class _Msg:
        chat = SimpleNamespace(id=chat_id)

    class _Cq:
        message = _Msg()
        from_user = SimpleNamespace(id=chat_id)

        async def answer(self, *a, **kw):
            pass

    cb = SimpleNamespace(action="det", camp_idx=0, idx=0)
    await on_audience_detach(_Cq(), cb)
    assert presented.get("operation") == "detach_audience"
    assert presented["params"]["audience_resource_names"] == [_UL]
    assert presented["params"]["_audience_names"] == ["Покупатели"]


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
