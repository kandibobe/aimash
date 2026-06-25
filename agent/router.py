"""Адаптер модели — единая точка ко ВСЕМ LLM через OpenRouter (OpenAI-совместимый API).

Смена модели = строка конфига (.env), не переписывание кода. Дефолт — дешёвая.
Решение по модели принимается по A/B-тесту (scripts/ab_test_models.py), а не по бренду.
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from core.config import settings

# Назначение → конкретная модель (сменяемо через .env)
ROLE_MODELS = {
    "parsing": settings.llm_parsing,  # парсинг команд → function calling (денежный путь)
    "copy": settings.llm_copy,  # генерация RSA-текстов
    "fallback": settings.llm_fallback,
}

# Кандидаты для A/B-теста (см. scripts/ab_test_models.py)
AB_CANDIDATES = [
    "deepseek/deepseek-chat",
    "nousresearch/hermes-4-70b",
    "nousresearch/hermes-4-405b",
    "anthropic/claude-sonnet-4.6",
]


def _client() -> AsyncOpenAI:
    if not settings.openrouter_api_key.get_secret_value():
        raise RuntimeError("OPENROUTER_API_KEY не задан в .env")
    return AsyncOpenAI(
        api_key=settings.openrouter_api_key.get_secret_value(),
        base_url=settings.openrouter_base_url,
    )


async def chat(
    messages: list[dict[str, Any]],
    *,
    role: str = "parsing",
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
) -> Any:
    """Один вызов модели. `role` выбирает модель из ROLE_MODELS; `model` — явное переопределение.

    Возвращает message ответа (с .content и/или .tool_calls). Тул-исполнение — НЕ здесь:
    mutation-инструменты только предлагают proposal, выполняет код после «да».
    """
    chosen = model or ROLE_MODELS.get(role) or settings.llm_parsing
    kwargs: dict[str, Any] = {"model": chosen, "messages": messages}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = await _client().chat.completions.create(**kwargs)
    return resp.choices[0].message
