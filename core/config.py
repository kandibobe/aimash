"""Конфигурация из окружения (.env). Секреты только отсюда, никогда из кода.

Секреты обёрнуты в pydantic.SecretStr — маскируются в логах/трейсбеках/repr;
реальное значение доступно ТОЛЬКО через .get_secret_value() в точке использования.
SecretStr — это защита от утечки в логи, НЕ шифрование (за шифрование at-rest — core.secrets).
"""

from __future__ import annotations

from pydantic import SecretStr
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
    llm_parsing: str = "deepseek/deepseek-chat"  # A/B: дёшево, ≈Claude на парсинге
    llm_copy: str = "deepseek/deepseek-chat"  # копирайт ок и дёшево; апгрейд → claude
    llm_fallback: str = "anthropic/claude-sonnet-4.6"  # Hermes выбыл (нет tool use на OpenRouter)

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
    database_url: str = "postgresql+asyncpg://aimash:aimash@localhost:5432/aimash"

    @property
    def whitelist(self) -> set[int]:
        return {int(x) for x in self.telegram_whitelist_chat_ids.split(",") if x.strip()}

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


settings = Settings()
