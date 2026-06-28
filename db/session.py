"""Async-движок и фабрика сессий SQLAlchemy. Для dev — SQLite (aiosqlite), позже Postgres.

DATABASE_URL берётся из .env (dev: sqlite+aiosqlite:///aimash.db). init_db() создаёт таблицы
по db.models (для SQLite; на Postgres — Alembic-миграции).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import settings
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

engine = create_async_engine(_db_url, **_engine_kwargs)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def dispose_engine() -> None:
    """Закрыть пул соединений БД (graceful shutdown). Вызывать из bot.main.main() в finally —
    иначе на остановке остаются открытые соединения asyncpg (P2: lifecycle)."""
    await engine.dispose()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
