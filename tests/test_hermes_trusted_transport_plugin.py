"""Pinned Hermes hook behavior: trusted event correlation without an external-content phase lock."""

from __future__ import annotations

import importlib.util
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
import sys
import types
import uuid
import time
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
    telegram = types.ModuleType("telegram")

    class _InputFile:
        def __init__(self, stream, *, filename: str):
            self.stream = stream
            self.filename = filename

    telegram.InputFile = _InputFile
    session_context.get_session_env = lambda name, default="": env.get(name, default)
    gateway.session_context = session_context
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    monkeypatch.setitem(sys.modules, "telegram", telegram)
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


def _artifact_token(*, content: bytes = b"xlsx") -> str:
    now = int(time.time())
    payload = {
        "v": 1,
        "iat": now,
        "exp": now + 900,
        "container": "aimash-mcp",
        "path": "/tmp/aimash_artifacts/" + "a" * 32 + ".xlsx",
        "filename": "report.xlsx",
        "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "nonce": "b" * 32,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    sig = hmac.new(KEY.encode(), raw, hashlib.sha256).digest()

    def enc(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    return f"{enc(raw)}.{enc(sig)}"


def test_plugin_overwrites_model_token_with_verified_event(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    args = {"account": "7753643025", "campaign": "X", "trusted_turn_token": "model-forgery"}

    result = plugin._pre_tool_call(
        tool_name="mcp__aimash__pause_campaign",
        args=args,
        session_id="s1",
        turn_id="t1",
    )

    assert result is None
    assert args["trusted_turn_token"] != "model-forgery"
    turn = verify_turn_token(
        args["trusted_turn_token"],
        expected_tool="pause_campaign",
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


def test_register_resolves_deferred_telegram_plugin_before_patching(monkeypatch):
    plugin = _load(monkeypatch, _env())
    activated = []

    class _Adapter:
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return None

        async def edit_message(
            self, chat_id, message_id, content, *, finalize=False, metadata=None
        ):
            return None

        async def _handle_callback_query(self, update, context):
            return None

    def resolve(name):
        assert name == "telegram"
        activated.append(name)
        adapter_module = types.ModuleType("hermes_plugins.telegram_platform.adapter")
        adapter_module.TelegramAdapter = _Adapter
        adapter_module.normalize_telegram_chat_id = int
        monkeypatch.setitem(sys.modules, "hermes_plugins", types.ModuleType("hermes_plugins"))
        monkeypatch.setitem(
            sys.modules,
            "hermes_plugins.telegram_platform",
            types.ModuleType("hermes_plugins.telegram_platform"),
        )
        monkeypatch.setitem(sys.modules, "hermes_plugins.telegram_platform.adapter", adapter_module)
        return SimpleNamespace(name="telegram")

    platform_registry = types.ModuleType("gateway.platform_registry")
    platform_registry.platform_registry = SimpleNamespace(get=resolve)
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", platform_registry)
    for name in (
        "hermes_plugins",
        "hermes_plugins.telegram_platform",
        "hermes_plugins.telegram_platform.adapter",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    hooks = []
    plugin.register(SimpleNamespace(register_hook=lambda name, fn: hooks.append((name, fn))))

    assert activated == ["telegram"]
    assert _Adapter._aimash_button_bridge is True
    assert plugin._button_bridge_ready is True
    assert {name for name, _ in hooks} == {
        "pre_gateway_dispatch",
        "pre_tool_call",
        "post_tool_call",
        "transform_tool_result",
    }


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

        def _should_attempt_rich(self, content, metadata=None):
            return True

        def _is_callback_user_authorized(self, *args, **kwargs):
            return True

        def build_source(self, **kwargs):
            return SimpleNamespace(platform="telegram", **kwargs)

        async def handle_message(self, event):
            sent_events.append(event)

    # Pinned Hermes loads its bundled Telegram plugin under this synthetic namespace. Regressing to
    # ``plugins.platforms...`` patches a duplicate class and leaves the live gateway untouched.
    adapter_module = types.ModuleType("hermes_plugins.telegram_platform.adapter")
    adapter_module.TelegramAdapter = _Adapter
    adapter_module.normalize_telegram_chat_id = int
    hermes_plugins = types.ModuleType("hermes_plugins")
    telegram_platform = types.ModuleType("hermes_plugins.telegram_platform")
    monkeypatch.setitem(sys.modules, "hermes_plugins", hermes_plugins)
    monkeypatch.setitem(sys.modules, "hermes_plugins.telegram_platform", telegram_platform)
    monkeypatch.setitem(sys.modules, "hermes_plugins.telegram_platform.adapter", adapter_module)

    assert plugin._install_telegram_button_bridge() is True
    adapter = _Adapter()
    marker = "a" * 32
    card = f"🧾 Черновик изменения\n\npreview\n\nAIMASH_CONFIRM:{marker}"
    await adapter.send("-202", card, metadata={"notify": True})
    keyboard = attached[0]["reply_markup"].inline_keyboard
    assert [button.text for row in keyboard for button in row] == [
        "✅ Да",
        "❌ Нет",
    ]
    assert adapter._should_attempt_rich(card, metadata={"notify": True}) is False
    assert adapter._should_attempt_rich("обычный отчёт", metadata={"notify": True}) is True

    delivered = []

    async def _deliver(adapter_arg, chat_id, metadata):
        delivered.append((adapter_arg, chat_id, metadata))

    monkeypatch.setattr(plugin, "_deliver_pending_artifacts", _deliver)
    await adapter.send(
        "-202",
        "обычный итог без notify",
        metadata={"thread_id": "7"},
    )
    await adapter.edit_message(
        "-202",
        "303",
        card,
        finalize=True,
        metadata={"thread_id": "7"},
    )
    assert attached[1]["message_id"] == 404
    assert delivered == [
        (adapter, "-202", {"thread_id": "7"}),
        (adapter, "-202", {"thread_id": "7"}),
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


async def test_text_button_marker_is_cleaned_and_attached_as_keyboard(monkeypatch):
    plugin = _load(monkeypatch, _env())

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
    sent = []
    attached = []
    events = []

    class _MessageEvent(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    base = types.ModuleType("gateway.platforms.base")
    base.MessageEvent = _MessageEvent
    base.MessageType = SimpleNamespace(TEXT="text")
    monkeypatch.setitem(sys.modules, "gateway.platforms", types.ModuleType("gateway.platforms"))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base)

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
            sent.append(content)
            return _Result()

        async def edit_message(
            self, chat_id, message_id, content, *, finalize=False, metadata=None
        ):
            return _Result()

        async def _handle_callback_query(self, update, context):
            return None

        def _is_callback_user_authorized(self, *args, **kwargs):
            return True

        def build_source(self, **kwargs):
            return SimpleNamespace(platform="telegram", **kwargs)

        async def handle_message(self, event):
            events.append(event)

    adapter_module = types.ModuleType("hermes_plugins.telegram_platform.adapter")
    adapter_module.TelegramAdapter = _Adapter
    adapter_module.normalize_telegram_chat_id = int
    monkeypatch.setitem(sys.modules, "hermes_plugins", types.ModuleType("hermes_plugins"))
    monkeypatch.setitem(
        sys.modules,
        "hermes_plugins.telegram_platform",
        types.ModuleType("hermes_plugins.telegram_platform"),
    )
    monkeypatch.setitem(sys.modules, "hermes_plugins.telegram_platform.adapter", adapter_module)

    assert plugin._install_telegram_button_bridge() is True
    adapter = _Adapter()
    await adapter.send("-202", "Выберите действие:\n[Кнопка: Найти неэффективные ключи]")

    assert sent == ["Выберите действие:"]
    keyboard = attached[0]["reply_markup"].inline_keyboard
    assert [[button.text for button in row] for row in keyboard] == [["Найти неэффективные ключи"]]
    query = SimpleNamespace(
        data="ab:0",
        from_user=SimpleNamespace(id=101, first_name="Operator", full_name="Operator"),
        message=SimpleNamespace(
            message_id=404,
            chat_id=-202,
            chat=SimpleNamespace(type="private", full_name="Operator", title=None),
            message_thread_id=None,
            text="Выберите действие:",
            from_user=SimpleNamespace(id=9001, full_name="Aimash"),
            date=None,
        ),
        answer=lambda **kwargs: None,
        edit_message_reply_markup=lambda **kwargs: None,
    )

    async def _noop(**kwargs):
        return None

    query.answer = _noop
    query.edit_message_reply_markup = _noop
    await adapter._handle_callback_query(SimpleNamespace(update_id=505, callback_query=query), None)
    assert events[0].text == "Найти неэффективные ключи"


def test_external_tool_does_not_block_private_operator_write(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    assert (
        plugin._pre_tool_call(tool_name="web_search", args={}, session_id="s4", turn_id="t4")
        is None
    )
    args_after_web = {"account": "7753643025", "campaign": "X"}
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__pause_campaign",
            args=args_after_web,
            session_id="s4",
            turn_id="t4",
        )
        is None
    )
    verify_turn_token(
        args_after_web["trusted_turn_token"],
        expected_tool="pause_campaign",
        tool_args={"account": "7753643025", "campaign": "X"},
    )

    args = {"account": "7753643025", "campaign": "Y"}
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__pause_campaign",
            args=args,
            session_id="s5",
            turn_id="t5",
        )
        is None
    )
    assert (
        plugin._pre_tool_call(tool_name="web_search", args={}, session_id="s5", turn_id="t5")
        is None
    )


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
            tool_name="mcp__aimash__pause_campaign",
            args=args,
            session_id="s-safe-skill",
            turn_id="t-safe-skill",
        )
        is None
    )
    verify_turn_token(
        args["trusted_turn_token"],
        expected_tool="pause_campaign",
        tool_args={"account": "7753643025", "campaign": "Доставка цветов"},
    )


def test_skill_write_does_not_phase_lock_private_operator_proposal(monkeypatch):
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
    args = {"account": "7753643025", "campaign": "X"}
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__pause_campaign",
            args=args,
            session_id="s-skill-write",
            turn_id="t-skill-write",
        )
        is None
    )
    assert "trusted_turn_token" in args


