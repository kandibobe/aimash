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
    # whitelist обязателен в prod → передаём, чтобы проверить именно ключ-валидатор
    s = Settings(
        env="prod", secrets_encryption_key=key, telegram_whitelist_chat_ids="123", _env_file=None
    )
    assert s.is_prod is True


def _prod_kwargs(**extra):
    """Валидный prod-ключ по умолчанию — чтобы изолировать проверяемый валидатор."""
    base = {
        "env": "prod",
        "secrets_encryption_key": Fernet.generate_key().decode(),
        "telegram_whitelist_chat_ids": "123",
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


def test_require_dev_env_blocks_outside_dev(monkeypatch):
    """Гард dev-скриптов прямой записи (golden rule #10): вне ENV=dev — SystemExit; в dev — ок."""
    import core.config as cfg

    monkeypatch.setattr(cfg.settings, "env", "prod")
    with pytest.raises(SystemExit):
        cfg.require_dev_env()
    monkeypatch.setattr(cfg.settings, "env", "dev")
    cfg.require_dev_env()  # в dev не падает
