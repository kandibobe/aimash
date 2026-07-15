"""Шифрование секретов at-rest (refresh-токены и т.п. в БД).

Токены НИКОГДА не хранятся в открытом виде в БД и НИКОГДА не уходят в промпт/логи.
Ключ — из окружения (SECRETS_ENCRYPTION_KEY), не из кода.

СТАТУС: encrypt/decrypt АКТИВНЫ для пер-аккаунтных токенов §8 (мультиаккаунт). На старте
ads.client.load_oauth_cache() читает db.models.OAuthToken и decrypt(refresh_token_enc) → _OAUTH_RUNTIME
(шифротекст в БД, открытый токен только в памяти процесса). ЕДИНСТВЕННЫЙ основной refresh-токен
(Draft/главный аккаунт) намеренно берётся из .env (SecretStr), а НЕ из БД, — это env-секрет, не
«at-rest в БД», отдельный контур конфигурации (golden rule #5). Правило «токены в БД шифруются
at-rest» относится к OAuthToken и здесь соблюдено.
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
