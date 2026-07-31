"""Pinned Hermes hook behavior: trusted event correlation, token overwrite and И7 phase lock."""

from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from core.config import settings
from mcp_server.trusted_transport import verify_turn_token

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "deploy/hermes/plugins/aimash_trusted_transport/__init__.py"
KEY = "p" * 32


def _load(monkeypatch, env: dict[str, str]):
    gateway = types.ModuleType("gateway")
    session_context = types.ModuleType("gateway.session_context")
    session_context.get_session_env = lambda name, default="": env.get(name, default)
    gateway.session_context = session_context
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    monkeypatch.setenv("AIMASH_TRUST_HMAC_KEY", KEY)
    monkeypatch.setattr(settings, "aimash_trust_hmac_key", SecretStr(KEY))
    spec = importlib.util.spec_from_file_location(f"aimash_transport_{uuid.uuid4().hex}", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Raw:
    def __init__(self, *, own_reply: bool):
        self.from_user = SimpleNamespace(language_code="ru", username="operator")
        self.reply_to_message = (
            SimpleNamespace(from_user=SimpleNamespace(id=9001)) if own_reply else None
        )

    def get_bot(self):
        return SimpleNamespace(id=9001)


def _event(*, own_reply: bool = False, marker: str | None = None):
    return SimpleNamespace(
        source=SimpleNamespace(
            platform="telegram",
            user_id="101",
            chat_id="-202",
            chat_type="supergroup",
            thread_id="7",
            user_name="operator",
        ),
        message_id=303,
        reply_to_message_id=404 if own_reply else None,
        reply_to_text=(f"🔐 AIMASH_CONFIRM:{marker}\npreview" if marker else None),
        reply_to_is_own_message=False,
        raw_message=_Raw(own_reply=own_reply),
    )


def _env() -> dict[str, str]:
    return {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "-202",
        "HERMES_SESSION_MESSAGE_ID": "303",
        "HERMES_SESSION_USER_ID": "101",
    }


def test_plugin_overwrites_model_token_with_verified_event(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    args = {"account": "7753643025", "campaign": "X", "trusted_turn_token": "model-forgery"}

    result = plugin._pre_tool_call(
        tool_name="mcp__aimash__propose_pause_campaign",
        args=args,
        session_id="s1",
        turn_id="t1",
    )

    assert result is None
    assert args["trusted_turn_token"] != "model-forgery"
    turn = verify_turn_token(
        args["trusted_turn_token"],
        expected_tool="propose_pause_campaign",
        tool_args={"account": "7753643025", "campaign": "X"},
    )
    assert (turn.actor_user_id, turn.actor_chat_id, turn.message_id) == (101, -202, 303)


def test_execute_requires_reply_to_own_marker_message(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    args = {}
    blocked = plugin._pre_tool_call(
        tool_name="mcp__aimash__execute_confirmed",
        args=args,
        session_id="s2",
        turn_id="t2",
    )
    assert blocked["action"] == "block"
    assert "trusted_turn_token" not in args

    marker = "a" * 32
    plugin._capture_gateway_event(event=_event(own_reply=True, marker=marker))
    allowed = plugin._pre_tool_call(
        tool_name="mcp__aimash__execute_confirmed",
        args=args,
        session_id="s3",
        turn_id="t3",
    )
    assert allowed is None
    turn = verify_turn_token(
        args["trusted_turn_token"], expected_tool="execute_confirmed", tool_args={}
    )
    assert turn.reply_to_message_id == 404
    assert turn.reply_to_is_own_message is True
    assert turn.reply_confirmation_id == marker


def test_external_tool_and_write_are_mutually_exclusive_per_turn(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    assert (
        plugin._pre_tool_call(tool_name="web_search", args={}, session_id="s4", turn_id="t4")
        is None
    )
    blocked = plugin._pre_tool_call(
        tool_name="mcp__aimash__propose_pause_campaign",
        args={"account": "7753643025", "campaign": "X"},
        session_id="s4",
        turn_id="t4",
    )
    assert blocked["action"] == "block"

    args = {"account": "7753643025", "campaign": "Y"}
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__propose_pause_campaign",
            args=args,
            session_id="s5",
            turn_id="t5",
        )
        is None
    )
    blocked_external = plugin._pre_tool_call(
        tool_name="web_search", args={}, session_id="s5", turn_id="t5"
    )
    assert blocked_external["action"] == "block"


def test_recall_client_is_tainted_external_content(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__recall_client",
            args={"account": "7753643025"},
            session_id="s6",
            turn_id="t6",
        )
        is None
    )
    blocked = plugin._pre_tool_call(
        tool_name="mcp__aimash__propose_pause_campaign",
        args={"account": "7753643025", "campaign": "X"},
        session_id="s6",
        turn_id="t6",
    )
    assert blocked["action"] == "block"
