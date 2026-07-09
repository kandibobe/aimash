"""Fail-fast на отсутствие SECRETS_ENCRYPTION_KEY в prod (шифрование токенов at-rest).

dev/тесты с пустым ключом + SQLite должны конструироваться без ошибок; prod — падать.
Конструируем Settings явно с _env_file=None → тест не зависит от локального .env/окружения.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Settings  # noqa: E402


def test_prod_without_key_raises():
    with pytest.raises(ValidationError):
        Settings(env="prod", secrets_encryption_key="", _env_file=None)


def test_prod_with_invalid_key_raises():
    with pytest.raises(ValidationError):
        Settings(env="prod", secrets_encryption_key="not-a-valid-fernet-key", _env_file=None)


def test_dev_without_key_ok():
    s = Settings(env="dev", secrets_encryption_key="", _env_file=None)
    assert s.is_prod is False


def test_prod_with_valid_key_ok():
    key = Fernet.generate_key().decode()
    # whitelist + Google Ads creds обязательны в prod → передаём, чтобы проверить именно ключ-валидатор
    s = Settings(
        env="prod",
        secrets_encryption_key=key,
        telegram_whitelist_chat_ids="123",
        google_ads_developer_token="dev-token",
        google_ads_allowed_customer_ids="7753643025",
        _env_file=None,
    )
    assert s.is_prod is True


def _prod_kwargs(**extra):
    """Валидный prod-набор по умолчанию — чтобы изолировать проверяемый валидатор."""
    base = {
        "env": "prod",
        "secrets_encryption_key": Fernet.generate_key().decode(),
        "telegram_whitelist_chat_ids": "123",
        "google_ads_developer_token": "dev-token",
        "google_ads_allowed_customer_ids": "7753643025",
        "_env_file": None,
    }
    base.update(extra)
    return base


def test_prod_without_whitelist_raises():
    # пустой whitelist в prod = fail-open (бот ответил бы всем) → старт должен падать
    with pytest.raises(ValidationError):
        Settings(**_prod_kwargs(telegram_whitelist_chat_ids=""))


def test_prod_with_whitelist_ok():
    s = Settings(**_prod_kwargs(telegram_whitelist_chat_ids="123,456"))
    assert s.whitelist == {123, 456}


def test_dev_without_whitelist_ok():
    # в dev пустой whitelist допустим при конструировании (fail-closed обеспечивает middleware)
    s = Settings(**_prod_kwargs(env="dev", telegram_whitelist_chat_ids=""))
    assert s.whitelist == set()


def test_prod_without_google_ads_token_raises():
    # в prod пустой developer token = бот не сможет работать с Google Ads → падаем на старте
    with pytest.raises(ValidationError):
        Settings(**_prod_kwargs(google_ads_developer_token=""))


def test_prod_without_allowed_customer_ids_defaults_to_all_visible():
    # Решение владельца 2026-07 (Draft-only доктрина снята): prod с пустым списком мутаций
    # НЕ падает, а дефолтится в сентинел «all» — мутации на всём ВИДИМОМ потолке
    # (allowed_ceiling(); замок видимости ensure_allowed и confirm-гейт остаются).
    # Явный список id по-прежнему СУЖАЕТ набор; в dev/test пусто = fail-closed.
    s = Settings(**_prod_kwargs(google_ads_allowed_customer_ids=""))
    assert s.google_ads_allowed_customer_ids == "all"
    assert s.allow_all_visible is True


def test_prod_explicit_allowed_ids_narrow_not_all():
    # Явный список в prod НЕ подменяется сентинелом — владелец сузил набор осознанно.
    s = Settings(**_prod_kwargs(google_ads_allowed_customer_ids="7753643025"))
    assert s.allow_all_visible is False and s.allowed_customer_ids == {"7753643025"}


def test_dev_empty_allowed_ids_stays_fail_closed():
    # В dev/test пустой список НЕ дефолтится в «all» (не открываем локаль/CI).
    s = Settings(**_prod_kwargs(env="dev", google_ads_allowed_customer_ids=""))
    assert s.allow_all_visible is False and s.allowed_customer_ids == set()


def test_dev_without_google_ads_ok():
    # в dev живые креды Google Ads не обязательны (работа на фейках/офлайн-тестах)
    s = Settings(
        **_prod_kwargs(env="dev", google_ads_developer_token="", google_ads_allowed_customer_ids="")
    )
    assert s.is_prod is False


def test_prod_short_2fa_pin_raises():
    # A14: включённый 2FA с ЗАДАННЫМ коротким PIN (<6) в prod → fail-fast (легко перебрать)
    with pytest.raises(ValidationError):
        Settings(**_prod_kwargs(two_factor_enabled=True, two_factor_pin="123"))


def test_prod_strong_2fa_pin_ok():
    s = Settings(**_prod_kwargs(two_factor_enabled=True, two_factor_pin="123456"))
    assert s.two_factor_enabled is True


def test_prod_empty_2fa_pin_does_not_raise():
    # пустой PIN при включённом 2FA НЕ роняет старт — is_ready() fail-closed блокирует ops в рантайме
    s = Settings(**_prod_kwargs(two_factor_enabled=True, two_factor_pin=""))
    assert s.is_prod is True


def test_dev_short_2fa_pin_ok():
    # в dev/тестах короткий PIN допустим (фикстуры вроде "2468")
    s = Settings(**_prod_kwargs(env="dev", two_factor_enabled=True, two_factor_pin="2468"))
    assert s.is_prod is False


def test_provider_sort_valid_values_normalized():
    """price|throughput|latency допустимы; регистр/пробелы нормализуются (strip+lower)."""
    for raw, want in (("latency", "latency"), (" Throughput ", "throughput"), ("PRICE", "price")):
        s = Settings(env="dev", openrouter_parsing_provider_sort=raw, _env_file=None)
        assert s.openrouter_parsing_provider_sort == want


def test_provider_sort_empty_stays_off():
    s = Settings(env="dev", openrouter_parsing_provider_sort="", _env_file=None)
    assert s.openrouter_parsing_provider_sort == ""


def test_provider_sort_invalid_coerced_to_off():
    """Кривое значение (напр. 'speed', 'fastest', опечатка) → '' (ВЫКЛ), НЕ роняет старт —
    иначе улетело бы в OpenRouter → 400 'provider.sort: Invalid input' на каждом парсинге."""
    for bad in ("speed", "fastest", "throughput;", "'latency'", "quality"):
        s = Settings(env="dev", openrouter_parsing_provider_sort=bad, _env_file=None)
        assert s.openrouter_parsing_provider_sort == "", f"{bad!r} должно коэрситься в ''"


def test_require_dev_env_blocks_outside_dev(monkeypatch):
    """Гард dev-скриптов прямой записи (golden rule #10): вне ENV=dev — SystemExit; в dev — ок."""
    import core.config as cfg

    monkeypatch.setattr(cfg.settings, "env", "prod")
    with pytest.raises(SystemExit):
        cfg.require_dev_env()
    monkeypatch.setattr(cfg.settings, "env", "dev")
    cfg.require_dev_env()  # в dev не падает
