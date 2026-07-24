"""Логирование БЕЗ секретов (golden rule #5) + опциональный JSON-формат.

Публичная поверхность сохранена: `setup_logging(level=None)` и `log = getLogger("aimash")`
(их зовут bot.main, scheduler.*). Конфиг — из окружения (без pydantic, модуль дешёвый на импорт):
  LOG_LEVEL  = DEBUG|INFO|WARNING|ERROR (по умолчанию INFO; приоритет: аргумент > env > INFO)
  LOG_FORMAT = text|json (по умолчанию text)

Редакция секретов — на двух уровнях (defense-in-depth):
  1) RedactionFilter — скраб итогового сообщения (msg+args) ДО форматирования, для всех хендлеров;
  2) форматтеры — повторно скрабят финальную строку (ловит и traceback exc_info).
Идемпотентно: подстановка ***REDACTED*** сама под паттерны не попадает.
"""

from __future__ import annotations

import json
import logging
import os
import re

REDACTED = "***REDACTED***"

# Паттерны секрето-подобных строк. Замена — на REDACTED (ключ в key=value сохраняем).
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Telegram bot token: <digits>:<30+ url-safe>. (?<!\d) — не начинать посреди длинного числа,
    # но допускаем буквенный префикс (api.telegram.org/bot<token> — раньше \b после «bot» не давал
    # границы, и токен в URL утекал; B4).
    (re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}\b"), REDACTED),
    # Google OAuth client secret
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{8,}\b"), REDACTED),
    # OAuth refresh token (префикс 1//)
    (re.compile(r"\b1//[A-Za-z0-9_\-]{15,}\b"), REDACTED),
    # Fernet/base64-ключ (43 символа + '='); без хвостового \b — '=' не граница слова
    (re.compile(r"\b[A-Za-z0-9_\-]{43}="), REDACTED),
    # Authorization: Bearer <...>
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer " + REDACTED),
    # OpenRouter / OpenAI-стиль API-ключи (sk-or-v1-…, sk-…) — у них есть префикс sk-
    (re.compile(r"\bsk-(?:or-)?[A-Za-z0-9_\-]{16,}\b"), REDACTED),
    # B4: пароль в DSN-URL (postgresql://user:PASSWORD@host, redis://:PASSWORD@host) — раньше не
    # ловился ничем (database_url это SecretStr в repr, но СЫРАЯ строка DSN в логе/трейсбеке — нет).
    (re.compile(r"(?i)(://[^:/@\s]*:)([^@\s/]+)(@)"), r"\1" + REDACTED + r"\3"),
    # key=value / key: value для чувствительных ключей (значение скрываем, ключ оставляем).
    # B4: ключ может нести подчёркнутый ПРЕФИКС (developer_token, client_secret, DB_PASSWORD) —
    # раньше \bsecret\b не матчил «client_secret» (нет границы слова через '_') → секрет утекал.
    # Ведущий [\w.-]* поглощает префикс; опц. кавычка после [=:] покрывает JSON-форму "token":"…".
    (
        re.compile(
            r"(?i)([\w.-]*(?:api[_-]?key|secret|token|password|passwd|pwd|pin))"
            r"([\"']?\s*[=:]\s*)([\"']?)([^\s,;}'\"]+)"  # опц. закрыв. кавычка ключа (JSON "token":…)
        ),
        r"\1\2\3" + REDACTED,
    ),
]


