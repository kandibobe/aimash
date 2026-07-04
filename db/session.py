"""Async-движок и фабрика сессий SQLAlchemy. Для dev — SQLite (aiosqlite), позже Postgres.

DATABASE_URL берётся из .env (dev: sqlite+aiosqlite:///aimash.db). init_db() создаёт таблицы
по db.models (для SQLite; на Postgres — Alembic-миграции).
"""

from __future__ import annotations

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import settings
from core.logging import log
from db.models import Base

# SQLite (dev/test): NullPool — aiosqlite держит соединение в фоновом потоке, привязанном к
# своему event loop. Пул, переживающий loop (напр. per-test event loop у pytest-asyncio),
# даёт повторное использование соединения из мёртвого loop → зависание на завершении процесса.
# NullPool закрывает соединение сразу после использования → процесс выходит чисто.
# Для asyncpg (prod) оставляем дефолтный пул (переиспользование соединений).
_db_url = settings.database_url.get_secret_value()  # SecretStr → реальный DSN в точке использования
_engine_kwargs: dict = {"future": True}
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


async def dispose_engine() -> None:
    """Закрыть пул соединений БД (graceful shutdown). Вызывать из bot.main.main() в finally —
    иначе на остановке остаются открытые соединения asyncpg (P2: lifecycle)."""
    await engine.dispose()


# B2: гард одного polling-инстанса. Ключ — произвольное стабильное 64-битное число ("AIMASH" в hex):
# одинаков между рестартами, уникален для этого приложения (маловероятно совпасть с чужим advisory-
# lock в общей БД). Соединение, удерживающее lock, держим живым весь рантайм.
_SINGLE_INSTANCE_LOCK_KEY = 0x41494D415348  # "AIMASH"
_lock_conn = None  # type: ignore[var-annotated]


async def acquire_single_instance_lock() -> bool:
    """B2: не дать двум polling-инстансам работать одновременно (Telegram отдаёт 409 Conflict дублю —
    повторяющаяся боль, особенно перекрытие старого/нового контейнера при `compose up --build`).
    Берём session-level Postgres advisory lock НЕблокирующе (pg_try_advisory_lock): занят → False,
    вызывающий чисто выходит (не лезем polling'ом → 409 не возникает). Соединение держим открытым
    весь рантайм — lock снимается автоматически при его закрытии/падении процесса (session-level, не
    транзакционный, откат/commit его не трогают). На SQLite (dev/test) advisory-lock нет → no-op=True."""
    global _lock_conn
    if _db_url.startswith("sqlite"):
        return True
    conn = await engine.connect()
    try:
        got = (
            await conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _SINGLE_INSTANCE_LOCK_KEY}
            )
        ).scalar()
    except Exception:
        await conn.close()
        raise
    if not got:
        await conn.close()
        return False
    _lock_conn = conn  # держим соединение открытым → держим lock весь рантайм
    return True


async def release_single_instance_lock() -> None:
    """B2: отпустить advisory lock (закрыть удерживающее соединение). Вызывать в finally teardown.
    Идемпотентно; no-op если lock не брался (SQLite/второй инстанс)."""
    global _lock_conn
    if _lock_conn is not None:
        try:
            await _lock_conn.close()  # session-level lock снимется вместе с закрытием соединения
        except Exception:  # noqa: BLE001 — освобождение lock не должно ронять teardown
            pass
        _lock_conn = None


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


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all не альтерит существующие таблицы → аддитивный self-heal схемы (только dev SQLite;
        # на Postgres истина — Alembic). Закрывает дрейф «модель ⟂ БД» после новой колонки в модели.
        if _db_url.startswith("sqlite"):
            await conn.run_sync(heal_sqlite_schema)
