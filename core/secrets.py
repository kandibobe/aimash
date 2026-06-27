"""Шифрование секретов at-rest (refresh-токены и т.п. в БД).

Токены НИКОГДА не хранятся в открытом виде в БД и НИКОГДА не уходят в промпт/логи.
Ключ — из окружения (SECRETS_ENCRYPTION_KEY), не из кода.

⚠️ СТАТУС (тест-фаза): этот модуль + таблица db.models.OAuthToken — ЗАДЕЛ под мультиаккаунт (§8).
В рантайме ads.client.build_client пока берёт единственный refresh-токен из .env (SecretStr),
а не из oauth_tokens. encrypt/decrypt включаются вместе со снятием замка аккаунта (golden rule #9,
golden rule #5). Не считать «шифрование токенов в БД» активным до этой проводки.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from core.config import settings


def _fernet() -> Fernet:
    key = settings.secrets_encryption_key.get_secret_value()
    if not key:
        raise RuntimeError("SECRETS_ENCRYPTION_KEY не задан — сгенерируй: Fernet.generate_key()")
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def redact(secret: str | None) -> str:
    """Для логов: показать только хвост, никогда полный секрет."""
    if not secret:
        return "<empty>"
    return f"***{secret[-4:]}" if len(secret) > 4 else "****"
