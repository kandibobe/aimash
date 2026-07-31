"""Pinned Hermes hook behavior: trusted event correlation, token overwrite and И7 phase lock."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
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


def test_plan_state_tools_receive_trusted_token(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    args = {"confirmation_id": "a" * 32}

    allowed = plugin._pre_tool_call(
        tool_name="mcp__aimash__cancel_proposal",
        args=args,
        session_id="s-plan",
        turn_id="t-plan",
    )

    assert allowed is None
    turn = verify_turn_token(
        args["trusted_turn_token"],
        expected_tool="cancel_proposal",
        tool_args={"confirmation_id": "a" * 32},
    )
    assert (turn.actor_user_id, turn.actor_chat_id) == (101, -202)


async def test_confirmation_button_becomes_exact_trusted_reply(monkeypatch):
    env = _env()
    plugin = _load(monkeypatch, env)

    class _Button:
        def __init__(self, text, callback_data):
            self.text = text
            self.callback_data = callback_data

    class _Markup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    telegram = types.ModuleType("telegram")
    telegram.InlineKeyboardButton = _Button
    telegram.InlineKeyboardMarkup = _Markup
    monkeypatch.setitem(sys.modules, "telegram", telegram)

    class _MessageEvent(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class _MessageType:
        TEXT = "text"

    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")
    base.MessageEvent = _MessageEvent
    base.MessageType = _MessageType
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base)

    sent_events = []
    attached = []

    class _Bot:
        async def edit_message_reply_markup(self, **kwargs):
            attached.append(kwargs)

    class _Result:
        success = True
        message_id = "404"

    class _Adapter:
        _aimash_button_bridge = False

        def __init__(self):
            self._bot = _Bot()

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return _Result()

        async def edit_message(
            self, chat_id, message_id, content, *, finalize=False, metadata=None
        ):
            return _Result()

        async def _handle_callback_query(self, update, context):
            raise AssertionError("Aimash callback must not reach the Hermes catch-all")

        def _is_callback_user_authorized(self, *args, **kwargs):
            return True

        def build_source(self, **kwargs):
            return SimpleNamespace(platform="telegram", **kwargs)

        async def handle_message(self, event):
            sent_events.append(event)

    adapter_module = types.ModuleType("plugins.platforms.telegram.adapter")
    adapter_module.TelegramAdapter = _Adapter
    adapter_module.normalize_telegram_chat_id = int
    plugins = types.ModuleType("plugins")
    plugin_platforms = types.ModuleType("plugins.platforms")
    plugin_telegram = types.ModuleType("plugins.platforms.telegram")
    monkeypatch.setitem(sys.modules, "plugins", plugins)
    monkeypatch.setitem(sys.modules, "plugins.platforms", plugin_platforms)
    monkeypatch.setitem(sys.modules, "plugins.platforms.telegram", plugin_telegram)
    monkeypatch.setitem(sys.modules, "plugins.platforms.telegram.adapter", adapter_module)

    assert plugin._install_telegram_button_bridge() is True
    adapter = _Adapter()
    marker = "a" * 32
    card = f"🧾 Черновик изменения\n\npreview\n\nAIMASH_CONFIRM:{marker}"
    await adapter.send("-202", card, metadata={"notify": True})
    keyboard = attached[0]["reply_markup"].inline_keyboard
    assert [button.text for row in keyboard for button in row] == [
        "✅ Подтвердить",
        "✏️ Изменить",
        "❌ Отмена",
    ]

    answers = []

    async def _answer(*, text):
        answers.append(text)

    async def _remove_markup(*, reply_markup):
        attached.append({"reply_markup": reply_markup})

    user = SimpleNamespace(
        id=101,
        first_name="Operator",
        full_name="Operator",
        username="operator",
        language_code="ru",
    )
    bot_user = SimpleNamespace(id=9001, full_name="Aimash")
    message = SimpleNamespace(
        message_id=404,
        message_thread_id=7,
        chat_id=-202,
        chat=SimpleNamespace(id=-202, type="supergroup", title="Ops"),
        text=card,
        caption=None,
        from_user=bot_user,
        date=datetime.now(timezone.utc),
    )
    query = SimpleNamespace(
        data=f"am:yes:{marker}",
        message=message,
        from_user=user,
        answer=_answer,
        edit_message_reply_markup=_remove_markup,
    )
    update = SimpleNamespace(update_id=505, callback_query=query)

    await adapter._handle_callback_query(update, None)

    assert answers == ["✅ Подтверждение принято"]
    assert len(sent_events) == 1
    event = sent_events[0]
    assert event.text == "да"
    assert event.reply_to_message_id == "404"
    assert event.reply_to_is_own_message is True
    plugin._capture_gateway_event(event=event)
    env["HERMES_SESSION_MESSAGE_ID"] = "505"
    args = {}
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__execute_confirmed",
            args=args,
            session_id="s-button",
            turn_id="t-button",
        )
        is None
    )
    turn = verify_turn_token(
        args["trusted_turn_token"], expected_tool="execute_confirmed", tool_args={}
    )
    assert turn.reply_to_message_id == 404
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


def test_guarded_local_skill_reads_do_not_taint_ads_proposal(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    for tool_name in ("skills_list", "skill_view", "clarify", "todo"):
        assert (
            plugin._pre_tool_call(
                tool_name=tool_name,
                args={},
                session_id="s-safe-skill",
                turn_id="t-safe-skill",
            )
            is None
        )

    args = {"account": "7753643025", "campaign": "Доставка цветов"}
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__propose_pause_campaign",
            args=args,
            session_id="s-safe-skill",
            turn_id="t-safe-skill",
        )
        is None
    )
    verify_turn_token(
        args["trusted_turn_token"],
        expected_tool="propose_pause_campaign",
        tool_args={"account": "7753643025", "campaign": "Доставка цветов"},
    )


def test_skill_write_still_taints_ads_proposal(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    assert (
        plugin._pre_tool_call(
            tool_name="skill_manage",
            args={"action": "create"},
            session_id="s-skill-write",
            turn_id="t-skill-write",
        )
        is None
    )
    blocked = plugin._pre_tool_call(
        tool_name="mcp__aimash__propose_pause_campaign",
        args={"account": "7753643025", "campaign": "X"},
        session_id="s-skill-write",
        turn_id="t-skill-write",
    )
    assert blocked["action"] == "block"


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
