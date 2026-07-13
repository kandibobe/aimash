"""Модели БД (SQLAlchemy 2.0). Миграции — Alembic (migrations/).

Таблицы:
- whitelist        — рантайм-allow-list Telegram chat_id (env ∪ БД; admin /adduser /removeuser)
- user_settings    — пороги алертов, язык, активный аккаунт, ui_prefs, per-user report_schedule (§14)
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
- recommendation   — advisor: показанная рекомендация (advisory, НЕ proposal); source/kind/priority
- recommendation_feedback — 👍/👎 оператора на рекомендацию (Слой B: сигнал для experience)
- recommendation_outcome — сшивка рекомендация→applied-мутация→delta метрик (Слой B: замер результата)
- bug_reports       — пользовательские баг-репорты (/reportbug, §6): текст РЕДАКТ., статус триажа
- account_health_snapshot — агрегаты health-score /audit на дату (субстрат трендов, N1.1; без PII)
- sheet_exports    — реестр созданных ботом Google-таблиц (отчёты/ключи): ссылка + исход шаринга

⚠️ Секреты (refresh-токены) хранятся ТОЛЬКО зашифрованными (oauth_tokens.refresh_token_enc).
В audit_log/proposals секретов нет. PII клиента (§20) — не секрет проекта, но в логи сырьём не
пишем (golden rule #5; редакция ошибок через core.logging.redact_text).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Whitelist(Base):
    """Рантайм-allow-list Telegram chat_id (§6/§12). ВОЗВРАЩЕНА (0017) — теперь реально читается
    рантаймом: WhitelistMiddleware пускает chat_id из env TELEGRAM_WHITELIST_CHAT_IDS ∪ этой таблицы
    (fail-closed: пустое объединение блокирует всех). Env остаётся бутстрапом первого админа; админ
    добавляет операторов без рестарта командой /adduser (core.access.add_whitelisted_user).

    В 0016 таблицу дропнули как мёртвую (тогда allow-list был только env). Отличие сейчас — она
    подключена к гейту. Граница безопасности → отдельная таблица (не JSON), запросная и аудируемая;
    added_by фиксирует, какой админ добавил (кто/когда, §12)."""

    __tablename__ = "whitelist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    added_by: Mapped[int | None] = mapped_column(
        BigInteger
    )  # chat_id админа, добавившего оператора
    note: Mapped[str | None] = mapped_column(String(255))  # опц. заметка (имя оператора и т.п.)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Admin(Base):
    """Рантайм-админы бота (0021, P4 — фиксы по живому тесту 2026-07-06). is_admin = env
    ADMIN_CHAT_IDS ∪ эта таблица (core.access.is_admin, зеркало Whitelist/0017): env — бутстрап
    первого админа (в рантайме неснимаем), таблица — /addadmin//removeadmin без рестарта VPS.
    Fail-closed: сбой БД ⇒ только env; пустые оба ⇒ админов нет. Админка даёт /grant//adduser/
    /addadmin и read-bypass пер-юзер грантов; мутационный замок Draft НЕ затрагивает."""

    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    added_by: Mapped[int | None] = mapped_column(BigInteger)  # chat_id админа, выдавшего админку
    note: Mapped[str | None] = mapped_column(String(255))  # опц. заметка (имя и т.п.)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    # §14 (P1-I): персональный crontab планового отчёта оператора. Читается
    # scheduler.service.register_user_report_schedules на старте → per-chat cron-джоба
    # run_scheduled_report(only_chat=...); глобальная джоба таких операторов пропускает (без дубля).
    # Пусто/NULL ⇒ оператор в глобальном расписании (env REPORT_SCHEDULE).
    report_schedule: Mapped[str | None] = mapped_column(String(128))
    alert_thresholds: Mapped[dict | None] = mapped_column(JSON)  # пороги аномалий
    model_override: Mapped[str | None] = mapped_column(String(128))  # переопределение LLM (опц.)
    language: Mapped[str | None] = mapped_column(String(8))  # язык интерфейса RU/EN (§4); NULL → RU
    # §8/мультиаккаунт: активный аккаунт чата (читаем/минтуем черновики на нём). NULL → Draft
    # (bot.account_ctx.get_active_account). Перепроверяется ensure_read_allowed + per-user доступом.
    selected_customer_id: Mapped[str | None] = mapped_column(String(20))
    # §UX-память: JSON-настройки интерфейса per-chat ({"last_report_period": "7"}). Отдельно от
    # alert_thresholds (тот — пороги scheduler-аномалий, читается целиком jobs._thresholds_by_chat).
    ui_prefs: Mapped[dict | None] = mapped_column(JSON)
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
    # Композитный индекс (chat_id, status) — история чата (db.history.list_recent_applied:
    # WHERE chat_id=? AND status='applied' ORDER BY id DESC). По (status, created_at) Postgres
    # сканировал ВСЕ applied-строки всех чатов; таблица растёт с каждой мутацией. Миграция 0027.
    __table_args__ = (
        Index("ix_proposals_status_created_at", "status", "created_at"),
        Index("ix_proposals_chat_status", "chat_id", "status"),
    )

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
    )  # pending|confirmed|executing|applied|failed|rejected|needs_review
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    # 1.7: агрегат «частые аккаунты» (bot.main._frequent_accounts) сканирует по chat_id на каждый
    # рендер пикера (с TTL-кэшем). Индекс зеркалится в миграции 0020 (конвенция против дрейфа).
    __table_args__ = (Index("ix_audit_log_chat_created", "chat_id", "created_at"),)

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
    )  # confirmed|rejected|applied|failed|needs_review
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


class Recommendation(Base):
    """advisor: одна ПОКАЗАННАЯ рекомендация (advisory, golden rule #1/#3). Это НЕ proposal — строка
    здесь ничего не исполняет и не создаёт мутацию; исполнение любого совета идёт ОТДЕЛЬНОЙ командой
    через confirm-гейт. suggested_operation — advisory-МЕТКА (для связывания исхода в Слое B), НЕ путь
    исполнения. evidence — метрики-триггер (база для замера delta); body — показанный текст (аудит).
    Секретов нет (только метрики/имена кампаний). rec_uid уникален (влезает в 64-байт callback_data).

    На SQLite (dev) таблицу создаёт create_all; на Postgres (prod) — Alembic (0018). Индексы
    объявлены и здесь — против дрейфа create_all/autogenerate (как Proposal/CampaignDraft)."""

    __tablename__ = "recommendation"
    __table_args__ = (
        Index("ix_recommendation_chat_customer_created", "chat_id", "customer_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rec_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    # /advise: optimize|keywords|rsa|structure; /audit: СЕМЬЯ чека (audit.thresholds.FAMILY_WEIGHT,
    # самая длинная — 'conversion_tracking', 19). Ширина 16 роняла /audit на Postgres (тихо на SQLite);
    # гард — tests/test_db_schema.test_recommendation_columns_fit_audit_taxonomy.
    topic: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # /audit: audit.engine check_id
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # warning|info
    target_campaign: Mapped[str | None] = mapped_column(String(255))
    # advisory-метка (для outcome-связывания), НЕ путь исполнения — мутация только через confirm-гейт.
    suggested_operation: Mapped[str | None] = mapped_column(String(64))
    priority: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # считает КОД
    evidence: Mapped[dict | None] = mapped_column(JSON)  # метрики-триггер (база для delta)
    body: Mapped[str | None] = mapped_column(Text)  # показанный текст (аудит), без секретов
    source: Mapped[str] = mapped_column(
        String(16), default="advise", nullable=False
    )  # advise|scheduler
    status: Mapped[str] = mapped_column(
        String(16), default="shown", nullable=False
    )  # shown|dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecommendationFeedback(Base):
    """Слой B: обратная связь оператора (👍/👎) на рекомендацию. Один голос-тогл на оператора —
    уникальность (rec_uid, chat_id); связь с recommendation по значению rec_uid (без FK — конвенция
    проекта, как audit_log/AccountAccess). actor_* — кто нажал (§12-стиль). Кнопки НЕ мутируют
    Google Ads и НЕ создают proposal — только запись сюда (инвариант test_advisor)."""

    __tablename__ = "recommendation_feedback"
    __table_args__ = (Index("ux_recommendation_feedback", "rec_uid", "chat_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rec_uid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_username: Mapped[str | None] = mapped_column(String(64))
    rating: Mapped[str] = mapped_column(String(8), nullable=False)  # up|down
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecommendationOutcome(Base):
    """Слой B: сшивка рекомендация → ПРИМЕНЁННАЯ мутация → delta метрик (замер результата). Создаётся
    хуком link_applied_mutation ПОСЛЕ успешного execute_confirmed, когда applied-мутация совпала с
    открытой рекомендацией (тот же customer_id+suggested_operation+target_campaign). measure_after —
    когда джоба замера (read-only) посчитает followup и delta (verdict КОДОМ). measured_at=NULL —
    ждёт замера. Только ЛОКАЛЬНАЯ БД: ни хук, ни джоба ничего не мутируют в Google Ads (rule #3).
    Связи по значению (rec_uid/confirmation_id, без FK — как audit_log)."""

    __tablename__ = "recommendation_outcome"
    __table_args__ = (Index("ix_recommendation_outcome_pending", "measured_at", "measure_after"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rec_uid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    confirmation_id: Mapped[str | None] = mapped_column(String(64), index=True)  # applied-мутация
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    target_campaign: Mapped[str | None] = mapped_column(String(255))
    apply_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    baseline: Mapped[dict | None] = mapped_column(JSON)  # метрики на момент рекомендации/применения
    followup: Mapped[dict | None] = mapped_column(JSON)  # метрики после окна (замер джобой)
    delta: Mapped[dict | None] = mapped_column(JSON)  # разница (считает КОД)
    measure_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    measured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # NULL=ждёт замера
    verdict: Mapped[str | None] = mapped_column(String(16))  # improved|worse|neutral (КОД)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccountHealthSnapshot(Base):
    """N1.1: агрегированный снапшот health-score аккаунта на дату — субстрат трендов (WoW-дельта в
    /audit, дайджесты позже). ТОЛЬКО агрегаты (score/grade/деньги/штрафы семей) — БЕЗ PII и имён
    кампаний (per-finding детали живут в recommendation). snapshot_date — ISO-дата в ТАЙМЗОНЕ
    АККАУНТА (границы дня аккаунта, не хоста). Идемпотентно per (customer_id, snapshot_date,
    period_days): повторный /audit в тот же день С ТЕМ ЖЕ окном перезаписывает агрегаты, а другое
    окно (/audit 7 vs 30) НЕ клоббрит базу тренда (ревью 2026-07-08). Дельты сравнимы ТОЛЬКО в
    пределах одной score_model_version И одного period_days (N1.0a) — сравнение делает КОД
    (audit/snapshot.py), разные версии → «н/д». Запись fail-OPEN: сбой не роняет аудит.

    На SQLite (dev) таблицу создаёт create_all; на Postgres (prod) — Alembic (0022). Индексы
    объявлены и здесь — против дрейфа create_all/autogenerate (как Recommendation)."""

    __tablename__ = "account_health_snapshot"
    __table_args__ = (
        Index(
            "ux_account_health_snapshot_cid_date_period",
            "customer_id",
            "snapshot_date",
            "period_days",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    # ISO YYYY-MM-DD (TZ аккаунта): строка сортируется хронологически на SQLite и Postgres одинаково.
    snapshot_date: Mapped[str] = mapped_column(String(10), nullable=False)
    score: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # 0..100, считает КОД (audit/engine)
    grade: Mapped[str] = mapped_column(String(4), nullable=False)
    total_spend: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    at_risk: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)  # окно аудита
    family_penalty: Mapped[dict | None] = mapped_column(JSON)  # family → penalty (агрегат, без PII)
    score_model_version: Mapped[str] = mapped_column(String(16), nullable=False)  # N1.0a
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BugReport(Base):
    """Пользовательский баг-репорт (команда /reportbug, §6 «сообщить об ошибке»). Оператор описывает
    проблему текстом → бот сохраняет сюда, форвардит админам и включает в еженедельный дайджест.
    text РЕДАКТИРОВАН через core.logging.redact_text ПЕРЕД записью (golden rule #5 — оператор мог
    вставить в описание что-то секрето-подобное). context_request_id сшивает репорт с последним
    инцидентом /diag (error_events) того же чата — для триажа. Ничего не мутирует в Google Ads."""

    __tablename__ = "bug_reports"
    __table_args__ = (Index("ix_bug_reports_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))  # @username автора (если есть)
    text: Mapped[str] = mapped_column(Text, nullable=False)  # описание — РЕДАКТИРОВАНО
    context_request_id: Mapped[str | None] = mapped_column(
        String(16)
    )  # сшивка с error_events (/diag)
    status: Mapped[str] = mapped_column(
        String(16), default="new", nullable=False
    )  # new|triaged|closed
    triaged_by: Mapped[int | None] = mapped_column(BigInteger)  # chat_id админа, сменившего статус
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SheetExport(Base):
    """Реестр созданных ботом Google-таблиц (отчёты /sheets и таблицы ключей визарда §19.4.2).

    Зачем: раньше ссылка жила только в сообщении Telegram и (для ключей) в CampaignDraft.wizard_state
    — черновик умирает по TTL 72ч, и ссылку было не найти. Здесь она переживает и рестарт, и визард
    (команда /mysheets отдаёт последние таблицы ЧАТА).

    Владелец файлов — Google-аккаунт OAuth-токена бота (GOOGLE_ADS_REFRESH_TOKEN): таблицы лежат на
    ЕГО Диске, scope drive.file. share — исход anyone-with-link на момент создания (reports.sheets:
    роль | 'off' | 'failed'), нужен чтобы /mysheets честно помечал приватные таблицы.

    Секретов нет: url — публичная ссылка, уже отправленная в чат. Ширины полей с запасом (урок
    0023_recommendation_topic_width: узкий VARCHAR ронял вставку на Postgres)."""

    __tablename__ = "sheet_exports"
    __table_args__ = (Index("ix_sheet_exports_chat_id_id", "chat_id", "id"),)  # выборка последних

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    customer_id: Mapped[str | None] = mapped_column(
        String(32)
    )  # аккаунт Google Ads (если известен)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # keywords|report
    spreadsheet_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    share: Mapped[str] = mapped_column(String(16), nullable=False)  # роль|off|failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuctionInsightRow(Base):
    """Ф5б: строка импортированного отчёта «Статистика аукционов» (CSV из интерфейса Google Ads).

    ЕДИНСТВЕННЫЙ легальный источник ИМЁН конкурентов: через API Google их не отдаёт (ресурса
    `auction_insight` в GAQL нет). Отсюда — таблица, а не фетчер: данные приносит человек файлом.

    Хранится ровно то, что было в файле (доли 0..1, None = «--» в отчёте, а НЕ ноль). Идемпотентно
    per (customer_id, snapshot_date, domain): повторный импорт за ту же дату перезаписывает срез
    (домены между выгрузками появляются и исчезают ⇒ срез перезаписывается целиком, не мержится).
    Сравнение во времени — между РАЗНЫМИ snapshot_date; period_label (диапазон дат из преамбулы
    файла) показывается рядом, чтобы клиент сам увидел, если сравнивает разные окна.

    Домен конкурента — публичный факт из отчёта Google, не PII и не секрет. На SQLite (dev) таблицу
    создаёт create_all; на Postgres (prod) — Alembic (0026)."""

    __tablename__ = "auction_insight_row"
    __table_args__ = (
        Index(
            "ux_auction_insight_cid_date_domain",
            "customer_id",
            "snapshot_date",
            "domain",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO, TZ аккаунта
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    is_you: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Доли 0..1. NULL = «--» в файле («не показывалось»), это НЕ 0.0 (GR8: нет данных ≠ ноль).
    impression_share: Mapped[float | None] = mapped_column(Float)
    overlap_rate: Mapped[float | None] = mapped_column(Float)
    position_above_rate: Mapped[float | None] = mapped_column(Float)
    top_of_page_rate: Mapped[float | None] = mapped_column(Float)
    abs_top_of_page_rate: Mapped[float | None] = mapped_column(Float)
    outranking_share: Mapped[float | None] = mapped_column(Float)
    period_label: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
