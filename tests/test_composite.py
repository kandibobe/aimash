from __future__ import annotations

import pytest

from ads import composite
from confirm.store import ConfirmedProposal
from conftest import FakeConfirmStore


@pytest.fixture(autouse=True)
def _verified_readback(monkeypatch):
    async def verify(operation, params, customer_id):  # noqa: ARG001
        return {"verified": True, "kind": operation, "expected": "after", "actual": "after"}

    monkeypatch.setattr(composite, "verify_applied_state", verify)


def _parent() -> ConfirmedProposal:
    return ConfirmedProposal(
        operation="composite",
        status="confirmed",
        user_initiated=True,
        params={
            "operations": [
                {"operation": "pause_campaign", "params": {"campaign": "A"}},
                {"operation": "pause_campaign", "params": {"campaign": "B"}},
            ]
        },
        customer_id="7753643025",
        summary="two changes",
        chat_id=1,
        origin_human_turn=True,
    )


class _ParentStore(FakeConfirmStore):
    def __init__(self) -> None:
        self.parent = _parent()
        super().__init__(self.parent)
        self.finalized = None
        self.failure = None
        self.needs_review = None

    async def finalize(self, confirmation_id, *, result):
        self.finalized = result

    async def record_failure(self, confirmation_id, *, error):
        self.failure = error

    async def mark_needs_review(self, confirmation_id, *, error):
        self.needs_review = error


@pytest.mark.asyncio
async def test_composite_executes_in_order_and_finalizes_parent(monkeypatch):
    calls = []

    async def apply(step_store, confirmation_id):
        calls.append(step_store._pending.params["campaign"])
        return {"applied": True, "campaign": calls[-1]}

    monkeypatch.setattr(composite, "execute_confirmed_step", apply)
    store = _ParentStore()
    result = await composite.execute_confirmed_composite(store, "c" * 32)

    assert calls == ["A", "B"]
    assert result["operation_count"] == 2
    assert all(item["verification"]["verified"] is True for item in result["operations"])
    assert store.finalized == result


@pytest.mark.asyncio
async def test_composite_defers_rename_until_name_based_steps_finish(monkeypatch):
    calls = []

    async def apply(step_store, confirmation_id):
        calls.append(step_store._pending.operation)
        return {"applied": True}

    parent = _parent()
    parent.params = {
        "operations": [
            {
                "operation": "update_campaign",
                "params": {"campaign": "Before", "new_name": "After"},
            },
            {
                "operation": "update_budget",
                "params": {"campaign": "Before", "mode": "set_to", "value": 1.1},
            },
        ]
    }
    monkeypatch.setattr(composite, "execute_confirmed_step", apply)
    store = _ParentStore()
    store.parent = parent
    store._p = parent

    result = await composite.execute_confirmed_composite(store, "c" * 32)

    assert calls == ["update_budget", "update_campaign"]
    assert [item["index"] for item in result["operations"]] == [2, 1]


@pytest.mark.asyncio
async def test_composite_compensates_completed_steps_on_failure(monkeypatch):
    calls = []

    async def apply(step_store, confirmation_id):
        operation = step_store._pending.operation
        campaign = step_store._pending.params["campaign"]
        calls.append((operation, campaign))
        if campaign == "B":
            raise RuntimeError("step failed")
        return {"applied": True}

    async def reverse(parent, operation, params):
        return "resume_campaign", {"campaign": params["campaign"]}

    monkeypatch.setattr(composite, "execute_confirmed_step", apply)
    monkeypatch.setattr(composite, "_rebuilt_reverse", reverse)
    store = _ParentStore()

    with pytest.raises(RuntimeError, match="step failed"):
        await composite.execute_confirmed_composite(store, "c" * 32)

    assert calls == [
        ("pause_campaign", "A"),
        ("pause_campaign", "B"),
        ("resume_campaign", "A"),
    ]
    assert store.failure and "rolled back" in store.failure
    assert store.needs_review is None


