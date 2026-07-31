"""Trusted WRITE facade: no model-controlled ids and no execution without a real reply anchor."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import ads.service
import mcp_server.tools_write as tools_write
from agent.tools.schemas import LaunchCampaign, MUTATION_TOOLS
from mcp_server.trusted_transport import TrustedTurn, trusted_turn_scope


def _turn(*, reply: bool = True) -> TrustedTurn:
    return TrustedTurn(
        actor_user_id=101,
        actor_chat_id=-202,
        actor_username="operator",
        chat_type="supergroup",
        thread_id="7",
        message_id=303,
        language_code="ru",
        reply_to_message_id=404 if reply else None,
        reply_to_is_own_message=reply,
        reply_confirmation_id="a" * 32 if reply else None,
        reply_to_text="audit preview" if reply else None,
        issued_at=1,
        expires_at=2,
    )


class _Store:
    applied_audit = SimpleNamespace(
        operation="update_campaign",
        customer_id="7753643025",
        result={
            "customer_id": "7753643025",
            "campaign_id": "23995782408",
            "new_name": "Доставка цветов [UAT]",
            "applied": True,
        },
    )

    async def bind_card_message_id_from_verified_reply(
        self, confirmation_id, message_id, *, actor_user_id, actor_chat_id
    ):
        return (
            confirmation_id == "a" * 32
            and message_id == 404
            and actor_user_id == 101
            and actor_chat_id == -202
        )

    async def get_confirmed(self, confirmation_id):  # noqa: ARG002
        return SimpleNamespace(customer_id="7753643025", status="pending", summary="audit preview")

    async def get_applied_audit_result(self, confirmation_id):  # noqa: ARG002
        return self.applied_audit


async def test_execute_facade_rejects_non_reply_before_service(monkeypatch):
    called = False

    async def _execute(*args, **kwargs):  # noqa: ARG001
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(tools_write, "ConfirmStore", _Store)
    monkeypatch.setattr(ads.service, "confirm_and_execute_by_reply", _execute)

    with trusted_turn_scope(_turn(reply=False)):
        result = await tools_write.execute_confirmed()

    assert result["status"] == "failed"
    assert result["error_code"] == "refused"
    assert called is False


async def test_execute_facade_uses_only_verified_reply_metadata(monkeypatch):
    seen = {}

    async def _execute(store, **kwargs):
        seen.update(kwargs)
        return {
            "customer_id": "forged-by-service",
            "operation": "forged-by-service",
            "display": "forged-by-service",
        }

    monkeypatch.setattr(tools_write, "ConfirmStore", _Store)
    monkeypatch.setattr(ads.service, "confirm_and_execute_by_reply", _execute)

    with trusted_turn_scope(_turn()) as _:
        result = await tools_write.execute_confirmed()

    assert result == {
        "status": "executed",
        "operation": "update_campaign",
        "summary": "• campaign_id: 23995782408\n• new_name: Доставка цветов [UAT]",
        "audit_result": {
            "customer_id": "7753643025",
            "campaign_id": "23995782408",
            "new_name": "Доставка цветов [UAT]",
            "applied": True,
        },
        "customer_id": "7753643025",
        "confirmation_id": "a" * 32,
    }
    assert seen == {
        "confirmation_id": "a" * 32,
        "actor_user_id": 101,
        "actor_chat_id": -202,
        "reply_to_message_id": 404,
        "actor_username": "operator",
    }


async def test_execute_facade_never_reports_executed_without_applied_audit(monkeypatch):
    class _NoAuditStore(_Store):
        applied_audit = None

    async def _execute(*args, **kwargs):  # noqa: ARG001
        return {"applied": True, "display": "must not escape"}

    monkeypatch.setattr(tools_write, "ConfirmStore", _NoAuditStore)
    monkeypatch.setattr(ads.service, "confirm_and_execute_by_reply", _execute)

    with trusted_turn_scope(_turn()):
        result = await tools_write.execute_confirmed()

    assert result == {
        "status": "needs_review",
        "error": "Изменение могло примениться, но подтверждённый итог audit недоступен. Нужна проверка оператором.",
        "error_code": "audit_unavailable",
        "confirmation_id": "a" * 32,
    }


async def test_execute_facade_rejects_marker_without_full_database_diff(monkeypatch):
    called = False

    async def _execute(*args, **kwargs):  # noqa: ARG001
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(tools_write, "ConfirmStore", _Store)
    monkeypatch.setattr(ads.service, "confirm_and_execute_by_reply", _execute)
    bad = replace(_turn(), reply_to_text="AIMASH_CONFIRM:" + "a" * 32 + "\nnot the diff")

    with trusted_turn_scope(bad):
        result = await tools_write.execute_confirmed()

    assert result["status"] == "failed"
    assert result["error_code"] == "refused"
    assert called is False


async def test_execute_facade_redacts_expected_errors(monkeypatch):
    secret = "sk-" + ("a" * 32)

    async def _execute(*args, **kwargs):  # noqa: ARG001
        raise ValueError(f"invalid credential {secret}")

    monkeypatch.setattr(tools_write, "ConfirmStore", _Store)
    monkeypatch.setattr(ads.service, "confirm_and_execute_by_reply", _execute)

    with trusted_turn_scope(_turn()):
        result = await tools_write.execute_confirmed()

    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_argument"
    assert secret not in result["error"]


async def test_launch_campaign_is_proposal_only_agent_capability(monkeypatch):
    captured = {}

    async def _propose(operation, model_cls, **kwargs):
        captured.update(operation=operation, model_cls=model_cls, kwargs=kwargs)
        return {"status": "pending", "operation": operation}

    monkeypatch.setattr(tools_write, "_propose", _propose)

    result = await tools_write.propose_launch_campaign("7753643025", "Draft Search")

    assert result == {"status": "pending", "operation": "launch_campaign"}
    assert captured == {
        "operation": "launch_campaign",
        "model_cls": LaunchCampaign,
        "kwargs": {"account": "7753643025", "campaign": "Draft Search"},
    }
    assert "launch_campaign" in MUTATION_TOOLS
    assert (
        tools_write.PROPOSE_TOOL_FUNCS["propose_launch_campaign"]
        is tools_write.propose_launch_campaign
    )
