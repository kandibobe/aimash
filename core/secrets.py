"""Шифрование секретов at-rest (refresh-токены и т.п. в БД).

Токены НИКОГДА не хранятся в открытом виде в БД и НИКОГДА не уходят в промпт/логи.
Ключ — из окружения (SECRETS_ENCRYPTION_KEY), не из кода.
"""
from __future__ import annotations

from cryptography.fernet import Fernet

from core.config import settings


def _fernet() -> Fernet:
    if not settings.secrets_encryption_key:
        raise RuntimeError("SECRETS_ENCRYPTION_KEY не задан — сгенерируй: Fernet.generate_key()")
    return Fernet(settings.secrets_encryption_key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def redact(secret: str | None) -> str:
    """Для логов: показать только хвост, никогда полный секрет."""
    if not secret:
        return "<empty>"
    return f"***{secret[-4:]}" if len(secret) > 4 else "****"
