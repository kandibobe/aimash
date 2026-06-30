"""Модели БД (SQLAlchemy 2.0). Миграции — Alembic (migrations/).

Таблицы:
- whitelist        — разрешённые chat_id (Telegram allow-list)
- user_settings    — расписание отчётов, пороги алертов, переопределение модели
- proposals        — очередь черновиков изменений (diff «было→станет», статус, customer_id)
- audit_log        — все операции: кто/когда/что/результат, по confirmation_id (БЕЗ секретов)
- oauth_tokens     — refresh-токены, ШИФРОВАННЫЕ at-rest (core.secrets.encrypt)
- error_events     — перехваченные исключения для триажа (§15): request_id/где/тип/текст РЕДАКТ.

⚠️ Секреты (refresh-токены) хранятся ТОЛЬКО зашифрованными (oauth_tokens.refresh_token_enc).
В audit_log/proposals секретов нет.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, String, Text, func
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
    language: Mapped[str | None] = mapped_column(String(8))  # язык интерфейса RU/EN (§4); NULL → RU
    # §8/мультиаккаунт: активный аккаунт чата (читаем/минтуем черновики на нём). NULL → Draft
    # (bot.account_ctx.get_active_account). Перепроверяется ensure_read_allowed + per-user доступом.
    selected_customer_id: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Proposal(Base):
    __tablename__ = "proposals"
    # Композитный индекс (status, created_at) — горячий скан очистки просроченных черновиков
    # (scheduler.cleanup_stale_proposals: WHERE status='pending', возраст по created_at). Создаётся
    # миграцией 0003; объявлен и здесь, чтобы create_all (dev/SQLite) и Alembic autogenerate
    # (env.py compare_type=True) НЕ дрейфовали — иначе autogenerate предложил бы DROP этого индекса.
    __table_args__ = (Index("ix_proposals_status_created_at", "status", "created_at"),)

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
    # fail-closed: True ставит ТОЛЬКО доверенный вход (Telegram-команда человека). Любой
    # автоматический создатель (scheduler/anomaly), забывший флаг, получит False → бюджет/ставка
    # будут заблокированы гейтом (golden rule #3). Дефолт True был бы fail-open.
    user_initiated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )  # pending|confirmed|executing|applied|failed|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    confirmation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # «Кто» (§12): user_id нажавшего ✅/❌. chat_id в группе — это чат, не человек; actor_user_id
    # точечно атрибутирует решение. Nullable: системные строки (applied/failed/scheduler-reject) —
    # NULL (актор берётся из связанной по confirmation_id строки confirmed/rejected).
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_username: Mapped[str | None] = mapped_column(String(64))
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
    # §8/Фаза 3: MCC (login_customer_id) ЭТОГО аккаунта — аккаунты живут под РАЗНЫМИ менеджерами,
    # поэтому login хранится per-account (ads.client.build_client подставит при сборке клиента).
    login_customer_id: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ErrorEvent(Base):
    """Перехваченное исключение для триажа (§15): глобальный on_error и scheduler-джобы пишут сюда
    через core.errors.capture_exception. message/traceback — УЖЕ редактированы (golden rule #5),
    секретов нет. request_id сшивает строку с логами того же апдейта; виден в /diag."""

    __tablename__ = "error_events"

    __table_args__ = (Index("ix_error_events_customer_created", "customer_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)  # NULL — ошибка вне Telegram (джоба)
    # §8: какой аккаунт обрабатывался при ошибке (для /diag-фильтра по аккаунту на масштабе ~10).
    customer_id: Mapped[str | None] = mapped_column(String(20))
    where: Mapped[str] = mapped_column(String(160), nullable=False)  # точка перехвата (handler/job)
    exc_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)  # str(e) — РЕДАКТИРОВАНО
    traceback: Mapped[str | None] = mapped_column(Text)  # РЕДАКТИРОВАНО + усечено
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccountAccess(Base):
    """Пер-пользовательский доступ к аккаунтам (§12/мультиоператор): chat_id → разрешённые
    customer_id. Draft доступен всем whitelisted без строки (см. ensure_account_allowed_for_user);
    прочие аккаунты — только при явном гранте здесь (fail-closed). Композится с глобальным
    ensure_read_allowed: чтение требует И глобального read-замка, И этого пер-юзер гранта.

    Граница безопасности → отдельная таблица (не JSON на UserSettings): запросная, аудируемая."""

    __tablename__ = "account_access"
    __table_args__ = (Index("ux_account_access", "chat_id", "customer_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CampaignTemplate(Base):
    """Именованный пресет настроек кампании (§2B): пользователь сохраняет валидированные params
    create_search_campaign под именем и переиспользует при создании. params НЕ содержат секретов
    (только параметры кампании/тексты/суммы). Уникальность (chat_id, name) — upsert по имени в
    рамках чата. На SQLite (dev) таблицу создаёт create_all; на Postgres (prod) — Alembic-миграция
    (heal_sqlite_schema таблицы НЕ создаёт). Применение — только через confirm-гейт (шаблон лишь
    ПРЕД-заполняет params, гейт не обходит)."""

    __tablename__ = "campaign_templates"
    __table_args__ = (Index("ux_campaign_templates_chat_name", "chat_id", "name", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    params: Mapped[dict] = mapped_column(
        JSON, nullable=False
    )  # валидированные create_search_campaign
    source_campaign: Mapped[str | None] = mapped_column(String(120))  # имя образца (если from X)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