def test_recall_client_does_not_phase_lock_private_operator_proposal(monkeypatch):
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
    args = {"account": "7753643025", "campaign": "X"}
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__pause_campaign",
            args=args,
            session_id="s6",
            turn_id="t6",
        )
        is None
    )
    assert "trusted_turn_token" in args


def test_signed_artifact_is_queued_for_exact_topic_and_hidden_from_model(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    token = _artifact_token()
    result = json.dumps(
        {
            "artifact": {
                "filename": "report.xlsx",
                "token": token,
                "marker": f"AIMASH_ARTIFACT:{token}",
            }
        }
    )

    plugin._post_tool_call(tool_name="mcp__aimash__build_report", result=result)
    transformed = plugin._transform_tool_result(
        tool_name="mcp__aimash__build_report", result=result
    )

    assert plugin._pending_artifacts[(-202, "7")] == [token]
    assert token not in transformed
    assert "report.xlsx" in transformed


async def test_verified_artifact_is_requeued_after_transient_delivery_failure(monkeypatch):
    plugin = _load(monkeypatch, _env())
    token = _artifact_token()
    plugin._pending_artifacts[(-202, "7")] = [token]

    def fail_copy(artifact, target):
        raise RuntimeError("temporary docker copy failure")

    monkeypatch.setattr(plugin, "_copy_artifact", fail_copy)
    scheduled = []
    monkeypatch.setattr(
        plugin,
        "_schedule_artifact_retry",
        lambda adapter, chat_id, metadata, key: scheduled.append(key),
    )
    adapter = SimpleNamespace(_bot=SimpleNamespace())

    await plugin._deliver_pending_artifacts(
        adapter,
        "-202",
        {"notify": True, "thread_id": "7"},
    )

    assert plugin._pending_artifacts[(-202, "7")] == [token]
    assert scheduled == [(-202, "7")]


async def test_artifact_background_retry_recovers_without_new_message(monkeypatch):
    plugin = _load(monkeypatch, _env())
    token = _artifact_token()
    plugin._pending_artifacts[(-202, "7")] = [token]
    attempts = 0
    sent = []

    def flaky_copy(artifact, target):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        target.write_bytes(b"xlsx")

    class _Bot:
        async def send_document(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(plugin, "_copy_artifact", flaky_copy)
    monkeypatch.setattr(plugin, "_remove_container_artifact", lambda artifact: None)
    monkeypatch.setattr(plugin, "_ARTIFACT_RETRY_DELAYS_S", (0,))
    adapter = SimpleNamespace(_bot=_Bot())

    await plugin._deliver_pending_artifacts(
        adapter,
        "-202",
        {"notify": True, "thread_id": "7"},
    )
    retry_task = plugin._artifact_retry_tasks[(-202, "7")]
    await retry_task

    assert attempts == 2
    assert len(sent) == 1
    assert plugin._pending_artifacts.get((-202, "7")) in (None, [])


def test_missing_button_bridge_falls_back_to_semantic_reply(monkeypatch):
    plugin = _load(monkeypatch, _env())
    monkeypatch.setattr(plugin, "_install_telegram_button_bridge", lambda: False)
    hooks = {}
    ctx = SimpleNamespace(register_hook=lambda name, fn: hooks.__setitem__(name, fn))

    plugin.register(ctx)
    assert plugin._button_bridge_ready is False

    plugin._capture_gateway_event(event=_event())
    proposal_args = {"account": "7753643025", "campaign": "X"}
    allowed = plugin._pre_tool_call(
        tool_name="mcp__aimash__pause_campaign",
        args=proposal_args,
        session_id="s-no-buttons",
        turn_id="t-no-buttons",
    )
    assert allowed is None
    assert "trusted_turn_token" in proposal_args

    execute_args = {}
    blocked = plugin._pre_tool_call(
        tool_name="mcp__aimash__execute_confirmed",
        args=execute_args,
        session_id="s-no-buttons",
        turn_id="t-no-buttons",
    )
    assert blocked["action"] == "block"
    assert "trusted_turn_token" not in execute_args

    marker = "a" * 32
    plugin._capture_gateway_event(event=_event(own_reply=True, marker=marker))
    allowed = plugin._pre_tool_call(
        tool_name="mcp__aimash__execute_confirmed",
        args=execute_args,
        session_id="s-no-buttons",
        turn_id="t-semantic-reply",
    )
    assert allowed is None
    turn = verify_turn_token(
        execute_args["trusted_turn_token"], expected_tool="execute_confirmed", tool_args={}
    )
    assert turn.reply_confirmation_id == marker


def test_forged_artifact_is_not_queued(monkeypatch):
    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    token = _artifact_token()[:-1] + "x"

    plugin._post_tool_call(
        tool_name="mcp__aimash__build_report",
        result=json.dumps({"artifact": {"token": token}}),
    )

    assert plugin._pending_artifacts == {}
