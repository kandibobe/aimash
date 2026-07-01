"""Получить refresh token для Google Ads API + Google Sheets-экспорта (OAuth Desktop flow) и сохранить в .env.

При запуске откроется БРАУЗЕР на этой машине:
1. Войди тем Google-аккаунтом (gmail от Антона), у которого есть доступ к Google Ads «Aimash» (775-364-3025).
2. Если «Google hasn't verified this app» → Advanced → Go to … (норм для своего dev-приложения).
   Если «Access blocked» → добавь этот gmail в Test users в OAuth consent screen (или Publish → In Production).
3. На экране согласия будут ТРИ доступа: Google Ads, «… only files you open with this app» (drive.file)
   и «See all your Google Sheets spreadsheets» (spreadsheets.readonly). Разреши ВСЕ — иначе /sheets
   упадёт с invalid_scope, а §19.4.1 не прочитает чужую таблицу ключей. Скрипт впишет refresh_token
   в .env (GOOGLE_ADS_REFRESH_TOKEN) — токен не печатается.

Зачем drive.file: /sheets и §19.4.2 создают Google-таблицу (reports/sheets.py) — минимально достаточный
scope (доступ ТОЛЬКО к файлам, созданным приложением). Зачем spreadsheets.readonly: §19.4.1 «Ссылка на
Google Sheets» — читать ПРОИЗВОЛЬНУЮ таблицу менеджера (drive.file видит только своё). Google Ads-доступ
(adwords) от этого не меняется — токен получает все разрешения сразу.

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

# adwords — Google Ads API; drive.file — создание таблиц (/sheets, §19.4.2);
# spreadsheets.readonly — чтение произвольной таблицы менеджера (§19.4.1). Все scope в ОДНОМ токене:
# иначе Sheets-рефреш просит недостающий scope → invalid_scope. Держим в синхроне с reports.sheets.
SCOPES = [
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]
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

    print(">>> Открываю браузер для входа в Google. Войди gmail-ом с доступом к аккаунту Aimash.")
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("⚠️ refresh_token пуст — перезапусти и выдай согласие заново (prompt=consent).")
        sys.exit(1)

    granted = set(getattr(creds, "scopes", None) or [])
    if "https://www.googleapis.com/auth/drive.file" not in granted:
        print(
            "⚠️ Доступ drive.file НЕ выдан — /sheets продолжит падать с invalid_scope. "
            "Перезапусти и на экране согласия отметь оба доступа (Google Ads + drive.file)."
        )
    save_to_env(creds.refresh_token)
    print(
        "✅ refresh_token получен и сохранён в .env (GOOGLE_ADS_REFRESH_TOKEN). Токен не печатается.\n"
        "   Покрывает Google Ads + Google Sheets (/sheets). Перезапусти бота, чтобы подхватил новый токен."
    )


if __name__ == "__main__":
    main()
