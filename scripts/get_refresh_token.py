"""Получить refresh token для Google Ads API + Google Sheets-экспорта (OAuth Desktop flow) и сохранить в .env.

    python scripts/get_refresh_token.py                     # общий токен: Ads + Sheets → GOOGLE_ADS_REFRESH_TOKEN
    python scripts/get_refresh_token.py --sheets            # ТОЛЬКО Sheets/Drive  → SHEETS_REFRESH_TOKEN
    python scripts/get_refresh_token.py --sheets --remote   # то же, но входит ЗАКАЗЧИК со СВОЕГО компьютера

--sheets: войти НУЖНЫМ gmail-ом (аккаунт-хранилище таблиц, напр. myhalads@gmail.com) — созданные ботом
таблицы будут лежать в ЕГО «Моём диске» и есть его квоту. Просим ТОЛЬКО drive.file (см. SHEETS_SCOPES):
scope adwords не нужен (доступа к MCC у этого аккаунта может не быть), а sensitive-scope
spreadsheets.readonly нельзя — на него Google отвечает «This app is blocked» неверифицированному
приложению, и владелец аккаунта просто не сможет дать согласие. Ads-токен остаётся прежним;
reports.sheets предпочтёт SHEETS_REFRESH_TOKEN для создания/шаринга таблиц, а чужие таблицы (§19.4.1)
будет читать Ads-токеном, у которого readonly есть.

Без --remote откроется БРАУЗЕР на ЭТОЙ машине:
1. Войди тем Google-аккаунтом (gmail от Антона), у которого есть доступ к Google Ads «Aimash» (775-364-3025).
   Для --sheets — аккаунтом, на чьём Drive должны храниться таблицы (Google Ads ему не нужен).
2. Если «Google hasn't verified this app» → Advanced → Go to … (норм для своего dev-приложения).
   Если «Access blocked»/«This app is blocked» → проверь OAuth consent screen: Publishing status должен
   быть In production (в Testing пускают только Test users, и токен там живёт 7 дней).
3. На экране согласия: для --sheets — ОДИН доступ «… only files you open with this app» (drive.file);
   для общего токена — ТРИ (Google Ads + drive.file + «See all your Google Sheets spreadsheets»).
   Разреши ВСЕ, что показаны, — иначе /sheets упадёт с invalid_scope, а §19.4.1 не прочитает чужую
   таблицу ключей. Скрипт впишет refresh_token в .env — токен не печатается.

--remote: владелец аккаунта — НЕ у этой машины (напр. Drive-аккаунт заказчика). run_local_server тут не
годится: он ждёт редиректа на СВОЙ localhost, т.е. браузер обязан быть на машине скрипта. OOB-поток
(«код на экране Google») Google отключил в 2023 — остался loopback-редирект. Поэтому: скрипт печатает
ссылку согласия → заказчик открывает её у себя и разрешает доступ → Google редиректит на
http://localhost:8765/?code=… , страница у него НЕ открывается (это нормально), но код виден в АДРЕСНОЙ
СТРОКЕ → заказчик присылает эту строку → скрипт меняет код на refresh_token. Код одноразовый и живёт
~10 минут. Пароль заказчика к нам не попадает; консент-скрин должен быть Published (In production),
иначе Google выдаёт refresh-токен всего на 7 дней.

Зачем drive.file: /sheets и §19.4.2 создают Google-таблицу (reports/sheets.py) — минимально достаточный
scope (доступ ТОЛЬКО к файлам, созданным приложением). Зачем spreadsheets.readonly: §19.4.1 «Ссылка на
Google Sheets» — читать ПРОИЗВОЛЬНУЮ таблицу менеджера (drive.file видит только своё). Google Ads-доступ
(adwords) от этого не меняется — токен получает все разрешения сразу.

Секреты (client_id/secret) берутся из .env, не из кода.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Google возвращает scope в другом порядке/с добавками (напр. openid) — oauthlib иначе роняет обмен
# «Scope has changed». Ставим ДО импорта flow: библиотека читает переменную при обмене кода.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google_auth_oauthlib.flow import Flow, InstalledAppFlow  # noqa: E402

from core.config import settings  # noqa: E402

# Токен аккаунта-ХРАНИЛИЩА (--sheets): ТОЛЬКО drive.file — он non-sensitive, верификация приложения
# не нужна. spreadsheets.readonly у Google SENSITIVE: неверифицированному приложению Google отвечает
# «This app is blocked… tried to access sensitive info», и владелец аккаунта не сможет дать согласие.
# Держим в синхроне с reports.sheets.SHEETS_SCOPES (гард — tests/test_keyword_sheets.py).
SHEETS_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# Общий (Ads) токен — НАШ аккаунт, согласие уже выдано: здесь sensitive-scope допустим и нужен —
# spreadsheets.readonly читает ПРОИЗВОЛЬНУЮ таблицу менеджера (§19.4.1, /kw add).
SCOPES = [
    "https://www.googleapis.com/auth/adwords",
    *SHEETS_SCOPES,
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]
ENV_PATH = ROOT / ".env"

# Фиксированный loopback-редирект для --remote: порт нельзя брать случайный (run_local_server так делает),
# он попадает в ссылку согласия и должен совпасть при обмене кода. Страница по нему не поднимается —
# заказчик копирует код из адресной строки.
REMOTE_REDIRECT = "http://localhost:8765/"


def save_to_env(token: str, var: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    line = f"{var}={token}"
    if re.search(rf"^{var}=.*$", text, flags=re.M):
        text = re.sub(rf"^{var}=.*$", line, text, flags=re.M)
    else:
        text += ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def _extract_code(text: str) -> str | None:
    """Код авторизации из того, что прислал заказчик: строка адресной строки ИЛИ голый код.

    Возвращает None, если кода нет (в т.ч. когда Google вернул ?error=access_denied).
    """
    text = text.strip().strip("<>").strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        query = parse_qs(urlparse(text).query)
        code = query.get("code", [""])[0]
        return code or None
    # голый код: у Google это «4/0A…» — пробелов не содержит
    return None if " " in text else text


def _remote_credentials(client_config: dict, scopes: list[str]):
    """Согласие даёт ЧУЖОЙ браузер: печатаем ссылку, принимаем обратно строку с одноразовым кодом."""
    flow = Flow.from_client_config(client_config, scopes=scopes, redirect_uri=REMOTE_REDIRECT)
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    print(
        "\n>>> ССЫЛКА СОГЛАСИЯ — отправь её владельцу Google-аккаунта (текст ниже можно скопировать целиком):\n"
        "\n"
        "———————————————————————————————————————————————————————————\n"
        f"1. Открой ссылку в браузере, где ты вошёл НУЖНЫМ gmail-ом (или выбери его на экране Google):\n"
        f"{auth_url}\n"
        "2. Увидишь «Google hasn't verified this app» → Advanced → Go to … (unsafe). Это нормально.\n"
        "3. Разреши доступ, который покажет Google: «файлы, созданные этим приложением» (drive.file).\n"
        "   К почте и остальному Диску доступа приложение не получает.\n"
        "4. После «Разрешить» браузер уйдёт на http://localhost:8765/... и покажет «Не удалось открыть\n"
        "   страницу» — так и должно быть. СКОПИРУЙ ВСЮ СТРОКУ ИЗ АДРЕСНОЙ СТРОКИ и пришли мне.\n"
        "5. Строка одноразовая и живёт ~10 минут. Не успел — скажи, пришлю новую ссылку.\n"
        "———————————————————————————————————————————————————————————\n"
    )
    answer = input(">>> Вставь сюда строку, которую прислал владелец аккаунта, и нажми Enter:\n")
    code = _extract_code(answer)
    if not code:
        print(
            "❌ Кода авторизации в этой строке нет. Нужна строка вида\n"
            "   http://localhost:8765/?code=4/0A...&scope=...\n"
            "   (если там error=access_denied — доступ не был разрешён; запусти скрипт заново)."
        )
        sys.exit(1)

    flow.fetch_token(code=code)  # код в лог/консоль не выводим — это одноразовый credential
    return flow.credentials


def main() -> None:
    sheets_only = "--sheets" in sys.argv
    remote = "--remote" in sys.argv
    scopes = SHEETS_SCOPES if sheets_only else SCOPES
    var = "SHEETS_REFRESH_TOKEN" if sheets_only else "GOOGLE_ADS_REFRESH_TOKEN"
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

    if remote:
        creds = _remote_credentials(client_config, scopes)
    else:
        print(
            ">>> Открываю браузер для входа в Google. Войди "
            + (
                "gmail-ом, на чьём Drive должны ХРАНИТЬСЯ таблицы."
                if sheets_only
                else "gmail-ом с доступом к аккаунту Aimash."
            )
        )
        flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("⚠️ refresh_token пуст — перезапусти и выдай согласие заново (prompt=consent).")
        sys.exit(1)

    granted = set(getattr(creds, "scopes", None) or [])
    if "https://www.googleapis.com/auth/drive.file" not in granted:
        print(
            "⚠️ Доступ drive.file НЕ выдан — /sheets продолжит падать с invalid_scope. "
            "Перезапусти и на экране согласия отметь ВСЕ доступы."
        )
    save_to_env(creds.refresh_token, var)
    print(
        f"✅ refresh_token получен и сохранён в .env ({var}). Токен не печатается.\n"
        + (
            "   Таблицы (/sheets, ключи визарда) теперь создаются на ЭТОМ Google-аккаунте.\n"
            "   Проверь: python scripts/check_sheets_share.py — он печатает владельца файла.\n"
            if sheets_only
            else "   Покрывает Google Ads + Google Sheets (/sheets).\n"
        )
        + "   Перезапусти бота, чтобы подхватил новый токен."
    )


if __name__ == "__main__":
    main()
