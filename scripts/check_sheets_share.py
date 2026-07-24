"""Живая проверка: открывается ли созданная ботом Google-таблица ВСЕМ по ссылке (anyone-with-link).

Зачем: при сбое шаринга бот молча оставляет таблицу приватной (reports.sheets._share_anyone никогда
не raise) — из кода не понять, работает ли доступ на ЭТОМ Google-аккаунте. Скрипт даёт факт:

1. печатает scopes, реально выданные refresh-токену (сам токен НЕ печатается — правило 5);
2. создаёт временную таблицу;
3. печатает ВЛАДЕЛЬЦА файла (drive.files.get owners) и сверяет с SHEETS_OWNER_EMAIL, если он задан;
4. открывает её anyone-with-link (role по умолчанию writer — как таблицы ключей визарда);
5. НЕЗАВИСИМО перечитывает права через drive.permissions().list — доказательство, а не «мы позвали API»;
6. печатает ссылку и удаляет таблицу (--keep оставит её, чтобы открыть в браузере инкогнито).

Владелец создаваемых файлов — Google-аккаунт, чьим refresh-токеном ходит Sheets: SHEETS_REFRESH_TOKEN,
если задан, иначе GOOGLE_ADS_REFRESH_TOKEN. Таблицы лежат на ЕГО Диске и едят его квоту (15 ГБ).
Google Ads НЕ трогается (мутаций нет).

Запуск:  PYTHONIOENCODING=utf-8 python scripts/check_sheets_share.py [--role reader|writer] [--keep]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _win_console import enable_utf8  # noqa: E402

enable_utf8()

from core.logging import redact_text  # noqa: E402
from reports.sheets import (  # noqa: E402
    SHARE_OFF,
    _build_drive_service,
    _build_service,
    _oauth_credentials,
    _share_anyone,
    is_shared,
)

TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"


def print_granted_scopes() -> None:
    """Scopes, реально выданные токену. Печатаем ТОЛЬКО поле scope — access_token не выводим."""
    import google.auth.transport.requests as greq

    creds = _oauth_credentials()
    req = greq.Request()
    try:
        creds.refresh(req)  # получаем access_token из refresh_token
        resp = req(url=f"{TOKENINFO}?access_token={creds.token}", method="GET")
        import json

        body = json.loads(resp.data.decode("utf-8"))
        scopes = (body.get("scope") or "").split()
    except Exception as e:  # noqa: BLE001 — диагностика не должна падать раньше самого теста
        print(f"⚠️  не удалось прочитать scopes токена: {redact_text(str(e))[:200]}")
        return
    print("Выданные токену scopes:")
    for s in scopes:
        print(f"  · {s}")
    if not any(s.endswith("/drive.file") or s.endswith("/drive") for s in scopes):
        print("❌ Нет drive.file — создание таблиц и шаринг работать НЕ будут. Нужен re-OAuth:")
        print("   python scripts/get_refresh_token.py")


def check_owner(drive, sid: str) -> bool:
    """На ЧЬЁМ Google-аккаунте оказался файл. Читаем owners у самого файла (drive.file это позволяет —
    файл создан нами), а не «мы же слали токен такой-то». SHEETS_OWNER_EMAIL задан и не совпал ⇒
    False: значит таблицы копятся на чужом Диске (и в чужой квоте), а это обнаруживается месяцами
    позже. Личный gmail всегда отдаёт emailAddress владельца."""
    from core.config import settings

    try:
        meta = drive.files().get(fileId=sid, fields="owners(emailAddress,displayName)").execute()
        owners = [o.get("emailAddress", "") for o in meta.get("owners", [])]
    except Exception as e:  # noqa: BLE001 — не роняем сам тест шаринга
        text = redact_text(str(e))
        print(f"⚠️  не удалось прочитать владельца файла: {text[:300]}")
        if "accessNotConfigured" in text or "drive.googleapis.com" in text:
            # Живой прогон 2026-07-13: в проекте был включён Sheets API, но НЕ Drive API — таблицы
            # создавались, а permissions.create падал 403, т.е. ссылка НИКОГДА не была публичной.
            print(
                "❗ В Google Cloud-проекте НЕ включён Google Drive API — шаринг работать не может."
            )
            print("   Включи: console.developers.google.com → APIs → Google Drive API → Enable")
        return True

    print(f"Владелец файла (Drive-аккаунт хранения): {', '.join(owners) or '—'}")
    expected = (settings.sheets_owner_email or "").strip().lower()
    if not expected:
        print("   (SHEETS_OWNER_EMAIL не задан — сверять не с чем)")
        return True
    if any(o.strip().lower() == expected for o in owners):
        print(f"✅ Совпадает с SHEETS_OWNER_EMAIL={expected}")
        return True
    print(f"❌ ОЖИДАЛСЯ {expected} — таблицы создаются НЕ на том аккаунте.")
    print("   Выдай токен нужного gmail: python scripts/get_refresh_token.py --sheets")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="writer", choices=("reader", "writer"))
    ap.add_argument("--keep", action="store_true", help="не удалять таблицу (открыть в браузере)")
    args = ap.parse_args()

    print_granted_scopes()
    print()

    sheets = _build_service()
    drive = _build_drive_service()

    created = (
        sheets.spreadsheets()
        .create(
            body={"properties": {"title": "aimash-share-check"}},
            fields="spreadsheetId,spreadsheetUrl",
        )
        .execute()
    )
    sid = created["spreadsheetId"]
    url = created.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sid}"
    print(f"✅ Таблица создана: {url}")

    owner_ok = check_owner(drive, sid)

    status = _share_anyone(sid, role=args.role, drive_service=drive)
    if status == SHARE_OFF:
        print("🔒 SHEETS_PUBLIC_LINK=false — публичный доступ ВЫКЛЮЧЕН владельцем (это не сбой).")
    elif not is_shared(status):
        print("❌ Drive отказал в публичном доступе. Причина — в логе выше (sheets-share: …).")
        print("   Типично: политика Google Workspace запрещает ссылки «для всех».")

    # Независимая проверка: что РЕАЛЬНО записано в правах файла.
    try:
        perms = (
            drive.permissions()
            .list(fileId=sid, fields="permissions(id,type,role)")
            .execute()
            .get("permissions", [])
        )
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  не удалось перечитать права: {redact_text(str(e))[:200]}")
        perms = []
    print(f"Права файла: {perms}")
    anyone = [p for p in perms if p.get("type") == "anyone"]
    if anyone:
        print(f"✅ ДОСТУПНО ВСЕМ ПО ССЫЛКЕ: role={anyone[0].get('role')}")
    else:
        print("❌ Публичного доступа НЕТ — ссылка откроется только владельцу аккаунта бота.")

    if args.keep:
        print(f"\nТаблица оставлена (проверьте ссылку в режиме инкогнито): {url}")
    else:
        try:
            drive.files().delete(fileId=sid).execute()
            print("\n🧹 Временная таблица удалена.")
        except Exception as e:  # noqa: BLE001 — уборка не должна маскировать диагноз выше
            print(f"\n⚠️  не удалось удалить временную таблицу {sid}: {redact_text(str(e))[:200]}")
    return 0 if (anyone and owner_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
