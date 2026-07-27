"""§20: агентский WRITE-инструмент для клиентского профиля.

Инструмент save_client_profile:
- Менеджер присылает free-text → агент вызывает save_client_profile
- Бот парсит текст через LLM (clients.profile_extract.extract_profile)
- Создаёт proposal (profile_save или profile_update) через confirm-гейт
- Показывает diff «было→станет» с кнопками ✅/✏️/✖️

Это memory-операция (НЕ Google Ads): исполняется через clients.execute.execute_confirmed_memory.

Имена инструментов заведомо вне MUTATION_TOOLS — не пересекаются с 39 мутационными.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field

# Имена заведомо непересекающиеся с MUTATION_TOOLS
CLIENT_PROFILE_TOOLS = frozenset({"save_client_profile"})
CLIENT_PROFILE_READ_TOOLS = frozenset(
    {"get_client_profile", "get_profile_context", "get_client_dossier"}
)


def _non_empty_text(v: str) -> str:
    s = str(v).strip()
    if not s:
        raise ValueError("текст не может быть пустым")
    return s


NonEmptyText = Annotated[str, AfterValidator(_non_empty_text)]


class SaveClientProfile(BaseModel):
    """§20: сохранить/обновить профиль клиента из свободного текста менеджера.

    Агент передаёт текст как есть — LLM-парсинг (clients.profile_extract) и мердж
    (клиенты.store.preview_merge) делает КОД. Если у customer_id уже есть профиль —
    обновление с мерджем (ничего не теряется). Если нет — создание нового.

    Все неструктурированные детали из текста, не попавшие в поля, сохраняются в notes.
    """

    customer_id: str = Field(
        min_length=1, max_length=20,
        description="ID рекламного аккаунта Google Ads (customer_id), к которому привязать профиль"
    )
    text: NonEmptyText = Field(
        description="Свободный текст менеджера: бренд, описание, сайт, услуги, цены, контакты, гео"
    )
    source: str = Field(
        default="text",
        description="Источник: text (ручной ввод) | crawl (краулинг). Не менять без причины."
    )


# Tool-описание для модели OpenAI/OpenRouter
SAVE_CLIENT_PROFILE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "save_client_profile",
        "description": "Сохранить или обновить профиль клиента по свободному тексту менеджера. "
        "Менеджер пишет 'Клиент Kasi Motors — автодилер в Найроби...' — "
        "передай текст целиком, код сам разберёт на поля (бренд/услуги/цены/контакты/гео). "
        "Если профиль уже есть — обновится с сохранением старых данных (ничего не теряется). "
        "Всегда указывай customer_id из активного аккаунта чата.",
        "parameters": SaveClientProfile.model_json_schema(),
    },
}
