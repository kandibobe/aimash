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
    google_ads_allowed_customer_ids: str = (
        ""  # белый список аккаунтов (см. ads.client.ensure_allowed)
    )
    google_ads_api_version: str = "v24"  # API-версия (мажор). SDK-пин google-ads — в pyproject.toml

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

    @property
    def whitelist(self) -> set[int]:
        return {int(x) for x in self.telegram_whitelist_chat_ids.split(",") if x.strip()}

    @property
    def model_choice_list(self) -> list[str]:
        """Пресеты моделей для /model (из env MODEL_CHOICES). Пусто => дефолт в agent.router."""
        return [m.strip() for m in self.model_choices.split(",") if m.strip()]

    @property
    def allowed_customer_ids(self) -> set[str]:
        """Аккаунты, которые боту РАЗРЕШЕНО трогать (нормализованные). Замок — в ads.client."""
        return {
            normalize_customer_id(x)
            for x in self.google_ads_allowed_customer_ids.split(",")
            if x.strip()
        }

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

                Fernet(key.encode())
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
