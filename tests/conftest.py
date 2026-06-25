"""pytest-конфиг: изолируем БД на временный SQLite-файл, чтобы write-путь
(store roundtrip) тестировался офлайн, без Postgres.

DATABASE_URL выставляется ДО импорта core.config/db.session (conftest pytest
импортирует раньше тест-модулей), поэтому движок db.session берёт именно его.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

_db = pathlib.Path(tempfile.gettempdir()) / "aimash_pytest.db"
try:
    if _db.exists():
        _db.unlink()  # чистый старт сессии тестов
except OSError:
    pass

# Форсим временный SQLite (перекрывает .env/реальное окружение только во время тестов).
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db.as_posix()}"
