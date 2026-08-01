"""Fail-closed bridge from the trusted Hermes Telegram hook into MCP PLAN/WRITE.

The model can see and choose MCP arguments, so Telegram identity must never be accepted as an
ordinary tool argument.  The Hermes plugin overwrites ``trusted_turn_token`` after the model has
finished constructing the call.  This module verifies its HMAC, expiry, exact tool name and the
digest of every ordinary argument before opening ``human_turn``/request context.

Hook failures are fail-open in Hermes.  That is safe here by construction: no valid token means no
PLAN/WRITE call.  The signing key is shared only by the gateway hook and this MCP process; Google
OAuth credentials remain absent from Hermes.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import inspect
import json
import math
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Any, Awaitable, Callable, Iterator, get_type_hints

from core import i18n
from core.config import settings
from core.context import reset_context, set_context
from core.provenance import human_turn

TOKEN_PARAM = "trusted_turn_token"
TOKEN_VERSION = 1
TOKEN_MAX_BYTES = 16_384
TOKEN_MAX_LIFETIME_S = 300
TOKEN_FUTURE_SKEW_S = 30
MCP_TOOL_PREFIX = "mcp__aimash__"
CONFIRM_MARKER_PREFIX = "AIMASH_CONFIRM:"
TRANSPORT_REFUSAL_MESSAGE = (
    "Не удалось безопасно связать действие с текущим сообщением Telegram. "
    "Отправьте команду ещё раз отдельным сообщением."
)


class TrustedTransportError(PermissionError):
    """Trusted Telegram metadata is absent, stale or not bound to this exact call."""


@dataclass(frozen=True, slots=True)
class TrustedTurn:
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
    issued_at: int
    expires_at: int
    message_text: str | None = None
    inbound_media: tuple["TrustedMedia", ...] = ()

    @property
    def run_id(self) -> str:
        """Stable per-human-message key for one pending confirmation card per command."""
        raw = f"telegram:{self.actor_chat_id}:{self.thread_id or '-'}:{self.message_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_TRUSTED_TURN: contextvars.ContextVar[TrustedTurn | None] = contextvars.ContextVar(
    "aimash_trusted_turn", default=None
)


@dataclass(frozen=True, slots=True)
class TrustedMedia:
    path: str
    suffix: str
    size: int
    sha256: str


_INBOUND_MEDIA_PATH_RE = re.compile(
    r"^/tmp/aimash_inbound/[0-9a-f]{32}\.(?:jpg|jpeg|png|webp|mp4|mov)$"
)


def _canonical_json_value(value: Any) -> Any:
    """Normalize harmless JSON number coercion performed by FastMCP."""
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


def canonical_args_digest(args: dict[str, Any]) -> str:
    """Digest arguments identically before and after typed FastMCP validation."""
    clean = _canonical_json_value({str(k): v for k, v in args.items() if str(k) != TOKEN_PARAM})
    try:
        encoded = json.dumps(
            clean,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TrustedTransportError("аргументы trusted-вызова неканоничны") from exc
    return hashlib.sha256(encoded).hexdigest()


def proposal_marker(confirmation_id: str) -> str:
    cid = str(confirmation_id).strip().lower()
    if len(cid) != 32 or any(ch not in "0123456789abcdef" for ch in cid):
        raise ValueError("confirmation_id должен быть 32-символьным hex")
    return f"{CONFIRM_MARKER_PREFIX}{cid}"


def _b64url_decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001 - единый безопасный отказ, без сырого токена
        raise TrustedTransportError("trusted token повреждён") from exc


def _required_int(payload: dict[str, Any], name: str, *, positive: bool = False) -> int:
    value = payload.get(name)
    if value is None or isinstance(value, bool):
        raise TrustedTransportError("trusted token содержит неверные числовые поля")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TrustedTransportError("trusted token содержит неверные числовые поля") from exc
    if positive and parsed <= 0:
        raise TrustedTransportError("trusted token содержит неверные числовые поля")
    return parsed


def _optional_positive_int(payload: dict[str, Any], name: str) -> int | None:
    value = payload.get(name)
    if value in (None, ""):
        return None
    return _required_int(payload, name, positive=True)


def _signing_key() -> bytes:
    value = settings.aimash_trust_hmac_key.get_secret_value()
    if len(value.encode("utf-8")) < 32:
        raise TrustedTransportError("trusted transport не настроен")
    return value.encode("utf-8")


def verify_turn_token(
    token: str,
    *,
    expected_tool: str,
    tool_args: dict[str, Any],
    default_args: dict[str, Any] | None = None,
    now: int | None = None,
) -> TrustedTurn:
    """Verify authenticity and bind the token to one exact MCP call."""
    if not isinstance(token, str) or not token or len(token.encode("utf-8")) > TOKEN_MAX_BYTES:
        raise TrustedTransportError("trusted Telegram context отсутствует")
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError as exc:
        raise TrustedTransportError("trusted token повреждён") from exc

    payload_bytes = _b64url_decode(payload_part)
    supplied_signature = _b64url_decode(signature_part)
    expected_signature = hmac.new(_signing_key(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise TrustedTransportError("trusted token не прошёл проверку подписи")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustedTransportError("trusted token повреждён") from exc
    if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION:
        raise TrustedTransportError("версия trusted token не поддерживается")
    if payload.get("platform") != "telegram":
        raise TrustedTransportError("trusted token получен не из Telegram")

    exact_tool = f"{MCP_TOOL_PREFIX}{expected_tool}"
    if payload.get("tool") != exact_tool:
        raise TrustedTransportError("trusted token выпущен для другого инструмента")

    # FastMCP materializes omitted schema defaults before invoking the wrapped function.  The hook
    # signs the model's original JSON, so compare only its signed keys and allow additional runtime
    # keys solely when they are declared defaults whose values were not changed.  Older tokens did
    # not carry arg_keys and retain the previous exact-dictionary behavior during rolling deploys.
    signed_keys = payload.get("arg_keys")
    digest_args = tool_args
    if signed_keys is not None:
        if (
            not isinstance(signed_keys, list)
            or any(not isinstance(key, str) or key == TOKEN_PARAM for key in signed_keys)
            or len(signed_keys) != len(set(signed_keys))
        ):
            raise TrustedTransportError("trusted token содержит неверный список аргументов")
        signed_key_set = set(signed_keys)
        if not signed_key_set.issubset(tool_args):
            raise TrustedTransportError("аргументы инструмента изменились после доверенного hook")
        defaults = default_args or {}
        runtime_only = set(tool_args) - signed_key_set
        if not runtime_only.issubset(defaults) or any(
            _canonical_json_value(tool_args[key]) != _canonical_json_value(defaults[key])
            for key in runtime_only
        ):
            raise TrustedTransportError("аргументы инструмента изменились после доверенного hook")
        digest_args = {key: tool_args[key] for key in signed_keys}
    if payload.get("args_sha256") != canonical_args_digest(digest_args):
        raise TrustedTransportError("аргументы инструмента изменились после доверенного hook")

    issued_at = _required_int(payload, "iat", positive=True)
    expires_at = _required_int(payload, "exp", positive=True)
    current = int(time.time()) if now is None else int(now)
    if issued_at > current + TOKEN_FUTURE_SKEW_S or expires_at < current:
        raise TrustedTransportError("trusted token просрочен")
    if expires_at <= issued_at or expires_at - issued_at > TOKEN_MAX_LIFETIME_S:
        raise TrustedTransportError("trusted token имеет недопустимый срок")

    reply_cid = payload.get("reply_confirmation_id") or None
    if reply_cid is not None:
        reply_cid = str(reply_cid).lower()
        if len(reply_cid) != 32 or any(ch not in "0123456789abcdef" for ch in reply_cid):
            raise TrustedTransportError("trusted token содержит неверный якорь подтверждения")

    actor_username = payload.get("actor_username") or None
    thread_id = payload.get("thread_id") or None
    chat_type = str(payload.get("chat_type") or "")
    if chat_type not in {"dm", "group", "forum", "private", "supergroup"}:
        raise TrustedTransportError("trusted token содержит неверный тип Telegram-чата")
    if not isinstance(payload.get("reply_to_is_own_message"), bool):
        raise TrustedTransportError("trusted token содержит неверный reply-флаг")
    reply_to_text = payload.get("reply_to_text")
    if reply_to_text is not None and (
        not isinstance(reply_to_text, str) or len(reply_to_text.encode("utf-8")) > 12_000
    ):
        raise TrustedTransportError("trusted token содержит недопустимый текст reply")

    message_text = payload.get("message_text")
    if message_text is not None and (
        not isinstance(message_text, str) or len(message_text.encode("utf-8")) > 12_000
    ):
        raise TrustedTransportError("trusted token contains invalid message text")

    raw_media = payload.get("inbound_media") or []
    if not isinstance(raw_media, list) or len(raw_media) > 10:
        raise TrustedTransportError("trusted token contains invalid inbound media")
    inbound_media: list[TrustedMedia] = []
    for item in raw_media:
        if not isinstance(item, dict):
            raise TrustedTransportError("trusted token contains invalid inbound media")
        path = str(item.get("path") or "")
        suffix = str(item.get("suffix") or "").lower()
        size = _required_int(item, "size", positive=True)
        digest = str(item.get("sha256") or "")
        if (
            not _INBOUND_MEDIA_PATH_RE.fullmatch(path)
            or suffix not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}
            or not path.endswith(suffix)
            or size > 20 * 1024 * 1024
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise TrustedTransportError("trusted token contains invalid inbound media")
        inbound_media.append(TrustedMedia(path=path, suffix=suffix, size=size, sha256=digest))

    return TrustedTurn(
        actor_user_id=_required_int(payload, "actor_user_id", positive=True),
        actor_chat_id=_required_int(payload, "actor_chat_id"),
        actor_username=str(actor_username)[:128] if actor_username else None,
        chat_type=chat_type,
        thread_id=str(thread_id)[:128] if thread_id else None,
        message_id=_required_int(payload, "message_id", positive=True),
        message_text=message_text or None,
        language_code=str(payload.get("language_code") or "ru")[:8],
        reply_to_message_id=_optional_positive_int(payload, "reply_to_message_id"),
        reply_to_is_own_message=payload["reply_to_is_own_message"],
        reply_confirmation_id=reply_cid,
        reply_to_text=reply_to_text or None,
        issued_at=issued_at,
        expires_at=expires_at,
        inbound_media=tuple(inbound_media),
    )


def get_trusted_turn() -> TrustedTurn:
    turn = _TRUSTED_TURN.get()
    if turn is None:
        raise TrustedTransportError("trusted Telegram context отсутствует")
    return turn


@contextmanager
def trusted_turn_scope(turn: TrustedTurn) -> Iterator[None]:
    """Open all bot-free contexts from one verified Telegram envelope."""
    req_token = set_context(
        request_id=turn.run_id,
        chat_id=turn.actor_chat_id,
        operation="hermes:trusted-turn",
    )
    lang_token = i18n.set_current_lang((turn.language_code or "ru")[:2])
    trusted_token = _TRUSTED_TURN.set(turn)
    try:
        with human_turn(actor_user_id=turn.actor_user_id, run_id=turn.run_id):
            yield
    finally:
        _TRUSTED_TURN.reset(trusted_token)
        i18n.reset_current_lang(lang_token)
        reset_context(req_token)


def trusted_tool(name: str, fn: Callable[..., Awaitable[dict[str, Any]]]):
    """Expose one PLAN/WRITE callable only behind a verified Telegram envelope.

    ``trusted_turn_token`` is intentionally added to the public MCP schema.  The model may emit a
    value for it, but the Hermes hook overwrites it in-place and the HMAC binds the final token to
    this exact tool name and every ordinary argument.  A missing hook/key/token therefore refuses
    before proposal creation, confirmation CAS or Google Ads SDK use.
    """
    raw_signature = inspect.signature(fn)
    resolved_hints = get_type_hints(fn)
    signature = raw_signature.replace(
        parameters=[
            parameter.replace(annotation=resolved_hints.get(parameter.name, parameter.annotation))
            for parameter in raw_signature.parameters.values()
        ],
        return_annotation=resolved_hints.get("return", raw_signature.return_annotation),
    )
    if TOKEN_PARAM in signature.parameters:
        raise RuntimeError(f"{name}: зарезервированный параметр {TOKEN_PARAM} уже занят")
    token_parameter = inspect.Parameter(
        TOKEN_PARAM,
        kind=inspect.Parameter.KEYWORD_ONLY,
        default="",
        annotation=str,
    )
    declared_defaults = {
        parameter.name: parameter.default
        for parameter in signature.parameters.values()
        if parameter.default is not inspect.Parameter.empty
    }

    @wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from core.access import ensure_account_allowed_for_user, is_whitelisted
        from core.config import normalize_customer_id
        from mcp_server.envelope import refused
        from mcp_server.redact import redact_error

        token = kwargs.pop(TOKEN_PARAM, "")
        try:
            bound = signature.bind(*args, **kwargs)
            ordinary_args = dict(bound.arguments)
            turn = verify_turn_token(
                token,
                expected_tool=name,
                tool_args=ordinary_args,
                default_args=declared_defaults,
            )
            bound.apply_defaults()
            if not await is_whitelisted(turn.actor_user_id):
                raise TrustedTransportError("оператор не входит в whitelist")
            account = ordinary_args.get("account")
            if account is not None:
                await ensure_account_allowed_for_user(
                    turn.actor_user_id, normalize_customer_id(str(account))
                )
            with trusted_turn_scope(turn):
                return await fn(*bound.args, **bound.kwargs)
        except TrustedTransportError:
            # The model and Telegram user must never see HMAC/hook implementation details.
            if name == "execute_confirmed":
                return {
                    "status": "failed",
                    "error": TRANSPORT_REFUSAL_MESSAGE,
                    "error_code": "refused",
                }
            return refused(TRANSPORT_REFUSAL_MESSAGE, error_code="refused")
        except (PermissionError, TypeError, ValueError) as exc:
            # The execute facade has its own stable result shape; proposals use refused().
            if name == "execute_confirmed":
                return {
                    "status": "failed",
                    "error": redact_error(exc),
                    "error_code": "refused",
                }
            return refused(redact_error(exc), error_code="refused")
        except Exception as exc:  # noqa: BLE001 - no raw framework error may escape to the model
            from core.logging import log

            log.warning(
                "trusted MCP tool %s failed before completion: %s", name, type(exc).__name__
            )
            if name == "execute_confirmed":
                return {
                    "status": "failed",
                    "error": redact_error(exc),
                    "error_code": "internal",
                }
            return refused(redact_error(exc), error_code="internal")

    wrapped.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[*signature.parameters.values(), token_parameter]
    )
    wrapped.__annotations__ = {**resolved_hints, TOKEN_PARAM: str}
    return wrapped
