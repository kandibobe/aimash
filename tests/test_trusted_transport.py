"""Cryptographic and schema guards for Hermes Telegram -> MCP PLAN/WRITE transport."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import time

import pytest
from pydantic import SecretStr, ValidationError

from core.config import settings
from core.context import get_context
from core.provenance import get_provenance
from mcp_server.trusted_transport import (
    TOKEN_PARAM,
    TrustedTransportError,
    canonical_args_digest,
    trusted_tool,
    verify_turn_token,
)

KEY = "k" * 32


def _token(
    tool: str,
    args: dict,
    *,
    now: int = 1_000,
    expires: int = 1_100,
    key: str = KEY,
    include_arg_keys: bool = True,
) -> str:
    payload = {
        "v": 1,
        "iat": now,
        "exp": expires,
        "platform": "telegram",
        "tool": f"mcp__aimash__{tool}",
        "args_sha256": canonical_args_digest(args),
        "nonce": "n" * 32,
        "actor_user_id": 101,
        "actor_chat_id": -202,
        "actor_username": "operator",
        "chat_type": "supergroup",
        "thread_id": "7",
        "message_id": 303,
        "language_code": "ru",
        "reply_to_message_id": None,
        "reply_to_is_own_message": False,
        "reply_confirmation_id": None,
        "reply_to_text": None,
    }
    if include_arg_keys:
        payload["arg_keys"] = sorted(args)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    sig = hmac.new(key.encode(), raw, hashlib.sha256).digest()

    def enc(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    return f"{enc(raw)}.{enc(sig)}"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "aimash_trust_hmac_key", SecretStr(KEY))


def test_token_is_bound_to_exact_tool_and_arguments():
    token = _token("propose_budget_change", {"account": "7753643025", "value": 20})
    turn = verify_turn_token(
        token,
        expected_tool="propose_budget_change",
        tool_args={"account": "7753643025", "value": 20},
        now=1_050,
    )
    assert turn.actor_user_id == 101

    with pytest.raises(TrustedTransportError):
        verify_turn_token(
            token,
            expected_tool="propose_budget_change",
            tool_args={"account": "7753643025", "value": 21},
            now=1_050,
        )
    with pytest.raises(TrustedTransportError):
        verify_turn_token(token, expected_tool="execute_confirmed", tool_args={}, now=1_050)


def test_digest_accepts_fastmcp_integer_to_float_coercion_but_not_value_change():
    token = _token(
        "propose_budget_change",
        {"account": "7753643025", "value": 20, "nested": [1, {"ratio": 2}]},
    )
    verify_turn_token(
        token,
        expected_tool="propose_budget_change",
        tool_args={"account": "7753643025", "value": 20.0, "nested": [1.0, {"ratio": 2.0}]},
        now=1_050,
    )
    with pytest.raises(TrustedTransportError):
        verify_turn_token(
            token,
            expected_tool="propose_budget_change",
            tool_args={"account": "7753643025", "value": 20.01, "nested": [1, {"ratio": 2}]},
            now=1_050,
        )


def test_forged_or_expired_token_is_rejected():
    forged = _token("execute_confirmed", {}, key="x" * 32)
    with pytest.raises(TrustedTransportError):
        verify_turn_token(forged, expected_tool="execute_confirmed", tool_args={}, now=1_050)

    expired = _token("execute_confirmed", {}, now=1_000, expires=1_010)
    with pytest.raises(TrustedTransportError):
        verify_turn_token(expired, expected_tool="execute_confirmed", tool_args={}, now=1_050)


def test_write_flag_requires_a_real_signing_key():
    from core.config import Settings

    with pytest.raises(ValidationError, match="AIMASH_TRUST_HMAC_KEY"):
        Settings(hermes_write_enabled=True, aimash_trust_hmac_key="short", _env_file=None)
    assert Settings(
        hermes_write_enabled=True, aimash_trust_hmac_key=KEY, _env_file=None
    ).hermes_write_enabled


async def test_wrapper_requires_token_and_opens_human_context(monkeypatch):
    called = False

    async def fn(account: str, currency: str | None = None) -> dict:
        nonlocal called
        called = True
        return {
            "account": account,
            "currency": currency,
            "human": get_provenance().human_turn,
            "actor": get_provenance().actor_user_id,
            "chat": get_context().chat_id,
        }

    async def allowed(actor):
        return actor == 101

    async def account_allowed(actor, account):
        assert (actor, account) == (101, "7753643025")

    monkeypatch.setattr("core.access.is_whitelisted", allowed)
    monkeypatch.setattr("core.access.ensure_account_allowed_for_user", account_allowed)
    wrapped = trusted_tool("propose_test", fn)
    assert TOKEN_PARAM in inspect.signature(wrapped).parameters

    refused = await wrapped(account="7753643025")
    assert refused["status"] == "refused"
    assert called is False

    now = int(time.time())
    token = _token("propose_test", {"account": "7753643025"}, now=now, expires=now + 120)
    # FastMCP supplies omitted schema defaults to the wrapper.  They are safe only while equal to
    # the function's declared defaults and must not invalidate the hook's exact argument binding.
    result = await wrapped(account="7753643025", currency=None, trusted_turn_token=token)
    assert result == {
        "account": "7753643025",
        "currency": None,
        "human": True,
        "actor": 101,
        "chat": -202,
    }

    called = False
    changed_default = await wrapped(account="7753643025", currency="USD", trusted_turn_token=token)
    assert changed_default["status"] == "refused"
    assert "TrustedTransportError" not in changed_default["error"]
    assert called is False


def test_legacy_token_keeps_exact_argument_binding():
    token = _token(
        "propose_test",
        {"account": "7753643025"},
        include_arg_keys=False,
    )
    verify_turn_token(
        token,
        expected_tool="propose_test",
        tool_args={"account": "7753643025"},
        now=1_050,
    )
    with pytest.raises(TrustedTransportError):
        verify_turn_token(
            token,
            expected_tool="propose_test",
            tool_args={"account": "7753643025", "currency": None},
            default_args={"currency": None},
            now=1_050,
        )


def test_wrapper_schema_never_accepts_model_identity_fields():
    from mcp_server.tools_write import execute_confirmed

    wrapped = trusted_tool("execute_confirmed", execute_confirmed)
    params = inspect.signature(wrapped).parameters
    assert set(params) == {TOKEN_PARAM}
    assert not ({"actor_user_id", "actor_chat_id", "reply_to_message_id"} & set(params))
