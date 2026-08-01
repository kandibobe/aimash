"""Trusted Telegram → Aimash MCP bridge for pinned Hermes v0.19.0.

This plugin has no Google/DB credentials.  It records the current normalized Telegram event,
correlates it with the task-local gateway session in ``pre_tool_call`` and overwrites one opaque
HMAC token after model argument generation.  The MCP server independently verifies the token.

Hermes plugin errors are fail-open, so this hook is deliberately *not* the final gate.  Missing or
invalid tokens are rejected by ``mcp_server.trusted_transport`` before proposal creation or SDK use.
"""

from __future__ import annotations

import base64
import asyncio
import functools
import hashlib
import hmac
import importlib
import json
import logging
import math
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_TOKEN_PARAM = "trusted_turn_token"
_TOKEN_VERSION = 1
_TOKEN_TTL_S = 120
_MAX_EVENTS = 2048
_EVENT_TTL_S = 600
_MCP_PREFIX = "mcp__aimash__"
_EXECUTE_TOOL = f"{_MCP_PREFIX}execute_confirmed"
_ACTION_TOOLS = frozenset(
    f"{_MCP_PREFIX}{name}"
    for name in (
        "update_budget",
        "update_bid",
        "update_keyword_bid",
        "set_bidding_strategy",
        "add_keywords",
        "remove_keywords",
        "add_negative_keywords",
        "remove_negative_keywords",
        "add_negatives_to_shared_set",
        "attach_shared_set",
        "pause_campaign",
        "resume_campaign",
        "launch_campaign",
        "update_campaign",
        "remove_campaign",
        "set_campaign_network",
        "set_campaign_display_network",
        "set_campaign_geo_target_type",
        "pause_ad_group",
        "resume_ad_group",
        "remove_ad_group",
        "pause_ad",
        "resume_ad",
        "remove_ad",
        "set_geo_proximity",
        "set_geo_location",
        "attach_audience",
        "detach_audience",
        "create_rsa",
        "create_search_campaign",
        "create_gdn_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
        "create_app_campaign",
        "add_sitelinks",
        "add_callouts",
        "add_structured_snippets",
        "attach_image_asset",
        "add_call_asset",
        "add_promotion",
        "add_price_asset",
        "remove_asset_link",
    )
)
_PLAN_STATE_TOOLS = frozenset(
    {
        f"{_MCP_PREFIX}list_pending_proposals",
        f"{_MCP_PREFIX}cancel_proposal",
        f"{_MCP_PREFIX}list_decisions",
        f"{_MCP_PREFIX}update_decision",
        f"{_MCP_PREFIX}list_incidents",
        f"{_MCP_PREFIX}update_incident",
        f"{_MCP_PREFIX}start_keyword_research",
        f"{_MCP_PREFIX}read_keyword_sheet",
        f"{_MCP_PREFIX}curation_start",
        f"{_MCP_PREFIX}curation_state",
        f"{_MCP_PREFIX}curation_apply",
        f"{_MCP_PREFIX}curation_finalize",
        f"{_MCP_PREFIX}search_wizard_start",
        f"{_MCP_PREFIX}search_wizard_state",
        f"{_MCP_PREFIX}search_wizard_update",
        f"{_MCP_PREFIX}search_wizard_finalize",
        f"{_MCP_PREFIX}ingest_media",
        f"{_MCP_PREFIX}start_client_crawl",
        f"{_MCP_PREFIX}profile_change",
        f"{_MCP_PREFIX}profile_clear",
    }
)
_INGEST_MEDIA_TOOL = f"{_MCP_PREFIX}ingest_media"
_CONFIRM_RE = re.compile(r"\bAIMASH_CONFIRM:([0-9a-fA-F]{32})\b")
_CALLBACK_RE = re.compile(r"^am:(yes|edit|no):([0-9a-fA-F]{32})$")
_ARTIFACT_MARKER = "AIMASH_ARTIFACT:"
_ARTIFACT_VERSION = 1
_ARTIFACT_MAX_BYTES = 20 * 1024 * 1024
_ARTIFACT_TOKEN_RE = re.compile(r"\bAIMASH_ARTIFACT:([A-Za-z0-9_.-]{40,12000})")
_ARTIFACT_PATH_RE = re.compile(r"^/tmp/aimash_artifacts/[0-9a-f]{32}\.[a-z0-9]{1,8}$")
_ARTIFACT_MIME = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
        "text/plain",
        "application/pdf",
        "image/jpeg",
        "image/png",
        "video/mp4",
    }
)


