"""Гард: уборка временных БД не трогает базу ЖИВОГО соседнего pytest-процесса.

Класс, который здесь закрывается. `tests/conftest.py` чистил хвосты глобом `aimash_pytest*.db*`
— по файлам ВСЕХ pid'ов, а не только своего. Пока прогон один, это незаметно. Запусти второй
pytest (обычный приём: гонять подмножество тестов, пока идёт полный прогон) — и он на импорте
conftest, ещё до первого теста, удаляет БД у работающего первого. `NullPool` не держит хендл между
тестами, поэтому unlink проходит даже на Windows, а SQLite при следующем подключении молча
пересоздаёт файл ПУСТЫМ.

Наблюдалось это как `no such table: agent_run_events` в случайных `apply_*`-тестах и списывалось
на «флак блокировки SQLite» — диагноз был неверный: блокировки тут нет вовсе, есть удалённая
из-под ног база. С Волны 3 цена выросла: `record_money_event` fail-closed, нет таблицы — нет
мутации, поэтому пропадала не пара интеграционных тестов, а весь денежный путь.

Проверяем ПРЕДИКАТ, а не побочный эффект импорта: conftest импортируется один раз за процесс, и
воспроизвести его уборку внутри теста нельзя — а предикат и есть то место, где решение принимается.
"""

from __future__ import annotations

import os
import pathlib
import time

from conftest import _OWN_DB_PREFIX, _REAP_AFTER_S, _reapable


def _touch(path: pathlib.Path, age_s: float) -> pathlib.Path:
    """Файл с mtime заданного возраста — модель «чужой прогон писал N секунд назад»."""
    path.write_bytes(b"x")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_foreign_fresh_db_is_not_reaped(tmp_path):
    """Свежий файл чужого pid — это РАБОТАЮЩИЙ прогон. Не трогаем.

    Ровно этот случай и ломал соседа: возраст 0 с, а старый код удалял безусловно."""
    foreign = _touch(tmp_path / "aimash_pytest_99999999.db", age_s=0.0)

    assert _reapable(foreign, time.time()) is False


def test_foreign_stale_db_is_reaped(tmp_path):
    """Хвост мёртвой сессии убирать по-прежнему надо — иначе правка выродится в «не чистим никогда»
    и temp будет расти файлом за прогон."""
    stale = _touch(tmp_path / "aimash_pytest_99999999.db", age_s=_REAP_AFTER_S + 60)

    assert _reapable(stale, time.time()) is True


def test_own_pid_files_are_reaped_regardless_of_age(tmp_path):
    """Свой pid — всегда наш: либо текущая БД, либо хвост предшественника, которому pid достался
    повторно. Оба случая обязаны уйти, иначе вернётся исходный класс «стартовали на ГРЯЗНОЙ базе».
    Спутники WAL (`-wal`/`-shm`) снимаются вместе с базой — отсюда сравнение по префиксу."""
    now = time.time()
    for suffix in ("", "-wal", "-shm"):
        own = _touch(tmp_path / f"{_OWN_DB_PREFIX}{suffix}", age_s=0.0)
        assert _reapable(own, now) is True, suffix


def test_own_prefix_belongs_to_this_process():
    """Префикс собран из ЖИВОГО pid, а не из литерала: разъедься они — предикат начнёт считать
    свою базу чужой и перестанет чистить хвосты, оставаясь при этом зелёным."""
    assert _OWN_DB_PREFIX == f"aimash_pytest_{os.getpid()}.db"