@pytest.mark.asyncio
async def test_composite_post_verify_mismatch_compensates_and_does_not_finalize(monkeypatch):
    calls = []

    async def apply(step_store, confirmation_id):  # noqa: ARG001
        calls.append((step_store._pending.operation, step_store._pending.params["campaign"]))
        return {"applied": True}

    async def verify(operation, params, customer_id):  # noqa: ARG001
        if operation == "pause_campaign" and params["campaign"] == "A":
            return {
                "verified": False,
                "kind": "status",
                "expected": "PAUSED",
                "actual": "ENABLED",
            }
        return {"verified": True, "kind": "status", "expected": "ENABLED", "actual": "ENABLED"}

    async def reverse(parent, operation, params):  # noqa: ARG001
        return "resume_campaign", {
            "campaign": params["campaign"],
            "_before": {"kind": "status"},
        }

    monkeypatch.setattr(composite, "execute_confirmed_step", apply)
    monkeypatch.setattr(composite, "verify_applied_state", verify)
    monkeypatch.setattr(composite, "_rebuilt_reverse", reverse)
    store = _ParentStore()

    with pytest.raises(composite.CompositeVerificationError, match="post-verify failed"):
        await composite.execute_confirmed_composite(store, "c" * 32)

    assert calls == [("pause_campaign", "A"), ("resume_campaign", "A")]
    assert store.finalized is None
    assert store.failure and "rolled back" in store.failure
    assert store.needs_review is None


@pytest.mark.asyncio
async def test_composite_verifier_exception_still_compensates_applied_step(monkeypatch):
    calls = []

    async def apply(step_store, confirmation_id):  # noqa: ARG001
        calls.append((step_store._pending.operation, step_store._pending.params["campaign"]))
        return {"applied": True}

    async def verify(operation, params, customer_id):  # noqa: ARG001
        if operation == "pause_campaign":
            raise ConnectionError("readback failed")
        return {"verified": True, "kind": "status", "expected": "ENABLED", "actual": "ENABLED"}

    async def reverse(parent, operation, params):  # noqa: ARG001
        return "resume_campaign", {
            "campaign": params["campaign"],
            "_before": {"kind": "status"},
        }

    monkeypatch.setattr(composite, "execute_confirmed_step", apply)
    monkeypatch.setattr(composite, "verify_applied_state", verify)
    monkeypatch.setattr(composite, "_rebuilt_reverse", reverse)
    store = _ParentStore()

    with pytest.raises(ConnectionError, match="readback failed"):
        await composite.execute_confirmed_composite(store, "c" * 32)

    assert calls == [("pause_campaign", "A"), ("resume_campaign", "A")]
    assert store.finalized is None
    assert store.failure and "rolled back" in store.failure


@pytest.mark.asyncio
async def test_composite_unverified_compensation_requires_operator_review(monkeypatch):
    async def apply(step_store, confirmation_id):  # noqa: ARG001
        if step_store._pending.params["campaign"] == "B":
            raise RuntimeError("second step failed")
        return {"applied": True}

    async def verify(operation, params, customer_id):  # noqa: ARG001
        if operation == "resume_campaign":
            return {"verified": None, "reason": "readback_unavailable"}
        return {"verified": True, "kind": "status", "expected": "PAUSED", "actual": "PAUSED"}

    async def reverse(parent, operation, params):  # noqa: ARG001
        return "resume_campaign", {
            "campaign": params["campaign"],
            "_before": {"kind": "status"},
        }

    monkeypatch.setattr(composite, "execute_confirmed_step", apply)
    monkeypatch.setattr(composite, "verify_applied_state", verify)
    monkeypatch.setattr(composite, "_rebuilt_reverse", reverse)
    store = _ParentStore()

    with pytest.raises(RuntimeError, match="second step failed"):
        await composite.execute_confirmed_composite(store, "c" * 32)

    assert store.failure is None
    assert store.needs_review and "rollback was incomplete" in store.needs_review


@pytest.mark.asyncio
async def test_composite_parent_is_one_shot(monkeypatch):
    async def apply(step_store, confirmation_id):
        return {"applied": True}

    monkeypatch.setattr(composite, "execute_confirmed_step", apply)
    store = _ParentStore()
    await composite.execute_confirmed_composite(store, "c" * 32)
    with pytest.raises(PermissionError):
        await composite.execute_confirmed_composite(store, "c" * 32)
