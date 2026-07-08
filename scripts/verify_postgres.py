"""§13 Верификация прод-БД на PostgreSQL: миграции чисто накатываются + смоук-чтение схемы.

Зачем: дефолт разработки — SQLite (init_db + аддитивный self-heal). Прод обязан идти на Postgres,
где ИСТИНА схемы — Alembic-миграции (heal_sqlite_schema НЕ применяется). Этот скрипт подтверждает,
что на ЧИСТОМ Postgres миграции доходят до head и рантайм-схема читается.

Запуск (DATABASE_URL ДОЛЖЕН указывать на Postgres):
    DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db python scripts/verify_postgres.py

Шаги:
1. Проверить, что DSN — Postgres (не sqlite), иначе отказ с инструкцией.
2. `alembic upgrade head` (subprocess) — накатить миграции до текущего head (0021_admins_runtime).
3. Сверить alembic current == head.
4. init_db() — на Postgres это no-op create_all (таблицы уже созданы миграциями); heal_sqlite
   НЕ вызывается. Служит детектором дрейфа «модель ⟂ миграции» (создал бы недостающую таблицу).
5. Смоук-чтение ключевых таблиц (proposals/audit_log/whitelist/bug_reports) — SELECT count.

exit 0 — всё чисто; exit 1 — любой сбой. Пароль БД в выводе НЕ печатаем (маскируем DSN).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

# Windows-консоль (cp1251) роняет emoji/кириллицу в выводе → UTF-8 (общий хелпер).
from _win_console import enable_utf8  # noqa: E402

enable_utf8()

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sqlalchemy import func, select  # noqa: E402

from core.config import settings  # noqa: E402

_HEAD = "0019_bug_reports"  # текущий head миграций (обновлять при добавлении ревизий)


def _mask_dsn(dsn: str) -> str:
    """DSN без пароля: scheme://user@host:port/db (golden rule #5 — пароль не в выводе)."""
    try:
        u = urlsplit(dsn)
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        user = f"{u.username}@" if u.username else ""
        return f"{u.scheme}://{user}{host}{port}{u.path}"
    except Exception:  # noqa: BLE001 — не роняем верификацию из-за форматирования
        return "<dsn>"


def _run_alembic_upgrade() -> bool:
    print("2) alembic upgrade head …")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        print("   ❌ миграции упали:\n" + (proc.stderr.strip() or "<нет stderr>"))
        return False
    print("   ✅ миграции накатаны")
    return True


def _check_alembic_head() -> bool:
    print(f"3) сверка alembic current == head ({_HEAD}) …")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
    )
    out = (proc.stdout + proc.stderr).strip()
    print("   " + (out or "<пусто>"))
    if _HEAD not in out:
        print(f"   ❌ current не на {_HEAD}")
        return False
    print("   ✅ на head")
    return True


async def _smoke_read() -> bool:
    from db.models import AuditLog, BugReport, Proposal, Whitelist
    from db.session import Session, init_db

    print("4) init_db() (детектор дрейфа модель⟂миграции; heal_sqlite не для Postgres) …")
    await init_db()
    print("   ✅ init_db прошёл")

    print("5) смоук-чтение ключевых таблиц …")
    async with Session() as s:
        for model in (Proposal, AuditLog, Whitelist, BugReport):
            n = (await s.execute(select(func.count()).select_from(model))).scalar_one()
            print(f"   • {model.__tablename__:16} rows={n}")
    print("   ✅ схема читается")
    return True


async def main() -> int:
    dsn = settings.database_url.get_secret_value()
    print(f"=== Verify Postgres · {_mask_dsn(dsn)} ===")
    print("1) проверка, что DSN — Postgres …")
    if dsn.startswith("sqlite"):
        print(
            "   ❌ DATABASE_URL указывает на SQLite. Задай Postgres-DSN, напр.:\n"
            "      DATABASE_URL=postgresql+asyncpg://aimash:***@localhost:5432/aimash"
        )
        return 1
    if "postgres" not in dsn:
        print(f"   ⚠️ необычный DSN scheme — ожидался postgresql+asyncpg ({_mask_dsn(dsn)})")
    print("   ✅ Postgres DSN")

    if not _run_alembic_upgrade():
        return 1
    if not _check_alembic_head():
        return 1
    try:
        await _smoke_read()
    except Exception as e:  # noqa: BLE001 — сообщаем сбой чтения без падения наружу
        print(f"   ❌ смоук-чтение упало: {type(e).__name__}: {e}")
        return 1

    print("\nГотово ✅ — Postgres-схема верифицирована (миграции→head + чтение). §13/§18.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