def redact_text(text: str) -> str:
    """Заменить секрето-подобные подстроки на REDACTED. Безопасно (никогда не бросает).

    Скрабит по ДВУМ источникам: (1) ТОЧНЫЕ известные значения SecretStr — нужно, т.к. Google Ads
    developer_token это «голый» алфанумерик без префикса/разделителя, под который НЕТ безопасного
    паттерна; (2) паттерны формы. Так outward-пути, зовущие ТОЛЬКО redact_text мимо RedactionFilter
    (bot.ux.err_text → Telegram, core.ads_errors.humanize, confirm.store.record_failure → audit_log),
    не утекут секрет (golden rule #5). Точные значения — ДО паттернов (целиком)."""
    if not text:
        return text
    out = text
    for secret in _known_secret_values():
        if secret in out:
            out = out.replace(secret, REDACTED)
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def _known_secret_values() -> set[str]:
    """Best-effort: точные значения известных SecretStr (ловит секрет, не подошедший под паттерн).
    Любая ошибка импорта/доступа — молча игнорируем (логи не должны падать из-за редакции)."""
    vals: set[str] = set()
    try:
        from core.config import settings

        for attr in (
            "telegram_bot_token",
            "openrouter_api_key",
            "google_ads_developer_token",
            "google_ads_client_secret",
            "google_ads_refresh_token",
            "secrets_encryption_key",
            # B4: раньше отсутствовали — DSN несёт пароль БД, sentry_dsn — ключ проекта,
            # two_factor_pin — код 2FA; все могут попасть в лог/трейсбек сырой строкой.
            "database_url",
            "sentry_dsn",
            "two_factor_pin",
        ):
            sv = getattr(settings, attr, None)
            if sv is None or not hasattr(sv, "get_secret_value"):
                continue
            v = sv.get_secret_value()
            if v and len(v) >= 6:  # не редактируем пустые/слишком короткие (шум)
                vals.add(v)
    except Exception:
        pass
    return vals


class RedactionFilter(logging.Filter):
    """Скрабит итоговое сообщение записи (с подставленными args) ДО форматирования.

    Сворачивает msg+args в одну строку, редактирует и обнуляет args — так секрет в args
    («token=%s», secret) тоже исчезает, а форматтер не пытается повторно применить args."""

    def __init__(self) -> None:
        super().__init__()
        self._known = _known_secret_values()  # снимок на момент конфигурации

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # некорректные args — не роняем логирование
            msg = str(record.msg)
        for secret in self._known:
            if secret in msg:
                msg = msg.replace(secret, REDACTED)
        record.msg = redact_text(msg)
        record.args = None
        return True


class ContextFilter(logging.Filter):
    """Впрыскивает поля корреляционного контекста (request_id/chat_id/operation из core.context)
    в КАЖДУЮ запись — все логи одного апдейта/джобы сшиваются по request_id (§15). Существующие
    log.* (resilience, router, хендлеры, on_error) получают поля без правок call-site. Никогда не
    роняет логирование (наблюдаемость не критична к собственной ошибке)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from core.context import get_context

            ctx = get_context()
            record.request_id = ctx.request_id
            record.chat_id = "-" if ctx.chat_id is None else ctx.chat_id
            record.operation = ctx.operation
        except Exception:  # noqa: BLE001 — контекст не должен ломать лог
            record.request_id = getattr(record, "request_id", "-")
            record.chat_id = getattr(record, "chat_id", "-")
            record.operation = getattr(record, "operation", "-")
        return True


class _RedactingTextFormatter(logging.Formatter):
    """Текстовый форматтер: редактирует финальную строку (в т.ч. traceback из exc_info)."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id"):  # запись мимо ContextFilter (тесты/чужой хендлер)
            record.request_id = "-"
        return redact_text(super().format(record))


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "req": getattr(record, "request_id", "-"),
            "chat": getattr(record, "chat_id", "-"),
            "op": getattr(record, "operation", "-"),
            "msg": redact_text(record.getMessage()),
        }
        if record.exc_info:
            data["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(data, ensure_ascii=False)


def _resolve_level(level: int | None) -> int:
    if level is not None:
        return level
    name = os.getenv("LOG_LEVEL", "").strip().upper()
    if name:
        val = logging.getLevelName(name)  # имя → int, либо строка если неизвестно
        if isinstance(val, int):
            return val
    return logging.INFO


def setup_logging(level: int | None = None) -> None:
    """Сконфигурировать корневой логгер: один stream-хендлер с редакцией секретов.
    Идемпотентно (перенастройка в тестах не плодит хендлеры)."""
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())  # сначала контекст (request_id/chat/op), потом редакция
    handler.addFilter(RedactionFilter())
    if os.getenv("LOG_FORMAT", "text").strip().lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            _RedactingTextFormatter(
                "%(asctime)s %(levelname)s [%(request_id)s] %(name)s %(message)s"
            )
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(_resolve_level(level))


log = logging.getLogger("aimash")
