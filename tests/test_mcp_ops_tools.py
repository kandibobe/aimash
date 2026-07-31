"""Trusted MCP adapters for the operational control plane."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import mcp_server.tools_ops as tools_ops
from mcp_server.envelope import ok
from mcp_server.tools_meta import get_bridge_capabilities
from mcp_server.trusted_transport import TrustedTurn, trusted_turn_scope

DRAFT = "7753643025"


def _turn() -> TrustedTurn:
    return TrustedTurn(
        actor_user_id=101,
        actor_chat_id=-202,
        actor_username="operator",
        chat_type="supergroup",
        thread_id="7",
        message_id=303,
        language_code="ru",
        reply_to_message_id=None,
        reply_to_is_own_message=False,
        reply_confirmation_id=None,
        reply_to_text=None,
        issued_at=1,
        expires_at=2,
    )


async def test_list_decisions_requires_trusted_turn() -> None:
    with pytest.raises(PermissionError, match="trusted"):
        await tools_ops.list_decisions(DRAFT)


async def test_update_decision_scopes_atomic_transition_to_account(monkeypatch) -> None:
    captured: dict = {}

    async def transition(uid: str, action: str, **kwargs) -> bool:
        captured.update(uid=uid, action=action, **kwargs)
        return True

    monkeypatch.setattr(tools_ops, "transition_decision", transition)
    with trusted_turn_scope(_turn()):
        result = await tools_ops.update_decision(
            DRAFT,
            "dec-1",
            "snoozed",
            note="review tomorrow",
            snooze_minutes=60,
            assigned_to=202,
        )

    assert result["updated"] is True
    assert captured["customer_id"] == DRAFT
    assert captured["actor_user_id"] == 101
    assert captured["assigned_to"] == 202
    assert captured["snoozed_until"] > datetime.now(timezone.utc)


async def test_list_incidents_has_machine_pagination(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(
            incident_uid=f"inc-{i}",
            customer_id=DRAFT,
            decision_uid=None,
            kind="runtime",
            severity="warning",
            title=f"Incident {i}",
            evidence={},
            status="open",
            assigned_to=None,
            occurrence_count=1,
            escalation_level=0,
            snoozed_until=None,
            last_seen_at=now,
        )
        for i in range(3)
    ]

    async def list_rows(*args, **kwargs):
        return rows

    monkeypatch.setattr(tools_ops, "_list_incidents", list_rows)
    with trusted_turn_scope(_turn()):
        result = await tools_ops.list_incidents(DRAFT, offset=1, limit=1)
    assert [row["incident_uid"] for row in result["rows"]] == ["inc-1"]
    assert result["has_more"] is True
    assert result["next_offset"] == 2


def test_envelope_exposes_terminal_pagination_state() -> None:
    result = ok([{"id": 1}], offset=0, limit=50)
    assert result["has_more"] is False
    assert result["next_offset"] is None


async def test_capabilities_do_not_disclose_kill_switch_reason(monkeypatch) -> None:
    import core.killswitch

    monkeypatch.setattr(core.killswitch, "mutations_disabled", lambda: "secret file path")
    result = await get_bridge_capabilities()
    assert result["safety"]["kill_switch_active"] is True
    assert "secret file path" not in str(result)
