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

import pytest

# Имя БД — ПЕР-ПРОЦЕССНОЕ (pid). Раньше одно общее "aimash_pytest.db": на Windows unlink
# молча падал OSError, если файл держал предыдущий (ещё не добитый) pytest-процесс, и сессия
# стартовала на ГРЯЗНОЙ базе прошлого прогона — флак-ассерты «профиль уже есть»/«лишние
# audit-строки»/IntegrityError в случайных местах. Per-pid имя убирает класс целиком.
_db = pathlib.Path(tempfile.gettempdir()) / f"aimash_pytest_{os.getpid()}.db"
for _stale in pathlib.Path(tempfile.gettempdir()).glob("aimash_pytest*.db*"):
    try:
        _stale.unlink()  # уборка хвостов прошлых сессий (вкл. -wal/-shm); занятые — пропускаем
    except OSError:
        pass

# Форсим временный SQLite (перекрывает .env/реальное окружение только во время тестов).
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db.as_posix()}"
# Матчим CI/прод-дефолт: пустой allow-list/read-list аккаунтов (замок fail-closed до конфигурации).
# Ловит класс «зелёно локально, красно в CI» из-за зависимости теста от локального .env.
os.environ["GOOGLE_ADS_ALLOWED_CUSTOMER_IDS"] = ""
os.environ["GOOGLE_ADS_READ_CUSTOMER_IDS"] = ""


@pytest.fixture(autouse=True)
def _reset_discovered_children_cache():
    """Изоляция: набор обнаруженных дочерних MCC (`ads.client._READ_DISCOVERED`/`_READ_CHILDREN_META`)
    — МОДУЛЬНЫЙ кэш. На машине с ЖИВЫМИ кредами ленивый само-обход (`ensure_read_children_discovered`,
    2026-07: /accounts, пикеры) наполняет его РЕАЛЬНЫМИ аккаунтами и ТЁК в следующие тесты — напр.
    get_stats видел «живых аккаунтов много» → возвращал need_account вместо read (флак только локально,
    в CI без кредов обход пуст). Чистим ДО и ПОСЛЕ каждого теста: кому набор нужен — задаёт его сам
    (`set_discovered_read_children*`). Убирает класс «обход тёк между тестами» целиком (а не заплатку)."""
    from ads.client import set_discovered_read_children, set_discovered_read_children_meta

    set_discovered_read_children([])
    set_discovered_read_children_meta([])
    yield
    set_discovered_read_children([])
    set_discovered_read_children_meta([])
