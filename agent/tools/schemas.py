"""Типизированные схемы инструментов агента (Pydantic) + tool-описания для модели.

Модель ЗАПОЛНЯЕТ схему → код ВАЛИДИРУЕТ диапазоны → дальше confirm-гейт.
Read-инструменты выполняются сразу; mutation-инструменты только предлагают proposal.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Currency = Literal["USD", "UAH", "EUR", "percent"]
MatchType = Literal["broad", "phrase", "exact"]

# Какие инструменты — изменяющие (mutation), какие read-only.
MUTATION_TOOLS = {
    "update_budget",
    "update_bid",
    "add_keywords",
    "add_negative_keywords",
    "pause_campaign",
    "resume_campaign",
    "set_geo_proximity",
}
READ_TOOLS = {"get_stats"}


# ── Pydantic-схемы (валидация в коде, не на доверии к модели) ───────────────────
class UpdateBudget(BaseModel):
    campaign: str
    mode: Literal["increase_by_percent", "increase_by_amount", "set_to"]
    value: float = Field(gt=0)
    currency: Currency = "USD"

    @field_validator("value")
    @classmethod
    def _pct_sane(cls, v, info):
        if info.data.get("mode") == "increase_by_percent" and v > 1000:
            raise ValueError("процент изменения подозрительно большой (>1000%)")
        return v


class UpdateBid(BaseModel):
    # campaign обязателен: ставка живёт на уровне ad group, нужно знать какой кампании.
    # Отсутствие кампании => ValidationError в loop ДО кнопок (а не raise после «да»).
    campaign: str
    mode: Literal["increase_by_percent", "set_to"]
    value: float = Field(gt=0)
    currency: Currency = "USD"

    @field_validator("value")
    @classmethod
    def _pct_sane(cls, v, info):
        if info.data.get("mode") == "increase_by_percent" and v > 1000:
            raise ValueError("процент изменения подозрительно большой (>1000%)")
        return v


class AddKeywords(BaseModel):
    campaign: str  # обязателен: ключи добавляются в группы этой кампании
    keywords: list[str] = Field(min_length=1, max_length=50)
    match_type: MatchType


class AddNegativeKeywords(BaseModel):
    # campaign обязателен: минус-слова добавляются на уровне кампании.
    campaign: str
    keywords: list[str] = Field(min_length=1, max_length=50)
    match_type: MatchType = "broad"


class PauseCampaign(BaseModel):
    campaign: str


class ResumeCampaign(BaseModel):
    campaign: str


class SetGeoProximity(BaseModel):
    campaign: str  # обязателен: радиус-таргетинг привязывается к кампании
    location: str
    radius_km: float = Field(gt=0, le=2000)  # лимит Google Ads


class GetStats(BaseModel):
    account: str | None = None
    period_days: int = Field(default=30, gt=0, le=400)


SCHEMAS: dict[str, type[BaseModel]] = {
    "update_budget": UpdateBudget,
    "update_bid": UpdateBid,
    "add_keywords": AddKeywords,
    "add_negative_keywords": AddNegativeKeywords,
    "pause_campaign": PauseCampaign,
    "resume_campaign": ResumeCampaign,
    "set_geo_proximity": SetGeoProximity,
    "get_stats": GetStats,
}


# ── OpenAI/OpenRouter tool-описания (генерим из Pydantic) ───────────────────────
def _tool(name: str, description: str, model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


TOOLS: list[dict] = [
    _tool("update_budget", "Изменить дневной бюджет кампании.", UpdateBudget),
    _tool(
        "update_bid",
        "Изменить ставку CPC на уровне групп объявлений указанной кампании "
        "(требуется ручная стратегия Manual CPC). Всегда указывай campaign.",
        UpdateBid,
    ),
    _tool(
        "add_keywords",
        "Добавить ключевые слова (с типом соответствия) в группы указанной кампании. "
        "Всегда указывай campaign.",
        AddKeywords,
    ),
    _tool(
        "add_negative_keywords",
        "Добавить минус-слова на уровне указанной кампании. Всегда указывай campaign.",
        AddNegativeKeywords,
    ),
    _tool("pause_campaign", "Поставить кампанию на паузу.", PauseCampaign),
    _tool("resume_campaign", "Возобновить (включить) кампанию из паузы.", ResumeCampaign),
    _tool("set_geo_proximity", "Таргетинг по точке с радиусом (км).", SetGeoProximity),
    _tool("get_stats", "Прочитать статистику (read-only).", GetStats),
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Команда неоднозначна (не указана кампания/сумма/направление) — переспросить, НЕ угадывать.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
]
