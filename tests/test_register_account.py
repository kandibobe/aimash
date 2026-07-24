"""scripts/register_account.py — регистрация per-account OAuth-токена (§8/Фаза 3).

Браузерный OAuth-флоу мокаем (InstalledAppFlow); тестируем то, что реально несёт риск:
  • _upsert — идемпотентность (вставка → обновление, без дублей);
  • round-trip шифра: encrypt → в БД ШИФР (не открытый токен) → ads.client.load_oauth_cache
    расшифровывает обратно (стыковка register → рантайм-кэш);
  • гейты main(): пустой ключ шифрования / пустые client_id|secret / нецифровой --account|--login
    → SystemExit(1) ДО конструирования flow (браузер не открывается);
  • golden rule #5: открытый refresh-токен НИКОГДА не печатается в stdout.

Реальная БД — temp SQLite (conftest). Все секреты тут — синтетические/фиктивные.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("google_auth_oauthlib")  # дев-зависимость OAuth-флоу; нет → скип модуля

from cryptography.fernet import Fernet  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

import ads.client as client  # noqa: E402
import scripts.register_account as reg  # noqa: E402
from core.config import settings  # noqa: E402
from core.secrets import decrypt, encrypt  # noqa: E402
from db.models import OAuthToken  # noqa: E402
from db.session import Session, init_db  # noqa: E402

ACCT = "1112223334"
ACCT_DASH = "111-222-3334"
LOGIN = "5556667778"
LOGIN_DASH = "555-666-7778"
FAKE_TOKEN = "1//0gFAKErefreshTOKEN_aimash_register_xyz789"  # синтетика, не реальный секрет


@contextmanager
def _enc_key():
    """Временный Fernet-ключ в settings (в обычном тест-env ключа нет → encrypt/decrypt падали бы)."""
    prev = settings.secrets_encryption_key
    settings.secrets_encryption_key = SecretStr(Fernet.generate_key().decode())
    try:
        yield
    finally:
        settings.secrets_encryption_key = prev


async def _delete_token(account: str) -> None:
    await init_db()
    async with Session() as s:
        await s.execute(delete(OAuthToken).where(OAuthToken.account == account))
        await s.commit()


async def _get_token(account: str) -> OAuthToken | None:
    async with Session() as s:
        return (
            await s.execute(select(OAuthToken).where(OAuthToken.account == account))
        ).scalar_one_or_none()


# ── _upsert: вставка → обновление, идемпотентно (одна строка) ────────────────────
async def test_upsert_inserts_then_updates_without_duplicate():
    await _delete_token(ACCT)
    with _enc_key():
        await reg._upsert(ACCT, encrypt("tok-1"), LOGIN)
        await reg._upsert(ACCT, encrypt("tok-2"), "9998887776")  # повтор → обновляет ту же строку

        async with Session() as s:
            rows = (
                (await s.execute(select(OAuthToken).where(OAuthToken.account == ACCT)))
                .scalars()
                .all()
            )
        assert len(rows) == 1  # дубля нет
        assert decrypt(rows[0].refresh_token_enc) == "tok-2"  # поля обновились
        assert rows[0].login_customer_id == "9998887776"
    await _delete_token(ACCT)


# ── round-trip: _upsert пишет шифр (не открытый токен), load_oauth_cache читает его ─
async def test_upsert_roundtrip_loads_via_oauth_cache():
    await _delete_token(ACCT)
    client._OAUTH_RUNTIME.clear()
    with _enc_key():
        enc = encrypt(FAKE_TOKEN)
        assert FAKE_TOKEN not in enc  # в БД ляжет ШИФР, а не открытый токен
        await reg._upsert(ACCT, enc, LOGIN)
        loaded = await client.load_oauth_cache()
    assert loaded >= 1
    assert client._OAUTH_RUNTIME[ACCT] == (FAKE_TOKEN, LOGIN)  # рантайм-кэш расшифровал обратно
    client._OAUTH_RUNTIME.clear()
    await _delete_token(ACCT)


# ── Гейты main(): браузер не открывается, выходим ДО конструирования flow ─────────
class _NoFlow:
    """Подмена InstalledAppFlow: если гейт НЕ сработал и дошло до flow — падаем явно."""

    @staticmethod
    def from_client_config(*a, **k):
        raise AssertionError("flow сконструирован — гейт не отсёк до браузера")


def _argv(account: str = ACCT_DASH, login: str = LOGIN_DASH) -> list[str]:
    return ["register_account", "--account", account, "--login", login]


def test_main_exits_on_invalid_account(monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(account="abc"))  # нецифровой → normalize даст ""
    monkeypatch.setattr(reg, "InstalledAppFlow", _NoFlow)
    with pytest.raises(SystemExit) as ei:
        reg.main()
    assert ei.value.code == 1


def test_main_exits_without_encryption_key(monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv())
    monkeypatch.setattr(settings, "secrets_encryption_key", SecretStr(""))  # ключа нет
    monkeypatch.setattr(reg, "InstalledAppFlow", _NoFlow)
    with pytest.raises(SystemExit) as ei:
        reg.main()
    assert ei.value.code == 1


def test_main_exits_without_client_credentials(monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv())
    monkeypatch.setattr(
        settings, "secrets_encryption_key", SecretStr(Fernet.generate_key().decode())
    )
    monkeypatch.setattr(settings, "google_ads_client_id", "")  # client_id пуст → отказ
    monkeypatch.setattr(reg, "InstalledAppFlow", _NoFlow)
    with pytest.raises(SystemExit) as ei:
        reg.main()
    assert ei.value.code == 1


# ── Успех: токен зашифрован в БД и НИКОГДА не напечатан (golden rule #5) ──────────
class _FakeCreds:
    def __init__(self, refresh_token: str):
        self.refresh_token = refresh_token


class _FakeFlow:
    def __init__(self, creds):
        self._creds = creds

    def run_local_server(self, **kw):
        return self._creds


def _flow_returning(token: str):
    class _F:
        @staticmethod
        def from_client_config(client_config, scopes=None):
            return _FakeFlow(_FakeCreds(token))

    return _F


def test_main_registers_and_never_prints_token(monkeypatch, capsys):
    asyncio.run(_delete_token(ACCT))
    monkeypatch.setattr(sys, "argv", _argv())
    monkeypatch.setattr(
        settings, "secrets_encryption_key", SecretStr(Fernet.generate_key().decode())
    )
    monkeypatch.setattr(settings, "google_ads_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_ads_client_secret", SecretStr("test-client-secret"))
    monkeypatch.setattr(reg, "InstalledAppFlow", _flow_returning(FAKE_TOKEN))

    reg.main()

    out = capsys.readouterr().out
    assert FAKE_TOKEN not in out  # ⛔ открытый токен в stdout — утечка секрета
    assert "зарегистрирован" in out  # сообщение об успехе есть

    row = asyncio.run(_get_token(ACCT))
    assert row is not None
    assert FAKE_TOKEN not in row.refresh_token_enc  # в БД — ШИФР
    assert decrypt(row.refresh_token_enc) == FAKE_TOKEN  # расшифровывается обратно
    assert row.login_customer_id == LOGIN  # дефис-форма нормализована
    asyncio.run(_delete_token(ACCT))
