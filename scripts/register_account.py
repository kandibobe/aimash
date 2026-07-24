"""Зарегистрировать per-account OAuth-токен (§8/Фаза 3: аккаунты под РАЗНЫМИ MCC).

Отличие от scripts/get_refresh_token.py: тот пишет ЕДИНСТВЕННЫЙ refresh-токен в .env (Draft и
тест-дочерние под одним MCC). Этот — для боевого мультиаккаунта: получает refresh-токен для
КОНКРЕТНОГО клиентского аккаунта и сохраняет его ЗАШИФРОВАННО в БД (oauth_tokens.refresh_token_enc,
core.secrets.encrypt) вместе с его login_customer_id (MCC этого аккаунта). Бот на старте грузит их
через ads.client.load_oauth_cache и собирает per-account клиента (ads.client.build_client).

⚠️ Требует SECRETS_ENCRYPTION_KEY (Fernet) — иначе шифровать нечем (core.secrets падает). Токен
НИКОГДА не печатается и не пишется в .env/логи (golden rule #5).

Запуск:  python -m scripts.register_account --account 111-222-3334 --login 555-666-7778
При запуске откроется БРАУЗЕР: войди Google-аккаунтом, у которого есть доступ к ЭТОМУ клиентскому
аккаунту через его MCC. Разреши оба доступа (Google Ads + drive.file для /sheets).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
from sqlalchemy import select  # noqa: E402

from core.config import normalize_customer_id, settings  # noqa: E402
from core.secrets import encrypt  # noqa: E402
from db.models import OAuthToken  # noqa: E402
from db.session import Session, init_db  # noqa: E402

SCOPES = [
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/drive.file",
]


async def _upsert(account: str, refresh_token_enc: str, login_customer_id: str) -> None:
    await init_db()
    async with Session() as s:
        row = (
            await s.execute(select(OAuthToken).where(OAuthToken.account == account))
        ).scalar_one_or_none()
        if row is None:
            s.add(
                OAuthToken(
                    account=account,
                    refresh_token_enc=refresh_token_enc,
                    login_customer_id=login_customer_id,
                )
            )
        else:
            row.refresh_token_enc = refresh_token_enc
            row.login_customer_id = login_customer_id
        await s.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description="Регистрация per-account OAuth-токена (шифр. в БД).")
    ap.add_argument("--account", required=True, help="клиентский customer_id (111-222-3334)")
    ap.add_argument("--login", required=True, help="MCC (login_customer_id) этого аккаунта")
    args = ap.parse_args()

    account = normalize_customer_id(args.account)
    login = normalize_customer_id(args.login)
    if not account or not login:
        print("❌ --account и --login должны содержать цифры customer_id")
        sys.exit(1)

    if not settings.secrets_encryption_key.get_secret_value():
        print("❌ Задай SECRETS_ENCRYPTION_KEY (Fernet.generate_key()) — токен шифруется at-rest.")
        sys.exit(1)
    if (
        not settings.google_ads_client_id
        or not settings.google_ads_client_secret.get_secret_value()
    ):
        print("❌ Заполни GOOGLE_ADS_CLIENT_ID и GOOGLE_ADS_CLIENT_SECRET в .env")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": settings.google_ads_client_id,
            "client_secret": settings.google_ads_client_secret.get_secret_value(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    print(
        f">>> Открываю браузер. Войди gmail-ом с доступом к аккаунту {account} (через MCC {login})."
    )
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("⚠️ refresh_token пуст — перезапусти и выдай согласие заново (prompt=consent).")
        sys.exit(1)

    enc = encrypt(creds.refresh_token)  # шифруем ДО записи; открытый токен в БД не попадает
    asyncio.run(_upsert(account, enc, login))
    print(
        f"✅ Аккаунт {account} зарегистрирован (MCC {login}). Токен зашифрован в oauth_tokens, "
        "не печатается. Перезапусти бота (load_oauth_cache подхватит на старте)."
    )


if __name__ == "__main__":
    main()
