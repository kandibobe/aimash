from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import ads.service
import mcp_server.tools_write as tools_write
from llm.schemas import PauseCampaign, UpdateBudget
from confirm.store import ConfirmStore
from core.context import reset_context, set_context
from core.provenance import human_turn


class _Store:
    def __init__(self) -> None:
        self.confirmed: list[tuple[str, int, int | None]] = []

    async def count_run_pending_proposals(self, run_id: str) -> int:  # noqa: ARG002
        return 0

    async def confirm(
        self,
        confirmation_id: str,
        *,
        chat_id: int,
        actor_user_id: int | None = None,
        actor_username: str | None = None,  # noqa: ARG002
    ) -> bool:
        self.confirmed.append((confirmation_id, chat_id, actor_user_id))
        return True


@contextmanager
def _trusted_human_turn():
    token = set_context(request_id="bias-action", chat_id=9001)
    try:
        with human_turn(actor_user_id=42, run_id="bias-action"):
            yield
    finally:
        reset_context(token)


async def _run(
    monkeypatch,
    *,
    operation: str,
    model_cls,
    built_params: dict,
    fields: dict,
):
    store = _Store()

    async def _build(**kwargs):
        return SimpleNamespace(
            cid=kwargs["cid"],
            operation=operation,
            customer_id=kwargs["customer_id"],
            params=built_params,
            display="attested before → after",
        )

    async def _execute(received_store, confirmation_id):
        assert received_store is store
        return {"applied": True, "confirmation_id": confirmation_id}

    monkeypatch.setattr(tools_write, "ConfirmStore", lambda: store)
    monkeypatch.setattr(tools_write, "build_proposal", _build)
    monkeypatch.setattr(ads.service, "execute_confirmed", _execute)
    with _trusted_human_turn():
        result = await tools_write._propose(
            operation,
            model_cls,
            account="7753643025",
            **fields,
        )
    return result, store


async def test_operational_mutation_requires_separate_approval(monkeypatch):
    result, store = await _run(
        monkeypatch,
        operation="pause_campaign",
        model_cls=PauseCampaign,
        built_params={"campaign": "Search"},
        fields={"campaign": "Search"},
    )

    assert result["ok"] is False
    assert result["status"] == "approval_required"
    assert result["operation"] == "pause_campaign"
    assert store.confirmed == []


async def test_proposal_is_persisted_without_claiming_or_applying(monkeypatch):
    async def _build(**kwargs):
        params = {"campaign": "Search"}
        await kwargs["store"].save_proposal(
            confirmation_id=kwargs["cid"],
            operation="pause_campaign",
            customer_id=kwargs["customer_id"],
            params=params,
            summary="ENABLED → PAUSED",
            chat_id=kwargs["chat_id"],
            user_initiated=True,
            risk_tier="L1",
        )
        return SimpleNamespace(
            cid=kwargs["cid"],
            operation="pause_campaign",
            customer_id=kwargs["customer_id"],
            params=params,
            display="ENABLED → PAUSED",
        )

    async def _execute(_store, _confirmation_id):
        raise AssertionError("proposal must not execute before a separate approval")

    monkeypatch.setattr(tools_write, "build_proposal", _build)
    monkeypatch.setattr(ads.service, "execute_confirmed", _execute)
    with _trusted_human_turn():
        result = await tools_write._propose(
            "pause_campaign",
            PauseCampaign,
            account="7753643025",
            campaign="Search",
        )

    assert result["status"] == "approval_required"
    row = (await ConfirmStore().load_proposals([result["confirmation_id"]]))[
        result["confirmation_id"]
    ]
    assert row is not None and row.status == "pending"


async def test_small_budget_change_requires_separate_approval(monkeypatch):
    result, store = await _run(
        monkeypatch,
        operation="update_budget",
        model_cls=UpdateBudget,
        built_params={
            "campaign": "Search",
            "mode": "increase_by_percent",
            "value": 20,
            "_before": {"before_micros": 100_000_000, "after_micros": 120_000_000},
        },
        fields={"campaign": "Search", "mode": "increase_by_percent", "value": 20},
    )

    assert result["status"] == "approval_required"
    assert result["error_type"] == "APPROVAL_REQUIRED"
    assert store.confirmed == []


async def test_critical_global_budget_returns_structured_approval(monkeypatch):
    result, store = await _run(
        monkeypatch,
        operation="update_budget",
        model_cls=UpdateBudget,
        built_params={
            "campaign": "Search",
            "mode": "increase_by_percent",
            "value": 20,
            "_before": {
                "before_micros": 100_000_000,
                "after_micros": 120_000_000,
                "shared_campaigns": ["Search", "Brand"],
            },
        },
        fields={"campaign": "Search", "mode": "increase_by_percent", "value": 20},
    )

    assert result["ok"] is False
    assert result["status"] == "approval_required"
    assert result["error_type"] == "APPROVAL_REQUIRED"
    assert result["confirmation_marker"] in result["preview"]
    assert store.confirmed == []


async def test_machine_turn_cannot_start_an_action(monkeypatch):
    called = False

    async def _build(**kwargs):  # noqa: ARG001
        nonlocal called
        called = True

    monkeypatch.setattr(tools_write, "build_proposal", _build)
    token = set_context(request_id="machine", chat_id=9001)
    try:
        result = await tools_write._propose(
            "pause_campaign",
            PauseCampaign,
            account="7753643025",
            campaign="Search",
        )
    finally:
        reset_context(token)

    assert result["ok"] is False
    assert result["status"] == "refused"
    assert called is False


async def test_machine_turn_cannot_start_composite_change(monkeypatch):
    called = False

    async def _build(**kwargs):  # noqa: ARG001
        nonlocal called
        called = True

    monkeypatch.setattr(tools_write, "build_proposal", _build)
    result = await tools_write.propose_composite_change(
        account="7753643025",
        operations=[
            {"operation": "pause_campaign", "params": {"campaign": "Search"}},
            {"operation": "pause_campaign", "params": {"campaign": "Brand"}},
        ],
    )

    assert result["ok"] is False
    assert result["status"] == "refused"
    assert called is False


async def test_invalid_action_arguments_return_self_healing_json(monkeypatch):
    monkeypatch.setattr(
        tools_write,
        "ConfirmStore",
        lambda: (_ for _ in ()).throw(AssertionError("store must not be reached")),
    )
    with _trusted_human_turn():
        result = await tools_write._propose(
            "update_budget",
            UpdateBudget,
            account="7753643025",
            campaign="Search",
            mode="decrease_by_percent",
            value=150,
        )

    assert result["ok"] is False
    assert result["error_type"] == "INVALID_ARGUMENT"
    assert result["suggested_action"]
