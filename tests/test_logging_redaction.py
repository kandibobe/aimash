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


# ── B4: расширенные формы утечки (подчёркнутые ключи, JSON, DSN-URL, PIN) ──────────
def test_redact_underscore_prefixed_keys():
    # раньше \bsecret\b/\btoken\b не матчили client_secret/developer_token (граница через '_')
    for form, secret in (
        ("developer_token=DEVsecretVALUE123", "DEVsecretVALUE123"),
        ("client_secret: CLIENTsecret999", "CLIENTsecret999"),
        ("DB_PASSWORD=hunter2pass", "hunter2pass"),
        ("two_factor_pin=246810", "246810"),
    ):
        out = L.redact_text(form)
        assert secret not in out and L.REDACTED in out, form


def test_redact_json_quoted_values():
    # Синтетические (несуществующие) значения — фикстуры проверки редакции, не реальные креды.
    for form, secret in (
        ('{"token": "abcXYZ123456"}', "abcXYZ123456"),  # gitleaks:allow
        ('{"refresh_token":"1//0abcSECRET"}', "1//0abcSECRET"),  # gitleaks:allow
    ):
        out = L.redact_text(form)
        assert secret not in out and L.REDACTED in out, form


def test_redact_dsn_password_in_url():
    out = L.redact_text("postgresql+asyncpg://aimash:S3cretPW@db.host:5432/aimash")
    assert "S3cretPW" not in out and L.REDACTED in out
    assert "db.host" in out  # хост НЕ секрет — остаётся


def test_redact_telegram_token_in_url():
    # api.telegram.org/bot<token> — токен после «bot» (без границы слова) раньше утекал
    out = L.redact_text(f"POST https://api.telegram.org/bot{TG_TOKEN}/sendMessage")
    assert TG_TOKEN not in out and L.REDACTED in out


def test_redact_leaves_safe_text_untouched():
    for safe in (
        "budget = 100",
        "campaign name = Летняя распродажа",
        "просто лог без секретов",
        "CTR: 3.5%",
    ):
        assert L.redact_text(safe) == safe


def test_known_secret_values_includes_dsn_sentry_pin():
    # реестр точных значений покрывает DSN/sentry/pin (для секретов без узнаваемой формы)
    import inspect

    src = inspect.getsource(L._known_secret_values)
    for attr in ("database_url", "sentry_dsn", "two_factor_pin"):
        assert attr in src, f"_known_secret_values должен включать {attr}"


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


def test_redact_text_scrubs_developer_token_by_known_value(monkeypatch):
    """Google Ads developer_token — «голый» алфанумерик без префикса/разделителя: под форму-паттерн
    НЕ подходит. redact_text обязан вычистить его по точному значению из settings (golden rule #5),
    т.к. outward-пути (err_text/humanize/record_failure) зовут ТОЛЬКО redact_text, мимо фильтра."""
    from core.config import settings

    token = "AbC123dEf456Gh789Jk012"  # 22-симв. форма developer-токена, gitleaks:allow
    monkeypatch.setattr(
        settings, "google_ads_developer_token", type(settings.google_ads_developer_token)(token)
    )
    out = L.redact_text(
        f"GoogleAdsException: developer token {token} is invalid"
    )  # без разделителя
    assert token not in out
    assert L.REDACTED in out


def test_redact_text_scrubs_openrouter_key_by_shape():
    key = "sk-or-v1-0123456789abcdef0123456789abcdef"  # gitleaks:allow
    out = L.redact_text(f"401 Unauthorized for key {key}")
    assert key not in out and L.REDACTED in out


def test_err_text_redacts_bare_developer_token(monkeypatch):
    """bot.ux.err_text (текст ошибки в Telegram) не утекает developer_token, даже без 'token='."""
    from bot.ux import err_text
    from core.config import settings

    token = "Zz99Yy88Xx77Ww66Vv55Uu"  # gitleaks:allow
    monkeypatch.setattr(
        settings, "google_ads_developer_token", type(settings.google_ads_developer_token)(token)
    )
    msg = err_text(RuntimeError(f"google.auth refused: {token} not authorized"))  # bare-токен
    assert token not in msg and L.REDACTED in msg


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