@dataclass(frozen=True, slots=True)
class _Inbound:
    captured_at: float
    actor_user_id: int
    actor_chat_id: int
    actor_username: str | None
    chat_type: str
    thread_id: str | None
    message_id: int
    message_text: str | None
    language_code: str
    reply_to_message_id: int | None
    reply_to_is_own_message: bool
    reply_confirmation_id: str | None
    reply_to_text: str | None
    media_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Artifact:
    token: str
    container: str
    path: str
    filename: str
    media_type: str
    size: int
    sha256: str
    expires_at: int


_lock = threading.RLock()
_events: dict[tuple[str, int, int], _Inbound] = {}
_pending_artifacts: dict[tuple[int, str | None], list[str]] = {}
_artifact_retry_tasks: dict[tuple[int, str | None], asyncio.Task] = {}
_ARTIFACT_RETRY_DELAYS_S = (1, 3, 10, 30, 60)
_button_bridge_ready: bool | None = None


def _telegram_adapter_module():
    """Return the module that owns the *live* TelegramAdapter class.

    Hermes v0.19 loads bundled plugins under the synthetic ``hermes_plugins`` namespace. Importing
    the source path ``plugins.platforms...`` creates a second module/class object; monkey-patching it
    succeeds but has no effect on the running gateway. Telegram itself is a deferred bundled plugin,
    so resolve its registry entry before importing the synthetic namespace: user plugins are loaded
    earlier during discovery, and otherwise the live adapter does not exist yet. Keep the source-path
    fallback for CLI/tests and older Hermes builds.
    """
    try:
        from gateway.platform_registry import platform_registry

        platform_registry.get("telegram")
    except (ImportError, ModuleNotFoundError):
        pass

    for module_name in (
        "hermes_plugins.telegram_platform.adapter",
        "plugins.platforms.telegram.adapter",
    ):
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue
        if getattr(module, "TelegramAdapter", None) is not None:
            return module
    return None


def _normalize_telegram_chat_id(chat_id):
    module = _telegram_adapter_module()
    normalize = getattr(module, "normalize_telegram_chat_id", None) if module else None
    return normalize(chat_id) if callable(normalize) else chat_id


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
        message_text=_optional_text(getattr(event, "content", None), limit=8_000),
        language_code=language_code,
        reply_to_message_id=_as_int(getattr(event, "reply_to_message_id", None), positive=True),
        reply_to_is_own_message=_reply_is_from_this_bot(raw, event),
        reply_confirmation_id=_extract_reply_confirmation_id(event),
        reply_to_text=_optional_text(getattr(event, "reply_to_text", None), limit=8_000),
        media_urls=tuple(str(item) for item in (getattr(event, "media_urls", None) or []) if item)[
            :10
        ],
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


