"""Конфигурация из окружения (.env). Секреты только отсюда, никогда из кода.

Секреты обёрнуты в pydantic.SecretStr — маскируются в логах/трейсбеках/repr;
реальное значение доступно ТОЛЬКО через .get_secret_value() в точке использования.
SecretStr — это защита от утечки в логи, НЕ шифрование (за шифрование at-rest — core.secrets).
"""

from __future__ import annotations

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_customer_id(customer_id: str) -> str:
    """Customer ID без разделителей: '775-364-3025' -> '7753643025'. Только цифры."""
    return "".join(ch for ch in str(customer_id) if ch.isdigit())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Окружение
    env: str = "dev"  # dev => только TEST MCC

    # Модель через OpenRouter (сменяемая)
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # имена llm_* (не model_*) — иначе шадоуят метод BaseModel.model_copy()
    # Разделение «что за что» (дефолты; ручной выбор оператора через /model бьёт их — см.
    # agent.router.effective_model: override > роль-дефолт):
    #   parsing — разбор команд (function calling, денежный путь): дёшево и точно, ошибку
    #             ловит confirm-гейт + код-валидация → дорогая модель тут не нужна.
    #   copy    — генерация RSA-текстов: качество РУССКОГО важнее цены → сильная модель.
    llm_parsing: str = "deepseek/deepseek-chat"  # A/B: дёшево, ≈Claude на парсинге
    llm_copy: str = "anthropic/claude-sonnet-4.6"  # копирайт RU — лучшее качество (RSA)
    llm_fallback: str = "anthropic/claude-sonnet-4.6"  # Hermes выбыл (нет tool use на OpenRouter)
    # Пресеты для рантайм-переключателя /model (CSV slug'ов OpenRouter). Пусто => дефолт в
    # agent.router._DEFAULT_CHOICES (tool-use-capable модели). Своя модель — через /model в боте.
    model_choices: str = ""
    # Потолок генерации по ролям (явный max_tokens — экономия бюджета БЕЗ потери качества:
    # без него OpenRouter резервирует полный max-output против дневного бюджета; см.
    # agent.router.ROLE_MAX_TOKENS). Парсинг → крошечный tool-call; копирайт → короткий JSON.
    llm_max_tokens_parsing: int = 1024
    llm_max_tokens_copy: int = 2048
    # :floor — роутинг к самому дешёвому провайдеру (тот же вес = текст-нейтрально, но фиксирует
    # на одном эндпоинте → операционно рискованнее). По умолчанию ВЫКЛ (fail-safe к надёжности).
    openrouter_price_floor: bool = False
    # Роутинг ТОЛЬКО parsing-роли (самый чувствительный к задержке путь — пользователь ждёт в
    # «печатает…») к быстрейшему эндпоинту модели через OpenRouter provider:{sort}. Значения:
    # "throughput" (выше токенов/с) или "latency" (ниже TTFT); пусто => ВЫКЛ (текущее поведение).
    # Копирайт НЕ трогаем (там важнее качество). Как и :floor, фиксирует на конкретном эндпоинте →
    # операционно рискованнее, поэтому по умолчанию ВЫКЛ и включается осознанно в .env.
    openrouter_parsing_provider_sort: str = ""

    # Telegram
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_whitelist_chat_ids: str = ""  # "123,456"

    # Google Ads
    google_ads_developer_token: SecretStr = SecretStr("")
    google_ads_client_id: str = ""  # OAuth client id — не секрет
    google_ads_client_secret: SecretStr = SecretStr("")
    google_ads_refresh_token: SecretStr = SecretStr("")
    google_ads_login_customer_id: str = ""  # менеджерский аккаунт (MCC), контекст авторизации
    # §8/мультиаккаунт: ДОПОЛНИТЕЛЬНЫЕ MCC (под разными менеджерами), под которыми бот обходит/
    # логинится (Фаза 3 — аккаунты под разными MCC). CSV. Легаси-скаляр выше вложен в множество
    # (login_customer_id_set). Пусто => только основной login_customer_id (поведение не меняется).
    google_ads_login_customer_ids: str = ""
    google_ads_allowed_customer_ids: str = (
        ""  # белый список аккаунтов для МУТАЦИЙ (см. ads.client.ensure_allowed)
    )
    # §8: аккаунты, доступные ТОЛЬКО на чтение (сводка по дочерним MCC) ПОМИМО мутационного списка.
    # fail-closed; мутации этим НЕ затрагиваются (свой узкий замок). Пусто => чтение, как и мутации,
    # только на разрешённый аккаунт (поведение не меняется). См. ads.client.ensure_read_allowed.
    google_ads_read_customer_ids: str = ""
    google_ads_api_version: str = "v24"  # API-версия (мажор). SDK-пин google-ads — в pyproject.toml
    # Дневной лимит операций Google Ads API (§3): Basic dev-token = 15 000 операций/сутки; Standard —
    # фактически без лимита (ставь высоким). core.quota предупреждает на 80% и БЛОКИРУЕТ новые
    # МУТАЦИИ на 95% (чтение не блокируем). 0 ⇒ трекинг выключен (без гарда).
    google_ads_daily_op_limit: int = 15000

    # Безопасность / БД
    secrets_encryption_key: SecretStr = SecretStr("")
    # SecretStr: DSN несёт пароль БД — маскируем в repr/логах/трейсбеках (golden rule #5).
    # Реальное значение — только через .get_secret_value() (db.session, migrations.env).
    database_url: SecretStr = SecretStr("postgresql+asyncpg://aimash:aimash@localhost:5432/aimash")

    # Наблюдаемость / мониторинг ошибок (Sentry, опционально). Пусто => ВЫКЛ (core.observability):
    # ноль накладных расходов и сети. SecretStr — DSN считается чувствительным. Перф-трейсинг по
    # умолчанию 0.0 (без оверхеда на запросах); ошибки сэмплируются 100%.
    sentry_dsn: SecretStr = SecretStr("")
    sentry_traces_sample_rate: float = 0.0

    # Планировщик / расписание (§14). Глобальная кадэнс read-only задач (отчёт/аномалии/очистка).
    # REPORT_SCHEDULE — стандартная crontab-строка «мин час день месяц день_недели»: одним полем
    # покрывает и ежедневно, и еженедельно (ТЗ §14 «ежедн./еженед.»). По умолчанию ежедневно 09:00.
    # Невалидная строка НЕ роняет старт (это не security-гейт): scheduler откатывается на дефолт с
    # громким логом (fail-safe). UserSettings.report_schedule (per-user) — задел под мультиюзер;
    # пока единый источник глобального расписания — env.
    report_schedule: str = "0 9 * * *"  # crontab: ежедневно 09:00 (локальное время)
    anomaly_interval_hours: int = 6  # проверка аномалий каждые N часов
    cleanup_interval_minutes: int = 60  # очистка просроченных черновиков каждые N минут
    # §19: TTL активного черновика визарда «Создание кампании» (campaign_drafts). Щедрый по
    # умолчанию — Этап-2 round-trip с Google Sheets может занять день. Старше → status='abandoned'
    # (та же очистка, что и просроченные proposals; cleanup_interval_minutes задаёт кадэнс).
    campaign_draft_ttl_hours: int = 72

    # §20: краулинг сайта клиента (clients.crawler). Статический краулер (без headless) с жёсткими
    # лимитами — не перегружать чужой сайт и не голодить общий event loop (краул в фоне, bounded).
    crawl_max_pages: int = 50  # потолок числа страниц за обход (ТЗ §20.4: «до 50–100»)
    crawl_max_depth: int = 3  # глубина BFS от главной
    crawl_time_budget_s: float = 90.0  # общий бюджет времени на весь обход (asyncio.wait_for)
    crawl_delay_s: float = 0.5  # вежливая пауза между запросами к одному домену
    crawl_max_text_chars: int = 5000  # сколько текста берём с одной страницы (токены/поверхность)
    # §20: зависшая (running) crawl_jobs старше N минут → failed на реконсиляции (in-process задача
    # умерла с процессом на рестарте). Кадэнс — cleanup_interval_minutes (та же очистка).
    crawl_stale_minutes: int = 30
    # §20.3: сколько ждём молча после последнего сообщения профиля до авто-сохранения (менеджер
    # может слать инфу несколькими сообщениями подряд — накапливаем в буфер, потом извлекаем).
    client_text_idle_s: int = 60

    @property
    def whitelist(self) -> set[int]:
        return {int(x) for x in self.telegram_whitelist_chat_ids.split(",") if x.strip()}

    @property
    def model_choice_list(self) -> list[str]:
        """Пресеты моделей для /model (из env MODEL_CHOICES). Пусто => дефолт в agent.router."""
        return [m.strip() for m in self.model_choices.split(",") if m.strip()]

    @property
    def allowed_customer_ids(self) -> set[str]:
        """Аккаунты, которые боту РАЗРЕШЕНО трогать (нормализованные). Замок — в ads.client.
        Фильтруем по НОРМАЛИЗОВАННОМУ результату (не по сырому `x.strip()`): мусорный токен без
        цифр (inline-комментарий/плейсхолдер из .env) нормализуется в '' и НЕ должен попасть в
        множество — иначе '' протекает в замки (см. login_customer_id_set)."""
        return {
            n
            for x in self.google_ads_allowed_customer_ids.split(",")
            if (n := normalize_customer_id(x))
        }

    @property
    def read_customer_ids(self) -> set[str]:
        """§8: аккаунты, доступные на ЧТЕНИЕ помимо мутационного allow-list (сводка по дочерним
        MCC), нормализованные. Замок чтения — ads.client.ensure_read_allowed (fail-closed).
        Фильтр по нормализованному результату (мусор без цифр → '' → отбрасывается)."""
        return {
            n
            for x in self.google_ads_read_customer_ids.split(",")
            if (n := normalize_customer_id(x))
        }

    @property
    def login_customer_id_set(self) -> set[str]:
        """Все MCC (нормализованные), под которыми разрешён обход/логин (§8). Основной
        login_customer_id ∪ доп. список google_ads_login_customer_ids. Замок обхода —
        ads.client.ensure_manager_allowed (fail-closed на пустом множестве).

        КРИТИЧНО: фильтруем по НОРМАЛИЗОВАННОМУ результату, а не по сырому `x.strip()`. Раньше
        непустой мусор без цифр (напр. inline-комментарий из .env.defaults, «просочившийся» как
        значение) проходил `x.strip()`, но `normalize_customer_id(x) == ''` — и '' попадал в
        множество. Тогда стартовый discover_read_children делал ga.search(customer_id='') →
        GoogleAdsException «Invalid customer ID ''», а ensure_manager_allowed fail-open на ''.
        Фильтр по нормализованному значению убирает класс целиком (происхождение мусора неважно)."""
        base_n = normalize_customer_id(self.google_ads_login_customer_id)
        base = {base_n} if base_n else set()
        extra = {
            n
            for x in self.google_ads_login_customer_ids.split(",")
            if (n := normalize_customer_id(x))
        }
        return base | extra

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @model_validator(mode="after")
    def _require_encryption_key_in_prod(self) -> "Settings":
        """Fail-fast: в prod пустой/невалидный SECRETS_ENCRYPTION_KEY недопустим (токены
        шифруются at-rest). В dev/тестах (SQLite, без шифрования) — не требуем, чтобы суйта
        оставалась зелёной. Срабатывает единообразно для бота, scheduler, скриптов и Alembic."""
        if self.env == "prod":
            key = self.secrets_encryption_key.get_secret_value()
            if not key:
                raise ValueError(
                    "SECRETS_ENCRYPTION_KEY обязателен в prod — сгенерируй: Fernet.generate_key()"
                )
            try:
                from cryptography.fernet import Fernet

                # Round-trip (а не только конструктор Fernet): шифруем И расшифровываем пробу —
                # ловит порчу ключа / сломанный crypto-бэкенд ДО первой реальной мутации токенов,
                # а не на первом обращении к oauth_tokens. Без импорта core.secrets (тот тянет config).
                f = Fernet(key.encode())
                probe = b"aimash-keycheck"
                if f.decrypt(f.encrypt(probe)) != probe:
                    raise ValueError("round-trip mismatch")
            except Exception as e:
                raise ValueError(
                    "SECRETS_ENCRYPTION_KEY невалиден (нужен ключ Fernet.generate_key())"
                ) from e
        return self

    @model_validator(mode="after")
    def _require_whitelist_in_prod(self) -> "Settings":
        """Fail-fast: в prod пустой whitelist недопустим — иначе бот отвечал бы ВСЕМ (fail-open).
        В dev/тестах не требуем (удобство), но WhitelistMiddleware всё равно fail-closed (пустой
        whitelist => бот никому не отвечает), как и замок аккаунта (ads.client.ensure_allowed)."""
        if self.env == "prod" and not self.whitelist:
            raise ValueError(
                "TELEGRAM_WHITELIST_CHAT_IDS обязателен в prod — пустой whitelist означал бы "
                "ответы всем (fail-open). Укажи хотя бы один chat_id."
            )
        return self

    @model_validator(mode="after")
    def _require_google_ads_in_prod(self) -> "Settings":
        """Fail-fast: в prod без developer token / allowed_customer_ids бот всё равно не сможет
        работать с Google Ads. Падаем на СТАРТЕ (тут), а не на первом вызове API: ensure_allowed
        и так fail-closed на пустой allow-list, но это срабатывает позже — лучше не подняться с
        неполной конфигурацией. В dev/тестах не требуем (работа на фейках/без живых кредов)."""
        if self.env == "prod":
            missing = []
            if not self.google_ads_developer_token.get_secret_value():
                missing.append("GOOGLE_ADS_DEVELOPER_TOKEN")
            if not self.allowed_customer_ids:
                missing.append("GOOGLE_ADS_ALLOWED_CUSTOMER_IDS")
            if missing:
                raise ValueError(
                    f"В prod обязательны: {', '.join(missing)} — иначе бот не сможет работать с "
                    "Google Ads (fail-fast на старте, а не на первом вызове API)."
                )
        return self


settings = Settings()


def require_dev_env() -> None:
    """Гард dev-скриптов прямой записи (минуют confirm-гейт): разрешено ТОЛЬКО при ENV=dev
    (golden rule #10). Иначе SystemExit — скрипт не стартует вне dev. Единый источник, чтобы гард
    нельзя было забыть в новом demo-скрипте (вызывать первой строкой main())."""
    if settings.env != "dev":
        raise SystemExit(
            f"Прямая запись мимо confirm-гейта запрещена вне ENV=dev (сейчас ENV={settings.env!r}) "
            "— golden rule #10."
        )
