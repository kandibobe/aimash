"""Single-confirmation execution for a bounded batch of reversible Ads changes."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ads.freshness import attach_freshness
from ads.service import execute_confirmed_step, read_state, verify_applied_state
from confirm.reverse import reverse_spec
from confirm.store import ConfirmedProposal
from core.logging import redact_text


class CaptureStore:
    """Capture ``build_proposal`` output without creating child proposal rows."""

    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def save_proposal(self, **kwargs: Any) -> None:
        self.saved = dict(kwargs)


class CompositeVerificationError(RuntimeError):
    """A step mutated successfully but its postcondition was not proven by READ-back."""


class _StepStore:
    """Expose one parent-authorized step to unchanged mutation functions."""

    def __init__(self, parent: ConfirmedProposal, operation: str, params: dict[str, Any]) -> None:
        self._pending = replace(parent, operation=operation, params=params, status="confirmed")
        self._claimed = False
        self.result: dict[str, Any] | None = None

    async def get_confirmed(self, confirmation_id: str) -> ConfirmedProposal:  # noqa: ARG002
        return self._pending if not self._claimed else replace(self._pending, status="executing")

    async def claim(
        self,
        confirmation_id: str,
        *,
        operation: str,  # noqa: ARG002
    ) -> ConfirmedProposal | None:
        if self._claimed or operation != self._pending.operation:
            return None
        self._claimed = True
        return replace(self._pending, status="executing")

    async def finalize(self, confirmation_id: str, *, result: dict) -> None:  # noqa: ARG002
        if not self._claimed:
            raise PermissionError("composite step was not claimed")
        self.result = dict(result)


def _ordered_steps(operations: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Preserve user order except that identity-changing renames execute last.

    Composite children currently address campaigns by their pre-change name.  Executing a rename
    first makes every later freshness read against that name fail as ``not_found``.  A rename is
    reversible and does not affect the semantics of the other children, so defer it while keeping
    the original proposal index for audit/reporting.
    """

    indexed = list(enumerate(operations, start=1))
    return sorted(indexed, key=lambda pair: pair[1].get("operation") == "update_campaign")


async def _rebuilt_reverse(
    parent: ConfirmedProposal,
    operation: str,
    params: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    reverse = reverse_spec(operation, params, params.get("_before"))
    if reverse is None:
        return None
    reverse_operation, reverse_params = reverse
    snap = await read_state(reverse_operation, reverse_params, customer_id=parent.customer_id)
    return reverse_operation, attach_freshness(reverse_params, snap)


async def execute_confirmed_composite(store, confirmation_id: str) -> dict[str, Any]:
    """Claim the parent once, run ordered steps, and compensate completed steps on failure."""
    parent = await store.get_confirmed(confirmation_id)
    if parent is None or parent.status != "confirmed" or parent.operation != "composite":
        raise PermissionError("composite proposal is not confirmed")
    operations = list((parent.params or {}).get("operations") or [])
    if not 2 <= len(operations) <= 10:
        raise ValueError("composite must contain 2..10 operations")
    claimed = await store.claim(confirmation_id, operation="composite")
    if claimed is None:
        raise PermissionError("composite proposal was already claimed or expired")

    completed: list[dict[str, Any]] = []
    try:
        for index, item in _ordered_steps(operations):
            operation = str(item["operation"])
            params = dict(item["params"])
            step_store = _StepStore(claimed, operation, params)
            result = await execute_confirmed_step(step_store, confirmation_id)
            completed_item = {
                "index": index,
                "operation": operation,
                "params": params,
                "result": result,
                "verification": {"verified": None, "reason": "readback_not_completed"},
            }
            # Append before READ-back: if the verifier itself fails, the mutation may already have
            # applied and therefore must participate in compensation.
            completed.append(completed_item)
            verification = await verify_applied_state(operation, params, claimed.customer_id)
            completed_item["verification"] = verification
            if verification.get("verified") is not True:
                raise CompositeVerificationError(
                    f"post-verify failed for composite step {index}: "
                    f"{verification.get('reason') or verification.get('kind') or 'unknown'}"
                )
    except Exception as exc:
        rollbacks: list[dict[str, Any]] = []
        rollback_ok = True
        for item in reversed(completed):
            try:
                rebuilt = await _rebuilt_reverse(claimed, item["operation"], item["params"])
                if rebuilt is None:
                    rollback_ok = False
                    rollbacks.append({"operation": item["operation"], "status": "unavailable"})
                    continue
                reverse_operation, reverse_params = rebuilt
                reverse_store = _StepStore(claimed, reverse_operation, reverse_params)
                result = await execute_confirmed_step(reverse_store, confirmation_id)
                verification = await verify_applied_state(
                    reverse_operation, reverse_params, claimed.customer_id
                )
                if verification.get("verified") is not True:
                    rollback_ok = False
                    rollbacks.append(
                        {
                            "operation": reverse_operation,
                            "status": "unverified",
                            "result": result,
                            "verification": verification,
                        }
                    )
                    continue
                rollbacks.append(
                    {
                        "operation": reverse_operation,
                        "status": "verified",
                        "result": result,
                        "verification": verification,
                    }
                )
            except Exception as rollback_exc:  # noqa: BLE001 - terminal audit decides recovery
                rollback_ok = False
                rollbacks.append(
                    {
                        "operation": item["operation"],
                        "status": "failed",
                        "error": redact_text(str(rollback_exc)),
                    }
                )
        error = redact_text(str(exc))
        if rollback_ok:
            await store.record_failure(
                confirmation_id,
                error=f"composite failed; completed steps rolled back: {error}",
            )
        else:
            await store.mark_needs_review(
                confirmation_id,
                error=f"composite failed and rollback was incomplete: {error}; {rollbacks}",
            )
        raise

    result = {
        "applied": True,
        "operation_count": len(completed),
        "operations": [
            {
                "index": item["index"],
                "operation": item["operation"],
                "result": item["result"],
                "verification": item["verification"],
            }
            for item in completed
        ],
    }
    await store.finalize(confirmation_id, result=result)
    return result