def _canonical_json_value(value):
    """Match FastMCP's JSON integer -> annotated float coercion before HMAC verification."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, list):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical_json_value(item) for key, item in value.items()}
    return value


def _canonical_digest(args: dict) -> str:
    clean = _canonical_json_value({str(k): v for k, v in args.items() if str(k) != _TOKEN_PARAM})
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


def _b64url_decode(value: str) -> bytes:
    if not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid base64url")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if not hmac.compare_digest(_b64url(decoded), value):
        raise ValueError("non-canonical base64url")
    return decoded


def _verify_artifact_token(token: str, *, now: int | None = None) -> _Artifact:
    """Verify MCP artifact provenance and constrain docker-copy to one temp directory."""
    if not isinstance(token, str) or len(token) > 12_000:
        raise ValueError("invalid artifact token")
    payload_part, signature_part = token.split(".", 1)
    payload_bytes = _b64url_decode(payload_part)
    supplied = _b64url_decode(signature_part)
    key = os.environ.get("AIMASH_TRUST_HMAC_KEY", "").encode("utf-8")
    if len(key) < 32:
        raise ValueError("artifact signing key unavailable")
    expected = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("artifact signature mismatch")
    payload = json.loads(payload_bytes.decode("utf-8"))
    current = int(time.time()) if now is None else int(now)
    if not isinstance(payload, dict) or payload.get("v") != _ARTIFACT_VERSION:
        raise ValueError("artifact version mismatch")
    issued_at = int(payload.get("iat") or 0)
    expires_at = int(payload.get("exp") or 0)
    if issued_at <= 0 or expires_at <= issued_at or expires_at < current:
        raise ValueError("artifact expired")
    if expires_at - issued_at > 15 * 60 + 5:
        raise ValueError("artifact lifetime invalid")
    path = str(payload.get("path") or "")
    filename = Path(str(payload.get("filename") or "")).name
    media_type = str(payload.get("media_type") or "")
    size = int(payload.get("size") or 0)
    digest = str(payload.get("sha256") or "")
    if payload.get("container") != "aimash-mcp" or not _ARTIFACT_PATH_RE.fullmatch(path):
        raise ValueError("artifact source invalid")
    if not filename or filename != str(payload.get("filename") or "") or len(filename) > 120:
        raise ValueError("artifact filename invalid")
    if media_type not in _ARTIFACT_MIME or not 0 < size <= _ARTIFACT_MAX_BYTES:
        raise ValueError("artifact type or size invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("artifact digest invalid")
    return _Artifact(
        token=token,
        container="aimash-mcp",
        path=path,
        filename=filename,
        media_type=media_type,
        size=size,
        sha256=digest,
        expires_at=expires_at,
    )


def _artifact_tokens(value) -> list[str]:
    """Extract only valid signed tokens from one MCP result."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if parsed is not None:
            return _artifact_tokens(parsed)
        candidates = [match.group(1) for match in _ARTIFACT_TOKEN_RE.finditer(value)]
    elif isinstance(value, dict):
        candidates = []
        token = value.get("token")
        if isinstance(token, str):
            candidates.append(token)
        for item in value.values():
            candidates.extend(_artifact_tokens(item))
    elif isinstance(value, list):
        candidates = [token for item in value for token in _artifact_tokens(item)]
    else:
        candidates = []
    valid: list[str] = []
    for token in candidates:
        try:
            _verify_artifact_token(token)
        except Exception:  # noqa: BLE001 - forged/expired descriptors are ignored fail-closed
            continue
        if token not in valid:
            valid.append(token)
    return valid


def _post_tool_call(*args, **kwargs) -> None:
    tool_name = str(kwargs.get("tool_name") or (args[0] if args else ""))
    if not tool_name.startswith(_MCP_PREFIX):
        return
    tokens = _artifact_tokens(kwargs.get("result"))
    inbound = _current_inbound()
    if not tokens or inbound is None:
        return
    key = (inbound.actor_chat_id, inbound.thread_id)
    with _lock:
        queued = _pending_artifacts.setdefault(key, [])
        for token in tokens:
            if token not in queued:
                queued.append(token)


def _strip_artifact_secrets(value):
    """Remove transport tokens before the tool result is appended to model context."""
    if isinstance(value, dict):
        return {
            key: _strip_artifact_secrets(item)
            for key, item in value.items()
            if key not in {"token", "marker"}
        }
    if isinstance(value, list):
        return [_strip_artifact_secrets(item) for item in value]
    if isinstance(value, str):
        return _ARTIFACT_TOKEN_RE.sub("[artifact queued for Telegram delivery]", value)
    return value


