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
- circuit_state    — распределённый размыкатель (Google Ads / LLM): состояние + аренда пробы
- operational_decisions / ops_incidents — единая очередь решений и дедуплицированный alert lifecycle
- budget_plans / pacing_snapshots — медиапланы, forecast и контроль потолков без автоприменения
- managed_experiments / playbook_versions — безопасные эксперименты и версионированные rules
- role_assignments / approval_votes / external_identities — RBAC, four-eyes и trusted identity mapping
- revenue_events / channel_metric_snapshots — PII-free CRM feedback и cross-channel metrics
- notification_routes — маршруты доставки по ссылкам на секреты, не сами credentials

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
    text,
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
    # Волна 1.4 — ВТОРОЙ, независимый бит провенанса. Отличие от user_initiated принципиальное:
    # тот приходит АРГУМЕНТОМ save_proposal (в headless-контуре его напишет вызывающий — MCP-тул,
    # cron, self-improvement-форк), а этот аргументом не задаётся вовсе: store читает его из
    # core.provenance, поднять который может только доверенный вход. Денежные apply_* требуют ОБА.
    origin_human_turn: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # «Кто заказал» — Telegram user_id человека, отдавшего команду. Не дублирует chat_id: в группе
    # chat_id — это чат, а не человек, и §8.4 №3 («подтвердил автор») по нему не проверяется.
    # ⚠️ Отклонение от план-файла, где колонка названа author_chat_id: чат уже есть выше (chat_id),
    # второй колонкой с тем же смыслом провенанс не усилить.
    author_user_id: Mapped[int | None] = mapped_column(BigInteger)
    # Корреляция с логами хода, в котором черновик родился (core.context.request_id, 8 hex, НЕ секрет).
    run_id: Mapped[str | None] = mapped_column(String(16))
    # message_id опубликованной карточки «было→станет». Заполняет ТРАНСПОРТ подтверждения (Волна 2.6),
    # сегодня NULL. Любая будущая проверка «ответ именно на эту карточку» обязана считать NULL
    # отказом, а не «проверять нечего» — иначе гард самоотключится на старых строках (fail-closed).
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # Волна 1b — состояние ОБЕЩАННОГО .xlsx-вложения: NULL = не обещано, 'pending' = обещано и не
    # доставлено, 'sending' (застолблено курьером), 'sent', 'failed'. Обещание в `summary` и эта
    # колонка рождаются ОДНОЙ вставкой — разъехаться им не на чем. Курьер живёт в процессе
    # `scheduler` (единственный вне `bot/`, у кого есть Bot-токен), поэтому доставка асинхронна и
    # наблюдаема: вечный 'pending' — видимая величина, а не тишина.
    attachment_state: Mapped[str | None] = mapped_column(String(16))
    # Волна 5 — тир риска (`confirm/risk.py`): 'L1'|'L2'|'L3', NULL на строках до миграции.
    # Тир не ослабляет ни одну проверку §2.2 и не даёт права на мутацию. Он меняет полноту карточки,
    # срок жизни согласия и, при включённом four-eyes, число независимых человеческих актов.
    # Пишется ради аудита («что человек видел, когда соглашался») и двух дополнительных условий
    # authoritative CAS: TTL и four-eyes.
    risk_tier: Mapped[str | None] = mapped_column(String(2))
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
    # Текст страницы (после вычитания шаблона). Раньше выбрасывался сразу после LLM-вызова: чтобы
    # пересобрать досье (clients.dossier_*) с новой схемой, приходилось заново обходить весь сайт.
    # Ретеншн — scheduler.jobs.purge_stale_rows (site_page_text_retain_days).
    text: Mapped[str | None] = mapped_column(Text)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClientDossier(Base):
    """§20: ДОСЬЕ клиента — сведённый по всему сайту документ (map-reduce поверх client_site_pages).

    Почему отдельная таблица, а не поля в client_profiles: снапшот профиля пишется в
    client_profile_history, которая ПЕРЕЖИВАЕТ «🗑 Очистить профиль» (ключ — customer_id, не FK) —
    имена сотрудников (чужая PII) остались бы в БД после удаления. Досье удаляется вместе с профилем
    (clients.store.apply_clear), истории не имеет.

    Confirm-гейт (правила 1–2): досье пишется как status='draft' и переводится в 'current' ТОЛЬКО
    внутри атомарного claim подтверждения (clients.execute.execute_confirmed_memory) — либо сразу,
    если профиля ещё не было (auto-save, гейта нет и у самого профиля). Один 'current' на аккаунт.

    Два артефакта: markdown — файл владельцу (с контактами); llm_context — то, что уезжает в промпт
    генераторов RSA/ключей (БЕЗ PII, clients.dossier_render). data — сведённый Dossier (JSON) для
    пересборки рендеров без повторного обхода."""

    __tablename__ = "client_dossiers"
    __table_args__ = (
        Index("ix_client_dossiers_customer_status", "customer_id", "status"),
        Index("ix_client_dossiers_profile", "profile_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    profile_id: Mapped[int | None] = mapped_column(
        Integer
    )  # client_profiles.id (без FK, как и всё §20)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft"
    )  # draft|current
    markdown: Mapped[str | None] = mapped_column(Text)  # файл владельцу (контакты ЕСТЬ)
    llm_context: Mapped[str | None] = mapped_column(Text)  # контекст генераторам (PII НЕТ)
    data: Mapped[dict | None] = mapped_column(JSON)  # сведённый Dossier целиком
    confirmation_id: Mapped[str | None] = mapped_column(String(64), index=True)  # сшивка с audit
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class AdsQuotaOp(Base):
    """§3 / C2: строка РАСПРЕДЕЛЁННОГО счётчика дневной квоты Google Ads API — общий стор вместо
    per-process (SPEC §9.2 `ads_quota_ops`, §11.3; HERMES_SPEC §31.3).

    Зачем таблица, а не модульный deque: bot, scheduler и per-session MCP-процесс — РАЗНЫЕ процессы
    со слепыми друг к другу счётчиками. Особенно per-session MCP: процесс поднимается на сессию и
    умирает на закрытии stdio — in-process счётчик для него структурно бесполезен. Общий стор —
    единственное, что видит всю нагрузку контура.

    Одна строка = один вызов `quota.record()` (НЕ операция): батч из 50 mutate-операций = одна
    строка `op_count=50` (Google тарифицирует каждую операцию, нам достаточно суммы). Скользящее
    24ч-окно считается в SQL: `SUM(op_count) WHERE ts > now-24h`. Ретеншн — purge_stale_rows.

    Без PII/секретов: customer_id — идентификатор аккаунта, не секрет проекта. На SQLite (dev/test)
    таблицу создаёт create_all; на Postgres (prod) — Alembic (0031)."""

    __tablename__ = "ads_quota_ops"
    __table_args__ = (
        # Глобальный счёт окна + prune: WHERE ts > cutoff.
        Index("ix_ads_quota_ops_ts", "ts"),
        # Пер-аккаунтный счёт: WHERE account = ? AND ts > cutoff (composite покрывает оба условия).
        Index("ix_ads_quota_ops_account_ts", "account", "ts"),
    )

    # Integer (как у всех сиблингов): на SQLite это INTEGER PRIMARY KEY → rowid-автоинкремент
    # (BIGINT PRIMARY KEY им НЕ является — id остался бы NULL). Объём строк крошечный (≤ число
    # API-вызовов/сутки < 15k, ретеншн 2 дня), 2.1млрд-потолка Integer хватает с гигантским запасом.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Момент фиксации батча (wall-clock UTC). Пишется и сравнивается через db.session.db_dt():
    # SQLite кладёт naive UTC, Postgres — tz-aware; окно фильтруется в SQL диалект-корректно.
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # customer_id или NULL (глобально-только запись, когда аккаунт вызывающему неизвестен).
    account: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # 'read' | 'mutate'
    op_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class AgentRun(Base):
    """#10 Наблюдаемость (Волна 1 шаг 10): агрегированный учёт ОДНОГО прогона агента —
    cost/latency/итерации. Того, чего не даёт core.usage (срез по РОЛЯМ, сбрасываемый на рестарте) и
    core.quota (счётчик API-операций, не денег): здесь строка = один ассистентский ход, персистентно.
    Сшивка по run_id с core.provenance/core.context (одна корреляция на ход, не вторая нумерация —
    provenance.run_id == context.request_id).

    Заполняется НАШИМ путём (agent.loop.run_analysis_agent, NL-команды); траты Hermes-прогонов идут
    мимо нашего процесса (config.yaml:70-71) — их подшивает ридер OpenRouter Activity API строкой с
    origin='hermes'. Deliverable шага 10 «сколько стоит один прогон / одна группа в месяц» считается
    отсюда: SUM(cost_usd) GROUP BY customer_id за окно по started_at.

    Без секретов/PII (числа + id аккаунта, не секрет). Запись fail-OPEN: наблюдаемость НЕ роняет
    денежный путь (как core.usage.record). На SQLite (dev/test) таблицу создаёт create_all; на
    Postgres (prod) — Alembic (0032)."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_run_id", "run_id"),
        # Отчёт «сколько стоит группа за окно»: WHERE customer_id = ? AND started_at > cutoff.
        Index("ix_agent_runs_customer_started", "customer_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # = provenance.run_id[:16] (машинный ход схлопывается в '-' — как в Proposal.run_id).
    run_id: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(
        String(16), default="machine", nullable=False
    )  # human|machine|hermes|cron
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    customer_id: Mapped[str | None] = mapped_column(String(20))  # для отчёта по клиенту/группе
    operation: Mapped[str | None] = mapped_column(String(64))  # analysis|command|…
    model: Mapped[str | None] = mapped_column(String(64))  # разрезолвленная модель (если известна)
    iterations_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # usage.cost OpenRouter (кредиты = USD). Реальная стоимость, а не оценка по токенам.
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="running", nullable=False
    )  # running|ok|error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRunEvent(Base):
    """#10 Наблюдаемость: один ШАГ прогона (LLM-вызов / tool-call / ads-read / ads-mutate) — то, что
    раньше жило только в лог-строках core.resilience (latency каждого вызова) и терялось после ротации.
    Сшивка с AgentRun по значению run_id (без FK — конвенция проекта, как audit_log). seq — порядок
    шага в прогоне.

    args_redacted проходит core.logging.redact_text ДО записи (golden rule #5): аргументы инструмента
    могут нести секрето-подобное (developer_token, url с ключом). result_digest — КОРОТКАЯ сводка/хэш,
    НЕ сырой результат (иначе таблица утянула бы PII клиента). Без секретов. Запись fail-OPEN. На
    SQLite create_all; на Postgres — Alembic (0032)."""

    __tablename__ = "agent_run_events"
    __table_args__ = (Index("ix_agent_run_events_run_seq", "run_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(16), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # порядок шага в прогоне
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # llm|tool|ads_read|ads_mutate
    tool_name: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_returned: Mapped[int | None] = mapped_column(
        Integer
    )  # для ads_read (сколько строк вернул)
    args_redacted: Mapped[str | None] = mapped_column(Text)  # РЕДАКТИРОВАНО (redact_text)
    result_digest: Mapped[str | None] = mapped_column(String(255))  # короткая сводка, не сырьё
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Волна 3 (event sourcing): хэш-цепочка ВНУТРИ run_id — вырезанное звено обязано быть видно, а не
    # только запрещено. prev_digest = payload_digest предыдущего события прогона (у первого — genesis
    # от run_id, core.observe.genesis_digest). NULL у строк, записанных ДО миграции 0035: они не
    # «сломаны», они вне цепочки, и core.observe.verify_chain считает их отдельно (`unchained`).
    # NULLABLE намеренно: NOT NULL потребовал бы задним числом подписать то, чего никто не подписывал.
    prev_digest: Mapped[str | None] = mapped_column(String(64))
    payload_digest: Mapped[str | None] = mapped_column(String(64))


def event_immutability_ddl(dialect: str) -> list[str]:
    """DDL неизменяемости `agent_run_events` для диалекта: список идемпотентных операторов.

    Зачем в СУБД, а не в коде. «Неизменяемое событие», обеспеченное дисциплиной вызывающего, — это
    не неизменяемость, а обещание: любой скрипт, миграция или psql-сессия обходит его молча. Здесь
    же UPDATE запрещён физически, а DELETE — пока строка денежного пути моложе пола хранения.

    Два правила, РАЗНЫЕ по строгости, и это осознанно:
      • UPDATE — запрещён ВСЕГДА и для всех kind. Легальной причины переписать событие нет ни одной;
        разрешить «иногда» значит завести путь, по которому подмену не отличить от штатной правки.
      • DELETE — разрешён, КРОМЕ событий MONEY_KINDS моложе MONEY_RETENTION_DAYS. Ретеншн обязан
        работать (таблица растёт монотонно), но денежный след не должен уходить вместе с мусором.
        Аварийного «флага обхода» нет намеренно: единственный, кому он понадобился бы, — тот, от
        кого пол и защищает. Ошибочно записанное событие исправляется компенсирующим событием, а не
        правкой журнала — это и есть event sourcing.

    Один источник этих правил на оба диалекта: Alembic зовёт для Postgres (прод), `db.session.init_db`
    — для SQLite (dev/test), поэтому гард проверяем локально, а не «только на проде»."""
    from core.observe import MONEY_KINDS, MONEY_RETENTION_DAYS

    kinds = ", ".join(f"'{k}'" for k in sorted(MONEY_KINDS))
    if dialect == "postgresql":
        return [
            f"""
CREATE OR REPLACE FUNCTION agent_run_events_immutable() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION 'agent_run_events immutable: UPDATE forbidden (run_id=%, seq=%)',
      OLD.run_id, OLD.seq;
  END IF;
  IF OLD.kind IN ({kinds})
     AND OLD.created_at > now() - interval '{MONEY_RETENTION_DAYS} days' THEN
    RAISE EXCEPTION 'agent_run_events: money-path event within retention floor (run_id=%, seq=%)',
      OLD.run_id, OLD.seq;
  END IF;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql;
""".strip(),
            "DROP TRIGGER IF EXISTS trg_agent_run_events_immutable ON agent_run_events;",
            "CREATE TRIGGER trg_agent_run_events_immutable "
            "BEFORE UPDATE OR DELETE ON agent_run_events "
            "FOR EACH ROW EXECUTE FUNCTION agent_run_events_immutable();",
        ]
    if dialect == "sqlite":
        # SQLite не знает TG_OP — два отдельных триггера. created_at здесь наивный UTC-текст
        # ('YYYY-MM-DD HH:MM:SS', server_default=CURRENT_TIMESTAMP), datetime('now',…) даёт тот же
        # формат, поэтому лексикографическое сравнение корректно (см. db.session.db_dt).
        return [
            "CREATE TRIGGER IF NOT EXISTS trg_agent_run_events_no_update "
            "BEFORE UPDATE ON agent_run_events BEGIN "
            "SELECT RAISE(ABORT, 'agent_run_events immutable: UPDATE forbidden'); END;",
            "CREATE TRIGGER IF NOT EXISTS trg_agent_run_events_money_retention "
            "BEFORE DELETE ON agent_run_events "
            f"WHEN OLD.kind IN ({kinds}) "
            f"AND OLD.created_at > datetime('now', '-{MONEY_RETENTION_DAYS} days') BEGIN "
            "SELECT RAISE(ABORT, "
            "'agent_run_events: money-path event within retention floor'); END;",
        ]
    return []


class CircuitState(Base):
    """Волна 2: состояние РАСПРЕДЕЛЁННОГО размыкателя (`core.breaker`) — одна строка на цепь.

    Зачем в БД, а не в модуле: bot, scheduler и per-session MCP — разные процессы. Локальный
    размыкатель у каждого свой, и после сбоя Google все три (× число аккаунтов) синхронно идут
    пробовать заново. Джиттер ретраев (`core.resilience`) разносит попытки ВНУТРИ одного вызова, но
    не между процессами; thundering herd лечит аренда пробы: в half-open право на пробный запрос
    берёт РОВНО ОДИН — атомарным UPDATE по `probe_lease_until` (rowcount==1 ⇒ ты пробник).

    `name` — `ads:<customer_id>` или `llm:<model_slug>` (см. `core.breaker.circuit_name`). Не секрет:
    id аккаунта и слаг модели секретами проекта не являются.

    Строк — единицы десятков (по аккаунту/модели), они переиспользуются, ретеншн не нужен.
    На SQLite (dev/test) таблицу создаёт create_all; на Postgres (prod) — Alembic (0033)."""

    __tablename__ = "circuit_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Уникальность обязательна: на ней держится «одна строка на цепь» и атомарность аренды пробы.
    name: Mapped[str] = mapped_column(String(96), unique=True, index=True, nullable=False)
    state: Mapped[str] = mapped_column(
        String(12), default="closed", nullable=False
    )  # closed|open|half_open
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Момент размыкания (wall-clock UTC через db_dt) — от него отсчитывается окно остывания.
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # До этого момента проба арендована другим процессом. NULL/прошлое ⇒ аренда свободна.
    probe_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RollbackWatch(Base):
    """Волна 4: наблюдение за применённой мутацией — «не стало ли хуже» (§2, автооткат).

    Строка заводится ПОСЛЕ успешного `finalize()` в `ads/service.py`, и только если выполнены оба
    условия: операция входит в `confirm.reverse.ROLLBACKABLE_OPS` И `reverse_spec` вернул не-`None`.
    Второе важнее первого: наблюдать за тем, что откатить нельзя, — значит копить вердикты, по
    которым нечего сделать, и приучать себя их игнорировать.

    ⚠️ Строка НЕ даёт права на мутацию и не несёт провенанса. Она фиксирует НАМЕРЕНИЕ наблюдать;
    что именно случится по вердикту, решает `mode`, а не наличие записи:

      * `shadow` (дефолт) — вердикт пишется, наружу не уходит НИЧЕГО. Копится выборка, по которой
        потом оценивается точность детектора. Это единственный режим, включённый на старте.
      * `alert`  — воркер сигналит человеку текстом; обратный черновик рождается в ЕГО ходе,
        обычным путём через confirm-гейт. Правок гейта, провенанса и золотых правил — ноль.
      * `auto`   — исполнение компенсации кодом. Включается пер-аккаунт и только после того, как
        shadow-точность подтверждена; сегодня этого пути нет вовсе (Волна 6a).

    `confirmation_id` — UNIQUE: одно наблюдение на применённую мутацию. Без уникальности повторный
    проход `finalize` (ретрай доставки, дубль джобы) завёл бы второе наблюдение за тем же
    изменением, и вердикт «деградация» пришёл бы дважды — а в режиме `auto` это была бы ДВОЙНАЯ
    компенсация: бюджет уехал бы ниже исходного.

    Ретеншн — общий для журнала наблюдений (`rollback_watch_retain_days`); денежного следа здесь
    нет, он живёт в `audit_log` и `agent_run_events`."""

    __tablename__ = "rollback_watch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    confirmation_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    # Числовой id кампании из Google Ads. Имя не годится: его могли переименовать между применением
    # и проверкой, и наблюдение молча начало бы мерить другую кампанию (или ничего).
    campaign_id: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # До этого момента вердикт выносить рано: метрики за неполное окно не отличают «стало хуже» от
    # «ещё не набралось». Проверка раньше срока даёт `insufficient`, а не `degraded`.
    window_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Во сколько раз изменение ДОЛЖНО было изменить расход (из снимка «было → станет»). Без него
    # детектор ловил бы собственную причину: подняли бюджет на 20% — расход вырос на 20% —
    # «деградация». Пишется в момент применения, потому что позже снимка «было» уже нет.
    expected_ratio: Mapped[float | None] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(
        String(8), default="shadow", nullable=False
    )  # shadow|alert|auto
    state: Mapped[str] = mapped_column(
        String(20), default="watching", index=True, nullable=False
    )  # watching|verdict_ok|verdict_degraded|acted|expired|skipped
    # Вердикт целиком, как его вынес детектор: метрика, база сравнения, отклонение, причина отказа.
    # JSON, потому что состав полей будет меняться по мере калибровки, а схему ради этого не гонять.
    verdict_json: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # confirmation_id компенсирующего черновика — заполняется только в `auto` (Волна 6a).
    acted_confirmation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OperationalDecision(Base):
    """Unified operator queue item.

    This is advisory state only: a row may point at a suggested operation or a proposal, but never
    authorizes or executes a Google Ads mutation. Execution remains in ``confirm/`` + ``ads/``.
    """

    __tablename__ = "operational_decisions"
    __table_args__ = (
        Index("ix_operational_decisions_queue", "customer_id", "status", "severity", "created_at"),
        Index("ix_operational_decisions_fingerprint", "customer_id", "fingerprint", "last_seen_at"),
        Index(
            "ux_operational_decisions_active_fingerprint",
            "active_fingerprint",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(96))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Равен fingerprint только пока decision активен; в terminal становится NULL. UNIQUE даёт
    # DB-level dedup конкурентных детекторов, но разрешает хранить несколько завершённых циклов.
    active_fingerprint: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_operation: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True, nullable=False)
    assigned_to: Mapped[int | None] = mapped_column(BigInteger, index=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    proposal_confirmation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    decision_note: Mapped[str | None] = mapped_column(Text)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OpsIncident(Base):
    """Deduplicated alert lifecycle: events fold into one acknowledgeable incident."""

    __tablename__ = "ops_incidents"
    __table_args__ = (
        Index("ix_ops_incidents_queue", "customer_id", "status", "severity", "last_seen_at"),
        Index("ux_ops_incidents_fingerprint", "customer_id", "fingerprint", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    decision_uid: Mapped[str | None] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    assigned_to: Mapped[int | None] = mapped_column(BigInteger)
    acknowledged_by: Mapped[int | None] = mapped_column(BigInteger)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[int | None] = mapped_column(BigInteger)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalation_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BudgetPlan(Base):
    """Versioned media-plan line for account/campaign/portfolio pacing."""

    __tablename__ = "budget_plans"
    __table_args__ = (
        Index(
            "ix_budget_plans_scope_period",
            "customer_id",
            "scope_type",
            "scope_id",
            "period_start",
            "period_end",
        ),
        Index(
            "ux_budget_plans_scope_version",
            "customer_id",
            "scope_type",
            "scope_id",
            "period_start",
            "period_end",
            "version",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(96), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    period_start: Mapped[str] = mapped_column(String(10), nullable=False)
    period_end: Mapped[str] = mapped_column(String(10), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    planned_spend_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    monthly_ceiling_micros: Mapped[int | None] = mapped_column(BigInteger)
    daily_ceiling_micros: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PacingSnapshot(Base):
    """Deterministic spend-to-plan result; recommendations are advisory only."""

    __tablename__ = "pacing_snapshots"
    __table_args__ = (
        Index("ix_pacing_snapshots_plan_asof", "plan_uid", "as_of_date", unique=True),
        Index("ix_pacing_snapshots_customer_asof", "customer_id", "as_of_date", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    as_of_date: Mapped[str] = mapped_column(String(10), nullable=False)
    spend_to_date_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_to_date_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    projected_spend_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    variance_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    variance_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    recommended_daily_budget_micros: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ManagedExperiment(Base):
    """First-class experiment record; this table does not create or mutate Ads experiments."""

    __tablename__ = "managed_experiments"
    __table_args__ = (Index("ix_managed_experiments_queue", "customer_id", "status", "end_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    control: Mapped[dict] = mapped_column(JSON, nullable=False)
    treatment: Mapped[dict] = mapped_column(JSON, nullable=False)
    primary_metric: Mapped[str] = mapped_column(String(32), nullable=False)
    success_direction: Mapped[str] = mapped_column(String(8), nullable=False)
    success_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    minimum_sample: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    end_date: Mapped[str] = mapped_column(String(10), nullable=False)
    rollback_trigger: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON)
    verdict: Mapped[str | None] = mapped_column(String(16))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RoleAssignment(Base):
    """Viewer/operator/approver/admin assignment, optionally scoped to one Ads customer."""

    __tablename__ = "role_assignments"
    __table_args__ = (
        Index("ux_role_assignments_scope", "user_id", "customer_id", "role", unique=True),
        Index("ix_role_assignments_customer_role", "customer_id", "role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), default="*", nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    capabilities: Mapped[list | None] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalVote(Base):
    """Independent approval evidence for four-eyes policies; never substitutes ConfirmStore CAS."""

    __tablename__ = "approval_votes"
    __table_args__ = (
        Index("ux_approval_votes_actor", "confirmation_id", "approver_user_id", unique=True),
        Index("ix_approval_votes_confirmation", "confirmation_id", "decision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approver_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RevenueEvent(Base):
    """PII-free CRM stage/revenue event keyed by a one-way external-id digest."""

    __tablename__ = "revenue_events"
    __table_args__ = (
        Index("ux_revenue_events_source_id", "source", "external_id_hash", unique=True),
        Index("ix_revenue_events_customer_time", "customer_id", "occurred_at"),
        Index("ix_revenue_events_campaign_time", "customer_id", "campaign_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(16), default="google", nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    qualified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revenue_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChannelMetricSnapshot(Base):
    """Provider-neutral daily metrics for portfolio and cross-channel orchestration."""

    __tablename__ = "channel_metric_snapshots"
    __table_args__ = (
        Index(
            "ux_channel_metric_scope_date",
            "channel",
            "customer_id",
            "external_account_id",
            "campaign_id",
            "metric_date",
            unique=True,
        ),
        Index("ix_channel_metric_customer_date", "customer_id", "metric_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(96), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    metric_date: Mapped[str] = mapped_column(String(10), nullable=False)
    spend_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    impressions: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    conversions: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    revenue_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlaybookVersion(Base):
    """Versioned deterministic rule; actions are restricted to creating decisions/incidents."""

    __tablename__ = "playbook_versions"
    __table_args__ = (
        Index("ux_playbook_versions_name_version", "name", "version", unique=True),
        Index("ix_playbook_versions_enabled", "enabled", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playbook_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rule: Mapped[dict] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExternalIdentity(Base):
    """Mapping created only after a trusted gateway verifies an OIDC/SAML identity."""

    __tablename__ = "external_identities"
    __table_args__ = (
        Index(
            "ux_external_identities_subject", "provider", "issuer_hash", "subject_hash", unique=True
        ),
        Index("ix_external_identities_user", "user_id", "active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    issuer_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NotificationRoute(Base):
    """Persistent routing policy; destination_ref points to config/secrets, never a raw webhook."""

    __tablename__ = "notification_routes"
    __table_args__ = (
        Index(
            "ux_notification_routes_scope", "customer_id", "channel", "destination_ref", unique=True
        ),
        Index("ix_notification_routes_enabled", "customer_id", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    destination_ref: Mapped[str] = mapped_column(String(96), nullable=False)
    severities: Mapped[list] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
