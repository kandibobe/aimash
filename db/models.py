"""Модели БД (SQLAlchemy 2.0). Миграции — Alembic (migrations/).

Таблицы:
- whitelist        — разрешённые chat_id (Telegram allow-list)
- user_settings    — расписание отчётов, пороги алертов, переопределение модели
- proposals        — очередь черновиков изменений (diff «было→станет», статус, customer_id)
- audit_log        — все операции: кто/когда/что/результат, по confirmation_id (БЕЗ секретов)
- oauth_tokens     — refresh-токены, ШИФРОВАННЫЕ at-rest (core.secrets.encrypt)

⚠️ Секреты (refresh-токены) хранятся ТОЛЬКО зашифрованными (oauth_tokens.refresh_token_enc).
В audit_log/proposals секретов нет.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Whitelist(Base):
    __tablename__ = "whitelist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    report_schedule: Mapped[str | None] = mapped_column(String(128))  # cron-строка планового отчёта
    alert_thresholds: Mapped[dict | None] = mapped_column(JSON)  # пороги аномалий
    model_override: Mapped[str | None] = mapped_column(String(128))  # переопределение LLM (опц.)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    confirmation_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # только Aimash Draft (7753643025)
    summary: Mapped[str] = mapped_column(Text, nullable=False)  # «было → станет»
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_initiated: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )  # pending|confirmed|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    confirmation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # confirmed|rejected|applied|failed
    result: Mapped[dict | None] = mapped_column(JSON)  # результат операции (БЕЗ секретов)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    refresh_token_enc: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # core.secrets.encrypt(...)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