def _transform_tool_result(*args, **kwargs):
    result = kwargs.get("result") if "result" in kwargs else (args[0] if args else None)
    if not _artifact_tokens(result):
        return None
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
        return json.dumps(_strip_artifact_secrets(parsed), ensure_ascii=False)
    except Exception:  # noqa: BLE001 - fail-open hook; original result remains available
        return None


def _copy_artifact(artifact: _Artifact, destination: Path) -> None:
    """Copy by argv (no shell), then verify exact size and digest before Telegram sees it."""
    subprocess.run(
        ["docker", "cp", f"{artifact.container}:{artifact.path}", str(destination)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    if destination.stat().st_size != artifact.size:
        raise ValueError("artifact size changed during delivery")
    if hashlib.sha256(destination.read_bytes()).hexdigest() != artifact.sha256:
        raise ValueError("artifact digest changed during delivery")


def _remove_container_artifact(artifact: _Artifact) -> None:
    subprocess.run(
        ["docker", "exec", artifact.container, "rm", "-f", "--", artifact.path],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )


async def _artifact_retry_worker(adapter, chat_id, metadata, key) -> None:
    """Retry transient Telegram/docker failures without waiting for another user message."""

    try:
        for delay in _ARTIFACT_RETRY_DELAYS_S:
            await asyncio.sleep(delay)
            await _deliver_pending_artifacts(
                adapter,
                chat_id,
                metadata,
                schedule_retry=False,
            )
            with _lock:
                if not _pending_artifacts.get(key):
                    return
        log.error("Aimash artifact delivery exhausted bounded retries")
    finally:
        with _lock:
            _artifact_retry_tasks.pop(key, None)


def _schedule_artifact_retry(adapter, chat_id, metadata, key) -> None:
    with _lock:
        task = _artifact_retry_tasks.get(key)
        if task is not None and not task.done():
            return
        _artifact_retry_tasks[key] = asyncio.create_task(
            _artifact_retry_worker(adapter, chat_id, dict(metadata or {}), key),
            name=f"aimash-artifact-retry-{key[0]}-{key[1] or 'root'}",
        )


async def _deliver_pending_artifacts(
    adapter, chat_id, metadata=None, *, schedule_retry: bool = True
) -> None:
    parsed_chat = _as_int(chat_id)
    if parsed_chat is None or getattr(adapter, "_bot", None) is None:
        return
    thread_id = _optional_text((metadata or {}).get("thread_id"))
    exact = (parsed_chat, thread_id)
    with _lock:
        tokens = _pending_artifacts.pop(exact, [])
        if not tokens:
            matches = [key for key in _pending_artifacts if key[0] == parsed_chat]
            if len(matches) == 1:
                tokens = _pending_artifacts.pop(matches[0], [])
                thread_id = matches[0][1]
    if not tokens:
        return
    from telegram import InputFile

    retry_tokens: list[str] = []
    for token in tokens:
        artifact = None
        try:
            artifact = _verify_artifact_token(token)
            with tempfile.TemporaryDirectory(prefix="aimash_artifact_") as tmp:
                target = Path(tmp) / artifact.filename
                await asyncio.to_thread(_copy_artifact, artifact, target)
                kwargs = {
                    "chat_id": _normalize_telegram_chat_id(chat_id),
                    "caption": f"📎 {artifact.filename}",
                }
                if thread_id is not None:
                    kwargs["message_thread_id"] = int(thread_id)
                with target.open("rb") as stream:
                    media = InputFile(stream, filename=artifact.filename)
                    if artifact.media_type.startswith("image/"):
                        await adapter._bot.send_photo(photo=media, **kwargs)
                    elif artifact.media_type == "video/mp4":
                        await adapter._bot.send_video(video=media, **kwargs)
                    else:
                        await adapter._bot.send_document(document=media, **kwargs)
            await asyncio.to_thread(_remove_container_artifact, artifact)
        except Exception as exc:  # noqa: BLE001 - text response survives failed attachment
            log.warning("Aimash artifact delivery failed: %s", type(exc).__name__)
            # A verified artifact remains in the container until Telegram accepts it. Preserve its
            # signed token so a later final response in the same topic can retry transient copy/API
            # failures instead of turning "queued" into a silent permanent loss.
            if artifact is not None:
                retry_tokens.append(token)
    if retry_tokens:
        retry_key = (parsed_chat, thread_id)
        with _lock:
            queued = _pending_artifacts.setdefault(retry_key, [])
            _pending_artifacts[retry_key] = retry_tokens + [
                token for token in queued if token not in retry_tokens
            ]
        if schedule_retry:
            _schedule_artifact_retry(adapter, chat_id, metadata, retry_key)


_INBOUND_MEDIA_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"})
_INBOUND_CONTAINER_RE = re.compile(
    r"^/tmp/aimash_inbound/[0-9a-f]{32}\.(?:jpg|jpeg|png|webp|mp4|mov)$"
)


def _copy_trusted_inbound_media(inbound: _Inbound) -> list[dict]:
    """Copy gateway-resolved media into MCP container without accepting a model path."""
    copied: list[dict] = []
    for raw in inbound.media_urls:
        source = Path(raw)
        try:
            if source.is_symlink():
                continue
            resolved = source.resolve(strict=True)
            suffix = resolved.suffix.lower()
            size = resolved.stat().st_size
            if not resolved.is_file() or suffix not in _INBOUND_MEDIA_SUFFIXES:
                continue
            if not 0 < size <= _ARTIFACT_MAX_BYTES:
                continue
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            destination = f"/tmp/aimash_inbound/{secrets.token_hex(16)}{suffix}"
            if not _INBOUND_CONTAINER_RE.fullmatch(destination):
                continue
            subprocess.run(
                ["docker", "exec", "aimash-mcp", "mkdir", "-p", "/tmp/aimash_inbound"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            subprocess.run(
                ["docker", "cp", str(resolved), f"aimash-mcp:{destination}"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            copied.append({"path": destination, "suffix": suffix, "size": size, "sha256": digest})
        except (OSError, subprocess.SubprocessError):
            log.warning("Aimash inbound media copy failed")
    return copied


def _signed_token(
    tool_name: str,
    tool_args: dict,
    inbound: _Inbound,
    *,
    inbound_media: list[dict] | None = None,
) -> str | None:
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
        "arg_keys": sorted(str(key) for key in tool_args if str(key) != _TOKEN_PARAM),
        "nonce": secrets.token_hex(16),
        "actor_user_id": inbound.actor_user_id,
        "actor_chat_id": inbound.actor_chat_id,
        "actor_username": inbound.actor_username,
        "chat_type": inbound.chat_type,
        "thread_id": inbound.thread_id,
        "message_id": inbound.message_id,
        "message_text": inbound.message_text,
        "language_code": inbound.language_code,
        "reply_to_message_id": inbound.reply_to_message_id,
        "reply_to_is_own_message": inbound.reply_to_is_own_message,
        "reply_confirmation_id": inbound.reply_confirmation_id,
        "reply_to_text": inbound.reply_to_text,
        "inbound_media": inbound_media or [],
    }
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    signature = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    return f"{_b64url(payload_bytes)}.{_b64url(signature)}"


def _is_aimash_write(tool_name: str) -> bool:
    return (
        tool_name == _EXECUTE_TOOL or tool_name in _ACTION_TOOLS or tool_name in _PLAN_STATE_TOOLS
    )


async def _attach_confirmation_keyboard(adapter, chat_id, message_id, content: str) -> None:
    """Attach UX-only buttons; a failure leaves the secure reply fallback intact."""
    match = _CONFIRM_RE.search(str(content or ""))
    if match is None or not message_id or getattr(adapter, "_bot", None) is None:
        return
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        confirmation_id = match.group(1).lower()
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Да", callback_data=f"am:yes:{confirmation_id}"),
                    InlineKeyboardButton("❌ Нет", callback_data=f"am:no:{confirmation_id}"),
                ],
            ]
        )
        await adapter._bot.edit_message_reply_markup(
            chat_id=_normalize_telegram_chat_id(chat_id),
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
    adapter_module = _telegram_adapter_module()
    if adapter_module is None:
        log.warning("Aimash Telegram button bridge unavailable: TelegramAdapter not found")
        return False
    TelegramAdapter = adapter_module.TelegramAdapter
    if getattr(TelegramAdapter, "_aimash_button_bridge", False):
        return True

    original_send = TelegramAdapter.send
    original_edit = TelegramAdapter.edit_message
    original_callback = TelegramAdapter._handle_callback_query
    original_should_attempt_rich = getattr(TelegramAdapter, "_should_attempt_rich", None)

    if callable(original_should_attempt_rich):

        @functools.wraps(original_should_attempt_rich)
        def should_attempt_rich(adapter, content, metadata=None):
            # Confirmation cards need an inline keyboard.  Keep this one small message on the
            # ordinary sendMessage path: Telegram's new rich/streaming path can finalize through
            # a different message id, which made the post-send keyboard edit target stale output.
            if _CONFIRM_RE.search(str(content or "")):
                return False
            return original_should_attempt_rich(adapter, content, metadata=metadata)

    @functools.wraps(original_send)
    async def send(adapter, chat_id, content, reply_to=None, metadata=None):
        result = await original_send(
            adapter, chat_id, content, reply_to=reply_to, metadata=metadata
        )
        if getattr(result, "success", False):
            await _attach_confirmation_keyboard(
                adapter, chat_id, getattr(result, "message_id", None), content
            )
            await _deliver_pending_artifacts(adapter, chat_id, metadata)
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
            effective_message_id = getattr(result, "message_id", None) or message_id
            await _attach_confirmation_keyboard(adapter, chat_id, effective_message_id, content)
            # Final answers are commonly completed through edit_message rather than send().
            # Without this symmetric hook signed XLSX/media stayed queued forever while the
            # model incorrectly reported that Telegram delivery had happened. Queue pop makes
            # this safe even when Hermes omits the optional ``notify`` metadata flag.
            await _deliver_pending_artifacts(adapter, chat_id, metadata)
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
    if callable(original_should_attempt_rich):
        TelegramAdapter._should_attempt_rich = should_attempt_rich
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
        inbound_media = (
            _copy_trusted_inbound_media(inbound) if tool_name == _INGEST_MEDIA_TOOL else []
        )
        token = _signed_token(tool_name, tool_args, inbound, inbound_media=inbound_media)
        if token is None:
            return {"action": "block", "message": "Aimash trusted transport не настроен"}
        # Model-supplied values are never honored: overwrite after hashing ordinary args.
        tool_args[_TOKEN_PARAM] = token
        return None

    # Private trusted-operator profile: READ, audit, web, memory, skills and native tools never
    # phase-lock one another. External content remains marked as data in MCP envelopes, while
    # mutation execution is protected by the independent trusted Telegram confirmation path.
    return None


def register(ctx):  # noqa: ANN001 - runtime-owned plugin API
    global _button_bridge_ready

    ctx.register_hook("pre_gateway_dispatch", _capture_gateway_event)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    _button_bridge_ready = _install_telegram_button_bridge()
    if not _button_bridge_ready:
        log.warning(
            "Telegram button bridge unavailable; WRITE confirmations use trusted semantic replies"
        )
    if len(os.environ.get("AIMASH_TRUST_HMAC_KEY", "").encode("utf-8")) < 32:
        log.warning("aimash_trusted_transport loaded without a valid signing key; WRITE will block")
    else:
        log.info("aimash_trusted_transport loaded (fail-closed MCP verification enabled)")
