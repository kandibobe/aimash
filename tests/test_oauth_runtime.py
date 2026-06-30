"""Per-account OAuth-токены в рантайме (§8/Фаза 3): load_oauth_cache расшифровывает oauth_tokens
в _OAUTH_RUNTIME, _cfg_for выбирает per-account креды (refresh-токен + login_customer_id) для
не-Draft, иначе .env. Сбой расшифровки одного аккаунта (порча) — пропуск, не падение старта.

Секреты не утекают: проверяем, что в рантайм-кэше лежит РАСШИФРОВАННЫЙ токен, но тестовый — фиктивный.
Тестовый ключ Fernet ставим в settings на время теста (в обычном тест-env ключа нет)."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.fernet import Fernet  # noqa: E402
from pydantic import SecretStr  # noqa: E402

import ads.client as client  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402

DRAFT = "7753643025"
ACCT = "1112223334"


@contextmanager
def _enc_key():
    prev = settings.secrets_encryption_key
    settings.secrets_encryption_key = SecretStr(Fernet.generate_key().decode())
    try:
        yield
    finally:
        settings.secrets_encryption_key = prev


def test_cfg_for_uses_runtime_creds_for_non_draft_else_env():
    client._OAUTH_RUNTIME.clear()
    client._CLIENT_CACHE.clear()
    # Draft → .env-конфиг (нет login_customer_id в тест-env → ключа нет в cfg).
    cfg_draft = client._cfg_for(DRAFT)
    assert "refresh_token" in cfg_draft
    # Зарегистрировали per-account токен → _cfg_for(ACCT) берёт его + per-account login.
    client.set_oauth_runtime(ACCT, "tok-acct", "5556667778")
    cfg = client._cfg_for(ACCT)
    assert cfg["refresh_token"] == "tok-acct"
    assert cfg["login_customer_id"] == "5556667778"
    client._OAUTH_RUNTIME.clear()
    client._CLIENT_CACHE.clear()


def test_set_oauth_runtime_busts_client_cache():
    client._OAUTH_RUNTIME.clear()
    client._CLIENT_CACHE.clear()
    client._CLIENT_CACHE[ACCT] = object()  # как будто клиент уже собран
    client.set_oauth_runtime(ACCT, "tok", "999")
    assert ACCT not in client._CLIENT_CACHE  # кэш клиента сброшен под новый токен
    client._OAUTH_RUNTIME.clear()


async def test_load_oauth_cache_decrypts_and_skips_bad():
    from core.secrets import encrypt
    from db.models import OAuthToken
    from db.session import Session

    await init_db()
    client._OAUTH_RUNTIME.clear()
    with _enc_key():
        good_enc = encrypt("good-token")
        async with Session() as s:
            # чистим возможные прошлые строки (temp БД переживает между тестами)
            from sqlalchemy import delete

            await s.execute(delete(OAuthToken))
            s.add(
                OAuthToken(account=ACCT, refresh_token_enc=good_enc, login_customer_id="5556667778")
            )
            s.add(OAuthToken(account="2223334445", refresh_token_enc="not-a-valid-token"))
            await s.commit()

        loaded = await client.load_oauth_cache()
    assert loaded == 1  # битый токен пропущен, валидный загружен
    assert client._OAUTH_RUNTIME[ACCT] == ("good-token", "5556667778")
    assert "2223334445" not in client._OAUTH_RUNTIME
    client._OAUTH_RUNTIME.clear()
