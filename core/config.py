"""Конфигурация из окружения (.env). Секреты только отсюда, никогда из кода."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Окружение
    env: str = "dev"  # dev => только TEST MCC

    # Модель через OpenRouter (сменяемая)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # имена llm_* (не model_*) — иначе шадоуят метод BaseModel.model_copy()
    llm_parsing: str = "deepseek/deepseek-chat"          # A/B: дёшево, ≈Claude на парсинге
    llm_copy: str = "deepseek/deepseek-chat"             # копирайт ок и дёшево; апгрейд → claude
    llm_fallback: str = "anthropic/claude-sonnet-4.6"    # Hermes выбыл (нет tool use на OpenRouter)

    # Telegram
    telegram_bot_token: str = ""
    telegram_whitelist_chat_ids: str = ""  # "123,456"

    # Google Ads
    google_ads_developer_token: str = ""
    google_ads_client_id: str = ""
    google_ads_client_secret: str = ""
    google_ads_refresh_token: str = ""
    google_ads_login_customer_id: str = ""
    google_ads_api_version: str = "v24"

    # Безопасность / БД
    secrets_encryption_key: str = ""
    database_url: str = "postgresql+asyncpg://aimash:aimash@localhost:5432/aimash"

    @property
    def whitelist(self) -> set[int]:
        return {int(x) for x in self.telegram_whitelist_chat_ids.split(",") if x.strip()}

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


settings = Settings()
