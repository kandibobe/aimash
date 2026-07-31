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
import time
from dataclasses import dataclass

import pytest

# Имя БД — ПЕР-ПРОЦЕССНОЕ (pid). Раньше одно общее "aimash_pytest.db": на Windows unlink
# молча падал OSError, если файл держал предыдущий (ещё не добитый) pytest-процесс, и сессия
# стартовала на ГРЯЗНОЙ базе прошлого прогона — флак-ассерты «профиль уже есть»/«лишние
# audit-строки»/IntegrityError в случайных местах. Per-pid имя убирает класс целиком.
_TMP = pathlib.Path(tempfile.gettempdir())
_db = _TMP / f"aimash_pytest_{os.getpid()}.db"

# …но сама уборка хвостов класс возвращала: глоб бьёт файлы ВСЕХ pid'ов, а не только своего.
# Второй одновременно запущенный pytest (обычный приём — гонять подмножество, пока идёт полный
# прогон) удалял БД У РАБОТАЮЩЕГО соседа: с NullPool между тестами открытых хендлов нет, поэтому
# unlink проходит и на Windows, а SQLite молча пересоздаёт файл ПУСТЫМ. Дальше сыплется
# `no such table: …` в СЛУЧАЙНЫХ тестах — это и списывалось на «блокировку SQLite». С Волны 3 оно
# ещё и валит денежный путь: `record_money_event` fail-closed, нет таблицы — нет мутации.
# Признак живого владельца — свежий mtime: работающий прогон пишет в свою БД непрерывно.
_REAP_AFTER_S = 3600.0
_OWN_DB_PREFIX = f"aimash_pytest_{os.getpid()}.db"


def _reapable(path: pathlib.Path, now: float) -> bool:
    """Можно ли удалить файл БД: свой pid (вкл. -wal/-shm) — да, чужой СВЕЖИЙ — нет."""
    if path.name.startswith(_OWN_DB_PREFIX):
        return True  # хвост предшественника с тем же pid — наш, добиваем
    return now - path.stat().st_mtime >= _REAP_AFTER_S


_now = time.time()
for _stale in _TMP.glob("aimash_pytest*.db*"):
    try:
        if _reapable(_stale, _now):
            _stale.unlink()  # хвост мёртвой сессии (вкл. -wal/-shm); занятые — пропускаем
    except OSError:
        pass

# Форсим временный SQLite (перекрывает .env/реальное окружение только во время тестов).
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db.as_posix()}"
# Матчим CI/прод-дефолт: пустой allow-list/read-list аккаунтов (замок fail-closed до конфигурации).
# Ловит класс «зелёно локально, красно в CI» из-за зависимости теста от локального .env.
os.environ["GOOGLE_ADS_ALLOWED_CUSTOMER_IDS"] = ""
os.environ["GOOGLE_ADS_READ_CUSTOMER_IDS"] = ""
# BZ-1: рубильник и файл-флаг НЕ должны течь из окружения машины в тесты (включённый на хосте
# рубильник красил бы весь write-слой в непонятный PermissionError). Тесты рубильника задают
# своё состояние явно (monkeypatch.setenv / settings.kill_switch_file).
os.environ.pop("DISABLE_ALL_MUTATIONS", None)
os.environ["KILL_SWITCH_FILE"] = ""
# И login-MCC пустой (как CI без .env). Иначе ленивый само-обход дочерних
# (ads.client.ensure_read_children_discovered, 2026-07: /accounts + пикеры → _read_account_rows)
# дёргал бы РЕАЛЬНЫЙ Google Ads SDK на машине с живым .env: тест ~30 с (сетевой round-trip) и флак
# SQLite (долгий await в loop'е рвал соседние тесты). Тесты обхода MCC задают login явно (_login()).
os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = ""
os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_IDS"] = ""

# bot.main — ПЕРВЫМ (как в проде: `python -m bot.main` — точка входа), ПОСЛЕ форса env выше.
# Гарантирует, что ни один хендлер-модуль не импортируется РАНЬШЕ bot.main. Иначе его
# `import bot.main as bm` втягивает bot.main на середине себя: register_all регистрирует хендлеры
# этого модуля уже ПОСЛЕ on_text (catch-all перестаёт быть последним) и теряет ре-экспорт имён —
# сессионный флак «btn_report пропал»/«on_text не последний», зависящий от порядка тест-модулей.
# Класс закрыт для всей сессии; страховка имён — module __getattr__ в bot/main.py, инвариант —
# tests/test_handler_order.py.
import bot.main  # noqa: E402,F401
from ads.freshness import Snapshot, SnapshotKind, attach_freshness  # noqa: E402


