from __future__ import annotations

import pytest

from ads import composite
from confirm.store import ConfirmedProposal
from conftest import FakeConfirmStore


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
    assert store.finalized == result


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
async def test_composite_parent_is_one_shot(monkeypatch):
    async def apply(step_store, confirmation_id):
        return {"applied": True}

    monkeypatch.setattr(composite, "execute_confirmed_step", apply)
    store = _ParentStore()
    await composite.execute_confirmed_composite(store, "c" * 32)
    with pytest.raises(PermissionError):
        await composite.execute_confirmed_composite(store, "c" * 32)
