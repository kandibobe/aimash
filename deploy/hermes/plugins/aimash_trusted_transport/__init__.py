"""Trusted Telegram → Aimash MCP bridge for pinned Hermes v0.19.0.

This plugin has no Google/DB credentials.  It records the current normalized Telegram event,
correlates it with the task-local gateway session in ``pre_tool_call`` and overwrites one opaque
HMAC token after model argument generation.  The MCP server independently verifies the token.

Hermes plugin errors are fail-open, so this hook is deliberately *not* the final gate.  Missing or
invalid tokens are rejected by ``mcp_server.trusted_transport`` before proposal creation or SDK use.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

_TOKEN_PARAM = "trusted_turn_token"
_TOKEN_VERSION = 1
_TOKEN_TTL_S = 120
_MAX_EVENTS = 2048
_EVENT_TTL_S = 600
_MCP_PREFIX = "mcp__aimash__"
_EXECUTE_TOOL = f"{_MCP_PREFIX}execute_confirmed"
_PROPOSE_PREFIX = f"{_MCP_PREFIX}propose_"
_PLAN_STATE_TOOLS = frozenset(
    {
        f"{_MCP_PREFIX}list_pending_proposals",
        f"{_MCP_PREFIX}cancel_proposal",
    }
)
_TAINTED_AIMASH_TOOLS = frozenset({f"{_MCP_PREFIX}recall_client"})
_CONFIRM_RE = re.compile(r"\bAIMASH_CONFIRM:([0-9a-fA-F]{32})\b")
_CALLBACK_RE = re.compile(r"^am:(yes|edit|no):([0-9a-fA-F]{32})$")
_SAFE_NATIVE_TOOLS = frozenset({"clarify"})


@dataclass(frozen=True, slots=True)
class _Inbound:
    captured_at: float
    actor_user_id: int
    actor_chat_id: int
    actor_username: str | None
    chat_type: str
    thread_id: str | None
    message_id: int
    language_code: str
    reply_to_message_id: int | None
    reply_to_is_own_message: bool
    reply_confirmation_id: str | None
    reply_to_text: str | None


_lock = threading.RLock()
_events: dict[tuple[str, int, int], _Inbound] = {}
_turn_phase: dict[tuple[str, str], str] = {}


def _as_int(value, *, positive: bool = False):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if positive and parsed <= 0:
        return None
    return parsed


def _platform_name(value) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _optional_text(value, *, limit: int = 128) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    return text[:limit] or None


def _reply_is_from_this_bot(raw_message, event) -> bool:
    if bool(getattr(event, "reply_to_is_own_message", False)):
        return True
    reply = getattr(raw_message, "reply_to_message", None)
    reply_user = getattr(reply, "from_user", None)
    reply_user_id = _as_int(getattr(reply_user, "id", None), positive=True)
    if reply_user_id is None:
        return False
    try:
        bot = raw_message.get_bot()
    except Exception:  # noqa: BLE001 - missing PTB binding means fail closed
        bot = None
    bot_id = _as_int(getattr(bot, "id", None), positive=True)
    return bot_id is not None and hmac.compare_digest(str(reply_user_id), str(bot_id))


def _extract_reply_confirmation_id(event) -> str | None:
    text = str(getattr(event, "reply_to_text", None) or "")
    match = _CONFIRM_RE.search(text)
    return match.group(1).lower() if match else None


def _event_key(platform: str, chat_id: int, message_id: int) -> tuple[str, int, int]:
    return (platform, chat_id, message_id)


def _prune(now: float) -> None:
    stale = [key for key, value in _events.items() if now - value.captured_at > _EVENT_TTL_S]
    for key in stale:
        _events.pop(key, None)
    while len(_events) > _MAX_EVENTS:
        _events.pop(next(iter(_events)), None)
    while len(_turn_phase) > _MAX_EVENTS:
        _turn_phase.pop(next(iter(_turn_phase)), None)


def _capture_gateway_event(*args, **kwargs) -> None:
    event = kwargs.get("event") or (args[0] if args else None)
    source = getattr(event, "source", None)
    platform = _platform_name(getattr(source, "platform", None))
    actor_user_id = _as_int(getattr(source, "user_id", None), positive=True)
    actor_chat_id = _as_int(getattr(source, "chat_id", None))
    message_id = _as_int(getattr(event, "message_id", None), positive=True)
    if platform != "telegram" or None in (actor_user_id, actor_chat_id, message_id):
        return

    raw = getattr(event, "raw_message", None)
    from_user = getattr(raw, "from_user", None)
    language_code = str(getattr(from_user, "language_code", None) or "ru")[:8]
    actor_username = getattr(from_user, "username", None) or getattr(source, "user_name", None)
    inbound = _Inbound(
        captured_at=time.time(),
        actor_user_id=actor_user_id,
        actor_chat_id=actor_chat_id,
        actor_username=str(actor_username)[:128] if actor_username else None,
        chat_type=_platform_name(getattr(source, "chat_type", None)),
        thread_id=_optional_text(getattr(source, "thread_id", None)),
        message_id=message_id,
        language_code=language_code,
        reply_to_message_id=_as_int(getattr(event, "reply_to_message_id", None), positive=True),
        reply_to_is_own_message=_reply_is_from_this_bot(raw, event),
        reply_confirmation_id=_extract_reply_confirmation_id(event),
        reply_to_text=_optional_text(getattr(event, "reply_to_text", None), limit=8_000),
    )
    with _lock:
        _prune(inbound.captured_at)
        _events[_event_key(platform, actor_chat_id, message_id)] = inbound


def _session_value(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "")
    except Exception:  # noqa: BLE001 - absence must become a block, not fallback identity
        return ""


def _current_inbound() -> _Inbound | None:
    platform = _session_value("HERMES_SESSION_PLATFORM").lower()
    chat_id = _as_int(_session_value("HERMES_SESSION_CHAT_ID"))
    message_id = _as_int(_session_value("HERMES_SESSION_MESSAGE_ID"), positive=True)
    user_id = _as_int(_session_value("HERMES_SESSION_USER_ID"), positive=True)
    if platform != "telegram" or None in (chat_id, message_id, user_id):
        return None
    with _lock:
        inbound = _events.get(_event_key(platform, chat_id, message_id))
    if inbound is None or inbound.actor_user_id != user_id:
        return None
    if time.time() - inbound.captured_at > _EVENT_TTL_S:
        return None
    return inbound


def _canonical_digest(args: dict) -> str:
    clean = {str(k): v for k, v in args.items() if str(k) != _TOKEN_PARAM}
    encoded = json.dumps(
        clean,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _signed_token(tool_name: str, tool_args: dict, inbound: _Inbound) -> str | None:
    key = os.environ.get("AIMASH_TRUST_HMAC_KEY", "").encode("utf-8")
    if len(key) < 32:
        return None
    now = int(time.time())
    payload = {
        "v": _TOKEN_VERSION,
        "iat": now,
        "exp": now + _TOKEN_TTL_S,
        "platform": "telegram",
        "tool": tool_name,
        "args_sha256": _canonical_digest(tool_args),
        "nonce": secrets.token_hex(16),
        "actor_user_id": inbound.actor_user_id,
        "actor_chat_id": inbound.actor_chat_id,
        "actor_username": inbound.actor_username,
        "chat_type": inbound.chat_type,
        "thread_id": inbound.thread_id,
        "message_id": inbound.message_id,
        "language_code": inbound.language_code,
        "reply_to_message_id": inbound.reply_to_message_id,
        "reply_to_is_own_message": inbound.reply_to_is_own_message,
        "reply_confirmation_id": inbound.reply_confirmation_id,
        "reply_to_text": inbound.reply_to_text,
    }
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    signature = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    return f"{_b64url(payload_bytes)}.{_b64url(signature)}"


def _is_aimash_write(tool_name: str) -> bool:
    return (
        tool_name == _EXECUTE_TOOL
        or tool_name.startswith(_PROPOSE_PREFIX)
        or tool_name in _PLAN_STATE_TOOLS
    )


async def _attach_confirmation_keyboard(adapter, chat_id, message_id, content: str) -> None:
    """Attach UX-only buttons; a failure leaves the secure reply fallback intact."""
    match = _CONFIRM_RE.search(str(content or ""))
    if match is None or not message_id or getattr(adapter, "_bot", None) is None:
        return
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from plugins.platforms.telegram.adapter import normalize_telegram_chat_id

        confirmation_id = match.group(1).lower()
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Подтвердить", callback_data=f"am:yes:{confirmation_id}"
                    ),
                    InlineKeyboardButton("✏️ Изменить", callback_data=f"am:edit:{confirmation_id}"),
                ],
                [InlineKeyboardButton("❌ Отмена", callback_data=f"am:no:{confirmation_id}")],
            ]
        )
        await adapter._bot.edit_message_reply_markup(
            chat_id=normalize_telegram_chat_id(chat_id),
            message_id=int(message_id),
            reply_markup=keyboard,
        )
    except Exception as exc:  # noqa: BLE001 - reply fallback must remain available
        log.warning("Aimash confirmation keyboard was not attached: %s", type(exc).__name__)


def _telegram_chat_type(chat) -> str:
    value = str(getattr(chat, "type", "")).split(".")[-1].lower()
    if value in {"group", "supergroup"}:
        return "group"
    if value == "channel":
        return "channel"
    return "dm"


async def _dispatch_confirmation_callback(adapter, update, query, choice: str) -> bool:
    """Turn a Telegram button tap into the same trusted reply event as typed text."""
    message = getattr(query, "message", None)
    user = getattr(query, "from_user", None)
    chat = getattr(message, "chat", None)
    update_id = _as_int(getattr(update, "update_id", None), positive=True)
    message_id = _as_int(getattr(message, "message_id", None), positive=True)
    chat_id = _as_int(getattr(message, "chat_id", None))
    user_id = _as_int(getattr(user, "id", None), positive=True)
    card_text = str(getattr(message, "text", None) or getattr(message, "caption", None) or "")
    marker = _CONFIRM_RE.search(card_text)
    if None in (update_id, message_id, chat_id, user_id) or marker is None:
        await query.answer(text="⚠️ Карточка устарела — создайте новый черновик.")
        return True

    data_match = _CALLBACK_RE.fullmatch(str(getattr(query, "data", "")))
    if data_match is None or not hmac.compare_digest(
        marker.group(1).lower(), data_match.group(2).lower()
    ):
        await query.answer(text="⛔ Кнопка не соответствует карточке.")
        return True

    if not adapter._is_callback_user_authorized(
        str(user_id),
        chat_id=chat_id,
        chat_type=str(getattr(chat, "type", None)),
        thread_id=str(getattr(message, "message_thread_id", None))
        if getattr(message, "message_thread_id", None) is not None
        else None,
        user_name=getattr(user, "first_name", None),
    ):
        await query.answer(text="⛔ У вас нет права подтверждать этот черновик.")
        return True

    from gateway.platforms.base import MessageEvent, MessageType

    thread_id = getattr(message, "message_thread_id", None)
    source = adapter.build_source(
        chat_id=str(chat_id),
        chat_name=getattr(chat, "title", None) or getattr(chat, "full_name", None),
        chat_type=_telegram_chat_type(chat),
        user_id=str(user_id),
        user_name=getattr(user, "full_name", None) or getattr(user, "first_name", None),
        thread_id=str(thread_id) if thread_id is not None else None,
        message_id=str(update_id),
        is_bot=False,
    )
    text_by_choice = {
        "yes": "да",
        "edit": "хочу изменить этот черновик",
        "no": "нет, отмени этот черновик",
    }
    event = MessageEvent(
        text=text_by_choice[choice],
        message_type=MessageType.TEXT,
        source=source,
        raw_message=query,
        message_id=str(update_id),
        platform_update_id=update_id,
        reply_to_message_id=str(message_id),
        reply_to_text=card_text,
        reply_to_author_id=str(getattr(getattr(message, "from_user", None), "id", "") or ""),
        reply_to_author_name=getattr(getattr(message, "from_user", None), "full_name", None),
        reply_to_is_own_message=True,
        metadata={"aimash_confirmation_callback": choice},
        timestamp=getattr(message, "date", None),
    )
    await query.answer(
        text={
            "yes": "✅ Подтверждение принято",
            "edit": "✏️ Напишите, что изменить",
            "no": "❌ Черновик отменяется",
        }[choice]
    )
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 - CAS still makes a repeated click harmless
        pass
    await adapter.handle_message(event)
    return True


def _install_telegram_button_bridge() -> bool:
    """Patch the pinned Telegram adapter without adding Ads credentials to Hermes."""
    try:
        from plugins.platforms.telegram.adapter import TelegramAdapter
    except Exception as exc:  # noqa: BLE001 - secure reply flow remains the fallback
        log.warning("Aimash Telegram button bridge unavailable: %s", type(exc).__name__)
        return False
    if getattr(TelegramAdapter, "_aimash_button_bridge", False):
        return True

    original_send = TelegramAdapter.send
    original_edit = TelegramAdapter.edit_message
    original_callback = TelegramAdapter._handle_callback_query

    @functools.wraps(original_send)
    async def send(adapter, chat_id, content, reply_to=None, metadata=None):
        result = await original_send(
            adapter, chat_id, content, reply_to=reply_to, metadata=metadata
        )
        if getattr(result, "success", False):
            await _attach_confirmation_keyboard(
                adapter, chat_id, getattr(result, "message_id", None), content
            )
        return result

    @functools.wraps(original_edit)
    async def edit_message(adapter, chat_id, message_id, content, *, finalize=False, metadata=None):
        result = await original_edit(
            adapter,
            chat_id,
            message_id,
            content,
            finalize=finalize,
            metadata=metadata,
        )
        if finalize and getattr(result, "success", False):
            await _attach_confirmation_keyboard(adapter, chat_id, message_id, content)
        return result

    @functools.wraps(original_callback)
    async def handle_callback(adapter, update, context):
        query = getattr(update, "callback_query", None)
        match = _CALLBACK_RE.fullmatch(str(getattr(query, "data", "") or ""))
        if match is not None:
            await _dispatch_confirmation_callback(adapter, update, query, match.group(1))
            return
        await original_callback(adapter, update, context)

    TelegramAdapter.send = send
    TelegramAdapter.edit_message = edit_message
    TelegramAdapter._handle_callback_query = handle_callback
    TelegramAdapter._aimash_button_bridge = True
    log.info("Aimash Telegram confirmation buttons enabled")
    return True


def _pre_tool_call(*args, **kwargs):
    tool_name = str(kwargs.get("tool_name") or (args[0] if args else ""))
    tool_args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else None
    if tool_args is None and len(args) > 1 and isinstance(args[1], dict):
        tool_args = args[1]
    tool_args = tool_args if isinstance(tool_args, dict) else {}
    session_id = str(kwargs.get("session_id") or "")
    turn_id = str(kwargs.get("turn_id") or "")
    turn_key = (session_id, turn_id)

    if _is_aimash_write(tool_name):
        if not session_id or not turn_id:
            return {"action": "block", "message": "Aimash WRITE: нет доверенной корреляции хода"}
        inbound = _current_inbound()
        if inbound is None:
            return {"action": "block", "message": "Aimash WRITE: нет trusted Telegram event"}
        if tool_name == _EXECUTE_TOOL and not (
            inbound.reply_to_is_own_message
            and inbound.reply_to_message_id is not None
            and inbound.reply_confirmation_id is not None
        ):
            return {
                "action": "block",
                "message": "Aimash WRITE: подтверждение должно быть реплаем на карточку с кодом",
            }
        with _lock:
            phase = _turn_phase.get(turn_key, "clean")
            if phase == "external":
                return {
                    "action": "block",
                    "message": "Aimash WRITE недоступен в ходе с внешним контентом (И7)",
                }
            _turn_phase[turn_key] = "write"
            _prune(time.time())
        token = _signed_token(tool_name, tool_args, inbound)
        if token is None:
            return {"action": "block", "message": "Aimash trusted transport не настроен"}
        # Model-supplied values are never honored: overwrite after hashing ordinary args.
        tool_args[_TOKEN_PARAM] = token
        return None

    if (
        tool_name.startswith(_MCP_PREFIX) and tool_name not in _TAINTED_AIMASH_TOOLS
    ) or tool_name in _SAFE_NATIVE_TOOLS:
        return None

    # Any other tool is an external/host surface for the purposes of И7.  A lock gives sequential
    # semantics even when the provider emits parallel calls: either external wins and WRITE blocks,
    # or WRITE wins and the external call is blocked before it can influence the proposal.
    if session_id and turn_id:
        with _lock:
            phase = _turn_phase.get(turn_key, "clean")
            if phase == "write":
                return {
                    "action": "block",
                    "message": "Внешний инструмент недоступен после PLAN/WRITE в этом ходе (И7)",
                }
            _turn_phase[turn_key] = "external"
            _prune(time.time())
    return None


def register(ctx):  # noqa: ANN001 - runtime-owned plugin API
    _install_telegram_button_bridge()
    ctx.register_hook("pre_gateway_dispatch", _capture_gateway_event)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    if len(os.environ.get("AIMASH_TRUST_HMAC_KEY", "").encode("utf-8")) < 32:
        log.warning("aimash_trusted_transport loaded without a valid signing key; WRITE will block")
    else:
        log.info("aimash_trusted_transport loaded (fail-closed MCP verification enabled)")
