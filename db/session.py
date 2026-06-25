"""Async-движок и фабрика сессий SQLAlchemy. Для dev — SQLite (aiosqlite), позже Postgres.

DATABASE_URL берётся из .env (dev: sqlite+aiosqlite:///aimash.db). init_db() создаёт таблицы
по db.models (для SQLite; на Postgres — Alembic-миграции).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.config import settings
from db.models import Base

engine = create_async_engine(settings.database_url, future=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
