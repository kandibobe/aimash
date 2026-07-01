"""pytest-конфиг: изолируем БД на временный SQLite-файл, чтобы write-путь
(store roundtrip) тестировался офлайн, без Postgres.

DATABASE_URL выставляется ДО импорта core.config/db.session (conftest pytest
импортирует раньше тест-модулей), поэтому движок db.session берёт именно его.
Здесь же ФОРСИМ пустой allow-list аккаунтов — чтобы локальный прогон совпадал с CI/проду
до конфигурации (fail-closed замок). Иначе env-зависимый тест зелёный локально (в .env
allow-list задан), но красный в CI — как случилось с §20-визардом. Тесты, которым нужен
разрешённый аккаунт, задают его ЯВНО (monkeypatch settings), а не через окружение.
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
# Матчим CI/прод-дефолт: пустой allow-list/read-list аккаунтов (замок fail-closed до конфигурации).
# Ловит класс «зелёно локально, красно в CI» из-за зависимости теста от локального .env.
os.environ["GOOGLE_ADS_ALLOWED_CUSTOMER_IDS"] = ""
os.environ["GOOGLE_ADS_READ_CUSTOMER_IDS"] = ""
