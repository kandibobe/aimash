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
    s = Settings(env="prod", secrets_encryption_key=key, _env_file=None)
    assert s.is_prod is True
