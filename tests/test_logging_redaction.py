"""Офлайн-тесты редакции секретов в логах (golden rule #5). Синтетические токены, без сети."""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.logging as L  # noqa: E402

# Синтетические (несуществующие) секреты — форма как у настоящих. gitleaks:allow на каждой
# строке: это не реальные креды, а фикстуры для проверки редакции.
TG_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"  # gitleaks:allow
GOCSPX = "GOCSPX-aB3dEfGh1jKlMnOp"  # gitleaks:allow
REFRESH = "1//0aBcDeFgHiJkLmNoPqRsTuVwXyZ"  # gitleaks:allow
FERNET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ="  # 43 + '=', gitleaks:allow


def _logger_with(handler_filter: bool, formatter: logging.Formatter):
    """Изолированный логгер с нашим фильтром/форматтером, пишущий в StringIO."""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    if handler_filter:
        h.addFilter(L.RedactionFilter())
    h.setFormatter(formatter)
    lg = logging.getLogger(f"aimash.test.{id(buf)}")
    lg.handlers.clear()
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg, buf


def test_redact_text_covers_known_secret_shapes():
    for secret in (TG_TOKEN, GOCSPX, REFRESH, FERNET):
        out = L.redact_text(f"value is {secret} end")
        assert secret not in out
        assert L.REDACTED in out


def test_redact_key_value_pairs():
    out = L.redact_text("api_key=sk-supersecretvalue123 and token: abcDEF12345")
    assert "sk-supersecretvalue123" not in out
    assert "abcDEF12345" not in out
    assert out.count(L.REDACTED) == 2


def test_filter_redacts_message_body():
    lg, buf = _logger_with(True, L._RedactingTextFormatter("%(message)s"))
    lg.info("connecting with %s now", TG_TOKEN)
    text = buf.getvalue()
    assert TG_TOKEN not in text  # секрет из args тоже вычищен
    assert L.REDACTED in text


def test_json_format_is_valid_and_redacted():
    lg, buf = _logger_with(True, L._JsonFormatter())
    lg.warning("auth header: %s", "Bearer SUPERSECRETtokenvalue999")
    line = buf.getvalue().strip()
    parsed = json.loads(line)  # валидный JSON
    assert parsed["level"] == "WARNING"
    assert "SUPERSECRETtokenvalue999" not in line
    assert L.REDACTED in parsed["msg"]


def test_json_redacts_exception_traceback():
    lg, buf = _logger_with(True, L._JsonFormatter())
    try:
        raise ValueError(f"boom token={TG_TOKEN}")
    except ValueError:
        lg.exception("failed")
    parsed = json.loads(buf.getvalue().strip())
    assert TG_TOKEN not in buf.getvalue()
    assert "exc" in parsed


def test_resolve_level_precedence(monkeypatch):
    assert L._resolve_level(logging.ERROR) == logging.ERROR  # явный аргумент главнее
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    assert L._resolve_level(None) == logging.WARNING
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert L._resolve_level(None) == logging.INFO  # дефолт


def test_known_secret_value_redacted_via_filter(monkeypatch):
    # Секрет, который НЕ подходит под паттерны (короткое «обычное» слово), но известен из settings.
    from core.config import settings

    monkeypatch.setattr(
        settings, "telegram_bot_token", type(settings.telegram_bot_token)("plainsecret42")
    )
    lg, buf = _logger_with(True, L._RedactingTextFormatter("%(message)s"))
    lg.info("leak: plainsecret42")
    assert "plainsecret42" not in buf.getvalue()
    assert L.REDACTED in buf.getvalue()
