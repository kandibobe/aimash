"""Адаптер модели — единая точка ко ВСЕМ LLM через OpenRouter (OpenAI-совместимый API).

Смена модели = строка конфига (.env), не переписывание кода. Дефолт — дешёвая.
Решение по модели принимается по A/B-тесту (scripts/ab_test_models.py), а не по бренду.
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from core.config import settings
from core.resilience import call_llm  # таймаут+ретрай на rate-limit/timeout OpenRouter

# Назначение → конкретная модель (дефолты из .env; сменяемо в рантайме через /model)
ROLE_MODELS = {
    "parsing": settings.llm_parsing,  # парсинг команд → function calling (денежный путь)
    "copy": settings.llm_copy,  # генерация RSA-текстов
    "fallback": settings.llm_fallback,
}

# Потолок генерации по ролям (явный max_tokens). Зачем ЯВНО: без max_tokens OpenRouter
# РЕЗЕРВИРУЕТ полный max-output модели против дневного бюджета (предоплата-резерв, не факт-расход)
# — для дорогой модели это ~$0.96/запрос резерва на ~$0.003 факта. Явный потолок = экономия
# бюджета БЕЗ потери качества: ответы крошечные (парс → tool-call; копирайт → короткий JSON),
# до потолка не дотягивают → обрезки не будет. Переопределяемо аргументом chat(max_tokens=…).
ROLE_MAX_TOKENS = {
    "parsing": settings.llm_max_tokens_parsing,
    "copy": settings.llm_max_tokens_copy,
    "fallback": settings.llm_max_tokens_copy,
}

# Кандидаты для A/B-теста (см. scripts/ab_test_models.py)
AB_CANDIDATES = [
    "deepseek/deepseek-chat",
    "nousresearch/hermes-4-70b",
    "nousresearch/hermes-4-405b",
    "anthropic/claude-sonnet-4.6",
]


def _with_price_floor(model: str) -> str:
    """`:floor` → роутинг к самому дешёвому провайдеру (тот же вес модели = текст-нейтрально).
    По умолчанию ВЫКЛ (settings.openrouter_price_floor): фиксирует на одном, потенциально менее
    надёжном эндпоинте → включается осознанно в .env. Slug с уже заданным вариантом (':') не трогаем."""
    if settings.openrouter_price_floor and ":" not in model:
        return f"{model}:floor"
    return model


# Пресеты для рантайм-переключателя /model. ВАЖНО: только модели с function calling (tool use) —
# парсинг команд (денежный путь) без него не работает (Hermes выбыл именно поэтому). Свою модель
# можно задать в боте (/model <vendor/slug>) или через env MODEL_CHOICES.
_DEFAULT_CHOICES = [
    "deepseek/deepseek-chat",  # дёшево, дефолт
    "anthropic/claude-sonnet-4.6",  # сильная
    "openai/gpt-4o-mini",  # дёшево, надёжный tool use
    "openai/gpt-4o",  # сильная, tool use
]
MODEL_CHOICES: list[str] = settings.model_choice_list or _DEFAULT_CHOICES

# Человекочитаемые подписи к slug'ам для кнопок /model (что для чего). Неизвестный slug
# (своя модель / env MODEL_CHOICES) показывается как есть.
MODEL_LABELS: dict[str, str] = {
    "deepseek/deepseek-chat": "🐬 DeepSeek V3 · дёшево (дефолт)",
    "anthropic/claude-sonnet-4.6": "🧠 Claude Sonnet 4.6 · топ-тексты",
    "openai/gpt-4o": "🤖 GPT-4o · сильный, универсал",
    "openai/gpt-4o-mini": "⚡ GPT-4o mini · дёшево, надёжный",
}


def model_label(slug: str) -> str:
    """Подпись модели для UI: дружелюбное имя если известно, иначе сам slug."""
    return MODEL_LABELS.get(slug, slug)


# Рантайм-override: одна активная модель НА ВСЕ роли (parsing/copy). None => дефолты ROLE_MODELS.
# Глобально на процесс (места вызова chat() в adcopy/keywords не знают chat_id). Персист —
# в БД на стороне бота (bot.main: UserSettings.model_override), сюда возвращается при старте.
_active_model: str | None = None


def set_active_model(model: str | None) -> None:
    """Включить активную модель для всех ролей (или None — вернуть дефолты ROLE_MODELS)."""
    global _active_model
    _active_model = (model or "").strip() or None


def get_active_model() -> str | None:
    """Текущий рантайм-override (None — используются дефолты по ролям)."""
    return _active_model


def effective_model(role: str = "parsing") -> str:
    """Какая модель реально пойдёт в запрос для роли: override > роль-дефолт > parsing-дефолт."""
    return _active_model or ROLE_MODELS.get(role) or settings.llm_parsing


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
    max_tokens: int | None = None,
) -> Any:
    """Один вызов модели. `role` выбирает модель из ROLE_MODELS; `model` — явное переопределение.

    Возвращает message ответа (с .content и/или .tool_calls). Тул-исполнение — НЕ здесь:
    mutation-инструменты только предлагают proposal, выполняет код после «да».
    """
    chosen = model or effective_model(role)  # явный model > рантайм-override > дефолт роли
    kwargs: dict[str, Any] = {"model": _with_price_floor(chosen), "messages": messages}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if temperature is not None:
        kwargs["temperature"] = temperature
    # Явный потолок генерации (ROLE_MAX_TOKENS) — экономия бюджета без потери качества (см. выше).
    mt = max_tokens if max_tokens is not None else ROLE_MAX_TOKENS.get(role)
    if mt:
        kwargs["max_tokens"] = mt
    # usage:{include:true} — на текущем OpenRouter no-op (cost и так в ответе), но на старых
    # сборках включает usage.cost; безвредно. Через extra_body — это вне типизированных полей SDK.
    kwargs["extra_body"] = {"usage": {"include": True}}
    # call_llm: zero-arg фабрика — tenacity создаёт свежую корутину на каждую попытку.
    # label → лог запроса к LLM (модель/роль, без секретов; ТЗ §15).
    resp = await call_llm(
        lambda: _client().chat.completions.create(**kwargs), label=f"{chosen}/{role}"
    )
    # Учёт расхода (токены + реальная стоимость OpenRouter) для /balance. Наблюдаемость не должна
    # ронять денежный путь — любую проблему с usage глотаем (record() и сам в try, тут — страховка).
    try:
        from core.usage import record

        record(role, getattr(resp, "usage", None))
    except Exception:  # noqa: BLE001
        pass
    return resp.choices[0].message
