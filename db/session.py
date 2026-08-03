"""Async-движок и фабрика сессий SQLAlchemy. Для dev — SQLite (aiosqlite), позже Postgres.

DATABASE_URL берётся из .env (dev: sqlite+aiosqlite:///aimash.db). init_db() создаёт таблицы
по db.models (для SQLite; на Postgres — Alembic-миграции).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import settings
from core.logging import log
from db.models import Base, event_immutability_ddl

# SQLite (dev/test): NullPool — aiosqlite держит соединение в фоновом потоке, привязанном к
# своему event loop. Пул, переживающий loop (напр. per-test event loop у pytest-asyncio),
# даёт повторное использование соединения из мёртвого loop → зависание на завершении процесса.
# NullPool закрывает соединение сразу после использования → процесс выходит чисто.
# Для asyncpg (prod) оставляем дефолтный пул (переиспользование соединений).
_db_url = settings.database_url.get_secret_value()  # SecretStr → реальный DSN в точке использования
_engine_kwargs: dict = {}
if _db_url.startswith("sqlite"):
    _engine_kwargs["poolclass"] = NullPool
else:
    # asyncpg (prod): pre_ping проверяет соединение перед выдачей из пула (после обрыва сети/
    # рестарта БД зависшее соединение пересоздаётся, а не отдаётся мёртвым); recycle пересоздаёт
    # соединения старше N сек (защита от server-side idle-timeout). Стабильность рантайма (P2).
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_recycle"] = 1800
    # Размер пула: дефолт SQLAlchemy (5) тесноват — бот И scheduler конкурентно ходят в БД
    # (confirm-колбэки + плановая очистка/аномалии). 10 постоянных + 5 overflow с запасом покрывают
    # пиковую нагрузку одного инстанса. connect timeout — не виснуть бесконечно на старте/обрыве.
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 5
    _engine_kwargs["connect_args"] = {"timeout": 10}

engine = create_async_engine(_db_url, **_engine_kwargs)
Session = async_sessionmaker(engine, expire_on_commit=False)


def db_dt(dt: datetime) -> datetime:
    """Привести момент времени к тому виду, в каком created_at ЛЕЖИТ в этой БД, — чтобы окно можно
    было отфильтровать в SQL, а не тянуть таблицу в Python.

    SQLite (dev/test) кладёт наивный UTC (server_default=CURRENT_TIMESTAMP), Postgres — tz-aware
    (timestamptz). Сравнение naive-колонки с tz-aware границей на SQLite молча даёт мусор, поэтому
    исторически весь фильтр делался в Python — ценой полного скана (audit_activity_since тянул ВЕСЬ
    audit_log на каждый недельный дайджест). Здесь граница окна приводится к диалекту — один вызов
    вместо скана."""
    if engine.dialect.name == "sqlite":
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


async def dispose_engine() -> None:
    """Закрыть пул соединений БД (graceful shutdown). Вызывать из bot.main.main() в finally —
    иначе на остановке остаются открытые соединения asyncpg (P2: lifecycle)."""
    await engine.dispose()


# B2: гард одного polling-инстанса. Ключ — произвольное стабильное 64-битное число ("AIMASH" в hex):
# одинаков между рестартами, уникален для этого приложения (маловероятно совпасть с чужим advisory-
# lock в общей БД). Соединение, удерживающее lock, держим живым весь рантайм.
#
# Ключ РАЗНЫЙ по ролям. Причина конкретная: бот и MCP-сервер живут в одном контейнере и смотрят в
# одну БД, поэтому общий ключ на двоих означает «кто стартовал вторым — не стартовал вовсе». Спека
# (`HERMES_SPEC.md` C1) предлагает перенести захват lock в общий `app/bootstrap.py`; с ОДНИМ ключом
# этот перенос уронит MCP на каждом старте, потому что бот ключ уже держит.
#
# Сегодня lock берёт ТОЛЬКО бот (`bot/main.py`), и MCP брать его НЕ ДОЛЖЕН, пока V14
# (`deploy/hermes/OPERATIONS.md` §12) не покажет, что Hermes держит один долгоживущий MCP-процесс:
# если процесс поднимается на сессию/вызов, single-instance lock убьёт вторую сессию. Ключ `mcp`
# заведён и покрыт тестом именно для того, чтобы перенос по C1 нельзя было сделать «одним ключом»
# молча. Роль `bot` СОХРАНЯЕТ исторический ключ: сменить его — значит на время одного редеплоя
# пустить два polling-инстанса и получить 409.
# Роль `scheduler` — НЕ «второй инстанс того же процесса», а «кто владеет джобами». Её берут ОБА
# кандидата: и `bot/main.py` (когда SCHEDULER_IN_BOT=true, историческое поведение), и standalone
# `python -m scheduler`. Кто взял — тот и планирует; проигравший джобы НЕ регистрирует. Иначе при
# ошибке конфигурации (флаг забыли снять, а контейнер планировщика подняли) каждая джоба идёт
# ДВАЖДЫ: два дайджеста, два алерта, две попытки reconcile одного и того же зависшего черновика.
# Флаг выражает НАМЕРЕНИЕ, lock — ЭНФОРСМЕНТ; на намерение полагаться нельзя, оно в env.
_ROLE_LOCK_KEYS: dict[str, int] = {
    "bot": 0x41494D415348,  # "AIMASH" — исторический, не менять
    "mcp": 0x41494D415349,
    "scheduler": 0x41494D41534A,
}
_lock_conns: dict[str, AsyncConnection] = {}


async def acquire_single_instance_lock(role: str = "bot") -> bool:
    """B2: не дать двум инстансам ОДНОЙ роли работать одновременно (Telegram отдаёт 409 Conflict
    дублю polling'а — повторяющаяся боль, особенно перекрытие старого/нового контейнера при
    `compose up --build`).
    Берём session-level Postgres advisory lock НЕблокирующе (pg_try_advisory_lock): занят → False,
    вызывающий чисто выходит (не лезем polling'ом → 409 не возникает). Соединение держим открытым
    весь рантайм — lock снимается автоматически при его закрытии/падении процесса (session-level, не
    транзакционный, откат/commit его не трогают). На SQLite (dev/test) advisory-lock нет → no-op=True.

    Args:
        role: чей это инстанс (`_ROLE_LOCK_KEYS`). Разные роли берут РАЗНЫЕ ключи и друг друга не
            блокируют; неизвестная роль — `KeyError`, а не тихий выбор чужого ключа."""
    key = _ROLE_LOCK_KEYS[role]
    if _db_url.startswith("sqlite"):
        return True
    conn = await engine.connect()
    try:
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar()
    except Exception:
        await conn.close()
        raise
    if not got:
        await conn.close()
        return False
    _lock_conns[role] = conn  # держим соединение открытым → держим lock весь рантайм
    return True


async def release_single_instance_lock(role: str = "bot") -> None:
    """B2: отпустить advisory lock (закрыть удерживающее соединение). Вызывать в finally teardown.
    Идемпотентно; no-op если lock этой ролью не брался (SQLite/второй инстанс)."""
    conn = _lock_conns.pop(role, None)
    if conn is not None:
        try:
            await conn.close()  # session-level lock снимется вместе с закрытием соединения
        except Exception:  # noqa: BLE001 — освобождение lock не должно ронять teardown
            pass


def heal_sqlite_schema(sync_conn) -> list[str]:
    """Dev (SQLite) self-heal схемы: `create_all` НЕ добавляет колонки в уже существующие таблицы,
    поэтому аддитивная миграция (напр. 0004 → audit_log.actor_user_id/actor_username) оставляла
    dev-БД рассинхронизированной с моделью. Симптомы: `no such column` на /journal (SELECT) и
    ТИХИЙ обрыв confirm-колбэка (INSERT audit-строки падал раньше cq.answer → «ничего не происходит»).

    Сверяем колонки модели с фактическими и `ALTER TABLE ADD COLUMN` каждую недостающую
    NULLABLE-колонку (аддитивно, без потери данных, идемпотентно). NOT NULL без дефолта на непустую
    таблицу SQLite добавить не даст — такую колонку НЕ трогаем, а ГРОМКО логируем (нужна ручная
    Alembic-миграция, чтобы дрейф не прошёл молча). Источник истины схемы на Postgres (prod) —
    Alembic; авто-хил трогает ТОЛЬКО SQLite (dev), см. вызов в init_db. Возвращает список действий."""
    insp = sa_inspect(sync_conn)
    existing = set(insp.get_table_names())
    actions: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue  # таблицы целиком создаёт create_all
        db_cols = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in db_cols:
                continue
            if not col.nullable:
                log.warning(
                    "schema-heal: %s.%s NOT NULL отсутствует — нужна Alembic-миграция (пропуск)",
                    table.name,
                    col.name,
                )
                actions.append(f"SKIP {table.name}.{col.name} (NOT NULL)")
                continue
            coltype = col.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'))
            log.warning("schema-heal: добавлена колонка %s.%s %s", table.name, col.name, coltype)
            actions.append(f"ADD {table.name}.{col.name} {coltype}")
    return actions


def _missing_tables(sync_conn) -> list[str]:
    """Таблицы моделей, которых нет в БД (Postgres: признак ненакатанных миграций)."""
    existing = set(sa_inspect(sync_conn).get_table_names())
    return [t.name for t in Base.metadata.sorted_tables if t.name not in existing]


async def init_db() -> None:
    """SQLite (dev/test): схему создаёт и лечит сам процесс. Postgres (prod): истина — Alembic.

    На Postgres `create_all` НЕЛЬЗЯ: он молча создаёт недостающие таблицы в обход миграций, версия
    в alembic_version при этом не двигается → дрейф «модель ⟂ миграции» невидим, а следующая
    миграция с `op.create_table` падает DuplicateTable уже на старте контейнера (крэш-луп прода:
    docker-entrypoint.sh гоняет `alembic upgrade head` до бота и не поднимает его на ошибке).
    Поэтому здесь только ПРОВЕРКА: схемы нет → падаем громко с указанием, что делать.
    """
    if _db_url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # create_all не альтерит существующие таблицы → аддитивный self-heal схемы.
            await conn.run_sync(heal_sqlite_schema)
            # Волна 3: неизменяемость agent_run_events обеспечивает СУБД, а не дисциплина
            # вызывающего. Ставим ЗДЕСЬ, а не через after_create-листенер: у уже существующей
            # dev-БД таблица не пересоздаётся, а гард нужен и ей. Операторы идемпотентны.
            for stmt in event_immutability_ddl("sqlite"):
                await conn.execute(text(stmt))
        return

    async with engine.begin() as conn:
        missing = await conn.run_sync(_missing_tables)
    if missing:
        raise RuntimeError(
            "схема БД не накатана — нет таблиц: "
            + ", ".join(sorted(missing))
            + ". Выполните `alembic upgrade head` (на проде это делает docker-entrypoint.sh)."
        )


async def assert_schema_at_head() -> None:
    """Postgres: схема БД не ОТСТАЁТ от миграций этого кода. Отстала — raise, обогнала — warning.

    Зачем отдельно от `init_db`. `init_db` ловит только отсутствие ТАБЛИЦ — то есть совсем не
    накатанную схему. Дрейф на одну миграцию (таблицы есть, новой колонки нет) он пропускает молча,
    и процесс работает по устаревшей схеме до первого `UndefinedColumn` в рантайме.

    Кому это нужно на практике: у бота гейт сильнее и стоит раньше — `docker-entrypoint.sh` гоняет
    `alembic upgrade head` ДО старта и не поднимает контейнер на ошибке миграции. Но гейт заведён
    по КОМАНДЕ контейнера (`python -m bot.main`), а MCP-сервер стартует `python -m mcp_server` —
    мимо него. Поднятый раньше бота (или на образе новее, чем накатанная схема), MCP сегодня
    молча работает по чужой схеме. Отсюда fail-fast здесь: не подняться честнее, чем отдавать
    агенту данные из БД, форму которой код понимает неправильно (правило 10).

    Почему НЕ голое `current == head`, а два разных исхода. Отставание БД («в коде есть миграция,
    которой в БД нет») — это гарантированный `UndefinedColumn` в рантайме, единственный честный
    ответ — не стартовать. Обгон БД («в БД ревизия, которой в этом коде нет») бывает ровно в одном
    сценарии: ОТКАТ на предыдущий образ после неудачного релиза. Миграции аддитивны, старый код с
    новой схемой обычно работает, а отказ стартовать превратил бы откат — штатный аварийный
    манёвр — в продолжение аварии. Поэтому здесь громкий warning, а не raise.

    Реализация читает только `alembic.ini` и каталог `migrations/` (`ScriptDirectory`), НЕ исполняя
    `env.py`: нам нужен список ревизий, а не подключение alembic к своей БД.

    Raises:
        RuntimeError: схема отстала от кода; таблицы `alembic_version` нет/не читается; либо
            `alembic.ini` недоступен (значит образ собран неправильно — гадать не будем).
    """
    if _db_url.startswith("sqlite"):
        return  # dev/test: схему держит create_all + heal_sqlite_schema, alembic_version не штампуется

    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    if not ini.is_file():
        raise RuntimeError(f"схема-гейт: не найден {ini} — проверить состав образа")
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = set(script.get_heads())

    try:
        async with engine.connect() as conn:
            current = set(
                (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars()
            )
    except Exception as e:  # noqa: BLE001 — правило 5: наружу тип, не str(e) (в DSN пароль)
        raise RuntimeError(
            f"схема-гейт: не прочитать alembic_version ({type(e).__name__}). "
            "Выполните `alembic upgrade head`."
        ) from None

    if current == heads:
        return

    known = {rev.revision for rev in script.walk_revisions()}  # все ревизии, известные ЭТОМУ коду
    if current - known:
        # В БД ревизия, которой в этом коде нет → откат на старый образ. Стартуем, но громко.
        log.warning(
            "схема БД обогнала код: alembic_version=%s, head этого кода=%s. "
            "Похоже на откат образа. Работаем, но проверьте, что ничего не сломано откатом.",
            sorted(current),
            sorted(heads),
        )
        return

    raise RuntimeError(
        f"схема БД отстала от кода: alembic_version={sorted(current) or '(пусто)'}, "
        f"head миграций={sorted(heads)}. Выполните `alembic upgrade head` "
        "(на проде это делает docker-entrypoint.sh при старте бота — MCP мимо него)."
    )
