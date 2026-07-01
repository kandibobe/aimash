"""Модели БД (SQLAlchemy 2.0). Миграции — Alembic (migrations/).

Таблицы:
- whitelist        — разрешённые chat_id (Telegram allow-list)
- user_settings    — расписание отчётов, пороги алертов, переопределение модели
- proposals        — очередь черновиков изменений (diff «было→станет», статус, customer_id)
- audit_log        — все операции: кто/когда/что/результат, по confirmation_id (БЕЗ секретов)
- oauth_tokens     — refresh-токены, ШИФРОВАННЫЕ at-rest (core.secrets.encrypt)
- error_events     — перехваченные исключения для триажа (§15): request_id/где/тип/текст РЕДАКТ.
- account_access   — пер-пользовательский доступ к аккаунтам (§12)
- campaign_templates — именованные пресеты настроек кампании (§2B)
- campaign_drafts  — накопленный черновик визарда «Создание кампании» (§19), survives рестарт
- client_profiles  — база знаний о клиенте (§20): профиль на customer_id (бренд/услуги/контакты)
- client_contacts / client_services / client_site_pages — детали профиля (§20.7)
- crawl_jobs       — журнал задач краулинга сайта клиента (§20.4): статус/страницы/ошибка
- client_profile_history — версии профиля «до» для отката/аудита (§20.5), переживают clear

⚠️ Секреты (refresh-токены) хранятся ТОЛЬКО зашифрованными (oauth_tokens.refresh_token_enc).
В audit_log/proposals секретов нет. PII клиента (§20) — не секрет проекта, но в логи сырьём не
пишем (golden rule #5; редакция ошибок через core.logging.redact_text).
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


class CampaignDraft(Base):
    """§19: накопленный черновик 8-этапного визарда «Создание кампании». Хранится В БД (не в
    in-memory FSM), потому что Этап-2 (верификация ключей в Google Sheets) уводит менеджера из
    Telegram редактировать таблицу и он возвращается позже — возможно, после рестарта бота
    (aiogram MemoryStorage потерял бы прогресс). wizard_state (JSON) — ЕДИНЫЙ источник накопленных
    данных всех этапов (settings/keywords/ad/images/assets/url_options); current_step — курсор
    стадии (0..7). Это НЕ proposal: в Google Ads ничего не меняется, пока финальное «Создать
    черновик» не выпустит ОДИН proposal create_search_campaign (confirm-гейт). Секретов здесь нет
    (только параметры/тексты кампании; бинарь изображений — во временном медиа-хранилище по media_id,
    а не тут). Один АКТИВНЫЙ черновик на чат (status='active'); завершённые/брошенные чистит TTL.

    На SQLite (dev) таблицу создаёт create_all; на Postgres (prod) — Alembic-миграция
    (heal_sqlite_schema таблицы НЕ создаёт). Индексы объявлены и здесь, чтобы create_all и Alembic
    autogenerate (compare_type=True) не дрейфовали."""

    __tablename__ = "campaign_drafts"
    __table_args__ = (
        Index("ix_campaign_drafts_chat_status", "chat_id", "status"),
        Index("ix_campaign_drafts_status_updated", "status", "updated_at"),  # горячий скан TTL
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    # Аккаунт МУТАЦИИ — всегда Draft 7753643025 (замок ads.client.ensure_allowed). preview_customer_id
    # — выбранный на Этапе-0 дочерний MCC, ТОЛЬКО для чтения (медианы/ассеты), мутаций на нём нет.
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    preview_customer_id: Mapped[str | None] = mapped_column(String(20))
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0..7
    wizard_state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )  # active|done|abandoned
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClientProfile(Base):
    """§20: база знаний о клиенте, привязана к рекламному аккаунту (customer_id). Заполняется
    менеджером свободным текстом (LLM-разбор) и/или краулингом сайта; используется как КОНТЕКСТ для
    генерации RSA/ассетов/ключей (§10, §19) — сам аккаунт НЕ меняет. «Один аккаунт — один профиль»
    (§20.2): customer_id UNIQUE. Изменяющие действия над профилем (save/update/clear) проходят
    confirm-гейт как memory-операции (clients.execute), НЕ через ads.mutations — замок Google Ads к
    ним неприменим (это локальная БД). Секретов нет; PII (телефоны/e-mail) в логи сырьём не пишем.

    На SQLite (dev) таблицы создаёт create_all; на Postgres (prod) — Alembic (0013). Детали профиля
    (контакты/услуги/страницы) — в отдельных таблицах, слабо связаны по profile_id (без FK-констрейнта,
    как AccountAccess/audit — связь по id/confirmation_id, проще миграции/heal)."""

    __tablename__ = "client_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    brand: Mapped[str | None] = mapped_column(String(255))  # имя/бренд(ы) клиента
    business_desc: Mapped[str | None] = mapped_column(Text)  # ниша, УТП, регион, преимущества
    geo: Mapped[str | None] = mapped_column(String(255))  # страны/города работы клиента
    language: Mapped[str | None] = mapped_column(String(64))  # язык(и) аудитории
    website: Mapped[str | None] = mapped_column(String(2048))  # основной URL (для краулинга)
    socials: Mapped[dict | None] = mapped_column(JSON)  # {"instagram": "...", "facebook": "..."}
    notes: Mapped[str | None] = mapped_column(Text)  # заметки менеджера + незамапленный текст
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClientContact(Base):
    """§20.7: контакт клиента (телефон/e-mail/адрес/соцсеть/мессенджер), привязан к профилю."""

    __tablename__ = "client_contacts"
    __table_args__ = (Index("ix_client_contacts_profile", "profile_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(Integer, nullable=False)  # client_profiles.id
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # phone|email|address|social|msgr
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClientService(Base):
    """§20.7: услуга/товар клиента (для structured snippets, callouts, релевантности заголовков)."""

    __tablename__ = "client_services"
    __table_args__ = (Index("ix_client_services_profile", "profile_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(Integer, nullable=False)  # client_profiles.id
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[str | None] = mapped_column(String(128))  # отображаемый текст (не micros)
    category: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClientSitePage(Base):
    """§20.7: страница сайта клиента после краулинга (карта страниц → будущие sitelinks)."""

    __tablename__ = "client_site_pages"
    __table_args__ = (Index("ix_client_site_pages_profile", "profile_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(Integer, nullable=False)  # client_profiles.id
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    page_type: Mapped[str | None] = mapped_column(String(32))  # home|services|about|contacts|...
    key_links: Mapped[dict | None] = mapped_column(JSON)  # важные внутренние ссылки страницы
    content_hash: Mapped[str | None] = mapped_column(
        String(32)
    )  # §20.5: сигнатура для diff перекраула
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrawlJob(Base):
    """§20.4: журнал задачи краулинга (фоновая). Статус running→done/failed; зависшие running
    (in-process задача умерла с процессом на рестарте) реконсилятся в failed (scheduler)."""

    __tablename__ = "crawl_jobs"
    __table_args__ = (Index("ix_crawl_jobs_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), default="full", nullable=False
    )  # full|incremental
    status: Mapped[str] = mapped_column(
        String(16), default="running", nullable=False
    )  # running|done|failed
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)  # РЕДАКТИРОВАНО (redact_text), без секретов/PII
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClientProfileHistory(Base):
    """§20.5/20.7: версия профиля «до» изменения (для отката и аудита). Ключ — customer_id (не FK),
    чтобы история ПЕРЕЖИВАЛА clear профиля. snapshot — полный профиль до операции (JSON)."""

    __tablename__ = "client_profile_history"
    __table_args__ = (Index("ix_client_profile_history_customer", "customer_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot: Mapped[dict | None] = mapped_column(JSON)  # профиль «до» (для «было→станет»/отката)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)  # save|update|clear
    confirmation_id: Mapped[str | None] = mapped_column(String(64), index=True)  # сшивка с audit
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