# ── Дублёр confirm-стора для ПРЯМЫХ вызовов ads.mutations.apply_* ────────────────────────────
#
# Был скопирован в 9 тест-файлов почти байт-в-байт. Копии разошлись бы на первом же расширении
# протокола — и разошлись: Волна 1.1 добавила `get_confirmed` (гейт A читает снимок из стора, а не
# из аргумента), и без него КАЖДАЯ копия молча превращала STRICT-операцию в «снимка нет ⇒ отказ».
# Один дублёр = одно место, где зеркалится контракт `confirm.store.ConfirmStore`.
@dataclass
class FakeProposal:
    """Снимок черновика, какой отдал бы настоящий стор.

    `params` по умолчанию АТТЕСТОВАН (собирается настоящим `attach_freshness`): подавляющее
    большинство тестов проверяет SDK-путь и гейты замка/подтверждения, а не свежесть, и требовать
    от каждого руками собрать снимок значило бы утопить их предмет в бойлерплейте. Тесты ПРО
    свежесть задают `params` явно — в частности `attested({...})` без `before` даёт черновик, у
    которого состояние прочитать НЕ удалось."""

    operation: str
    status: str = "confirmed"
    user_initiated: bool = False
    # Волна 1.4: второй бит провенанса. None ⇒ зеркалим user_initiated — расщепление битов у
    # настоящего ConfirmStore проверяется в tests/test_provenance_gate.py.
    origin_human_turn: bool | None = None
    params: dict | None = None
    # B1-4: blast-radius кап группирует историю ПО АККАУНТУ — зеркалим поле настоящего снимка.
    customer_id: str = "7753643025"

    def __post_init__(self) -> None:
        if self.origin_human_turn is None:
            self.origin_human_turn = self.user_initiated
        if self.params is None:
            self.params = attested({}, {"kind": "test"})


class FakeConfirmStore:
    """confirm_store-дублёр: `get_confirmed` (гейт A, до claim) + атомарный одноразовый `claim`."""

    def __init__(self, proposal: object | None = None):
        self._p = proposal
        self.finalized = False
        self._claimed = False

    async def get_confirmed(self, confirmation_id):
        # Зеркало ConfirmStore.get_confirmed: read-only, статус НЕ меняет (claim ещё впереди).
        return self._p

    async def claim(self, confirmation_id, *, operation):
        # Зеркало ConfirmStore.claim: атомарно/одноразово, только confirmed + совпавшая операция.
        p = self._p
        if p is None or p.status != "confirmed" or p.operation != operation or self._claimed:
            return None
        self._claimed = True
        return p

    async def recent_money_params(self, customer_id, *, operations, window_hours=24):
        # Зеркало ConfirmStore.recent_money_params (B1-4): у дублёра истории нет — пустое окно.
        # Тесты капа с историей строят её на НАСТОЯЩЕМ сторе (tests/test_budget_blast_radius.py).
        return []

    async def finalize(self, confirmation_id, *, result):
        self.finalized = True
        self.result = result


def attested(params: dict, before: dict | None = None) -> dict:
    """params с аттестацией свежести — как их кладёт карточка бота (Волна 1.1).

    Тест, который строит черновик руками и зовёт `execute_confirmed`/`apply_*`, обязан пройти гейты
    свежести, а не обойти их. Собираем аттестацию ТЕМ ЖЕ хелпером (`ads.freshness.attach_freshness`),
    а не литералом: изменится форма маркера — тесты поедут за кодом, а не начнут врать. `before=None`
    даёт честное «прочитать не смогли» (UNREADABLE) — для проверок отказа STRICT-операции."""
    snap = (
        Snapshot(SnapshotKind.OK, before)
        if before is not None
        else Snapshot(SnapshotKind.UNREADABLE, reason="test")
    )
    return attach_freshness(params, snap)


_schema_ready = False


@pytest.fixture(autouse=True)
async def _ensure_schema():
    """Схема тестовой БД поднимается ОДИН раз за процесс, до первого теста.

    Раньше её создавал тот тест, которому она понадобилась (`await init_db()` в теле), а
    юнит-тесты вертикали `ads.mutations.apply_*` обходились фейковым стором и БД не касались
    вовсе. Волна 3 это положение сняла: `_require_confirmation` пишет событие денежного пути
    fail-closed ДО `claim`, и «схемы нет» стало законным отказом исполнять мутацию.

    Чинится фикстурой, а не послаблением гарда: в проде эта зависимость и так жёсткая — `claim`
    ходит в ТУ ЖЕ базу, и конфигурации, где денежный путь работает без неё, не существует.
    Отсутствие схемы в тестах было удобством фейкового стора, а не свойством системы."""
    global _schema_ready
    if not _schema_ready:
        from db.session import init_db

        await init_db()
        _schema_ready = True


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
