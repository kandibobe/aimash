"""Получить refresh token для Google Ads API (OAuth Desktop flow) и сохранить в .env.

При запуске откроется БРАУЗЕР на этой машине:
1. Войди тем Google-аккаунтом (gmail от Антона), у которого есть доступ к Google Ads «Aimash» (775-364-3025).
2. Если «Google hasn't verified this app» → Advanced → Go to … (норм для своего dev-приложения).
   Если «Access blocked» → добавь этот gmail в Test users в OAuth consent screen (или Publish → In Production).
3. Разреши доступ. Скрипт сам впишет refresh_token в .env (GOOGLE_ADS_REFRESH_TOKEN) — токен не печатается.

Секреты (client_id/secret) берутся из .env, не из кода.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from core.config import settings  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/adwords"]
ENV_PATH = ROOT / ".env"


def save_to_env(token: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    line = f"GOOGLE_ADS_REFRESH_TOKEN={token}"
    if re.search(r"^GOOGLE_ADS_REFRESH_TOKEN=.*$", text, flags=re.M):
        text = re.sub(r"^GOOGLE_ADS_REFRESH_TOKEN=.*$", line, text, flags=re.M)
    else:
        text += ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def main() -> None:
    if not settings.google_ads_client_id or not settings.google_ads_client_secret:
        print("❌ Заполни GOOGLE_ADS_CLIENT_ID и GOOGLE_ADS_CLIENT_SECRET в .env")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": settings.google_ads_client_id,
            "client_secret": settings.google_ads_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print(">>> Открываю браузер для входа в Google. Войди gmail-ом с доступом к аккаунту Aimash.")
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("⚠️ refresh_token пуст — перезапусти и выдай согласие заново (prompt=consent).")
        sys.exit(1)

    save_to_env(creds.refresh_token)
    print("✅ refresh_token получен и сохранён в .env (GOOGLE_ADS_REFRESH_TOKEN). Токен не печатается.")


if __name__ == "__main__":
    main()
