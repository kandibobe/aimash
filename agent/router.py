"""Адаптер модели — единая точка ко ВСЕМ LLM через OpenRouter (OpenAI-совместимый API).

Смена модели = строка конфига (.env), не переписывание кода. Дефолт — дешёвая.
Решение по модели принимается по A/B-тесту (scripts/ab_test_models.py), а не по бренду.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from openai import AsyncOpenAI

from core.config import _VALID_PROVIDER_SORTS, settings
from core.resilience import (
    LLM_TIMEOUT_S,
    call_llm,
)  # таймаут+ретрай на rate-limit/timeout OpenRouter

# Назначение → конкретная модель (дефолты из .env; сменяемо в рантайме через /model)
ROLE_MODELS = {
    "parsing": settings.llm_parsing,  # парсинг команд → function calling (денежный путь)
    "copy": settings.llm_copy,  # генерация RSA-текстов
    "fallback": settings.llm_fallback,
    # P2: кластеризация/интент keyword research; пусто в .env ⇒ модель parsing (как раньше)
    "clustering": settings.llm_clustering or settings.llm_parsing,
    # P3: аналитик (агентный нарратив /audit); пусто ⇒ модель parsing (дешёвый дефолт)
    "analyst": settings.llm_analyst or settings.llm_parsing,
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
    "clustering": settings.llm_max_tokens_parsing,  # ответ — компактный JSON групп
    "analyst": settings.llm_max_tokens_analyst,  # короткий человеческий разбор аудита
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
    "deepseek/deepseek-chat",  # дёшево, дефолт (DeepSeek V3)
    "deepseek/deepseek-v4-pro",  # лучший DeepSeek (V4 Pro)
    "anthropic/claude-sonnet-4.6",  # сильная, топ-копирайт
    "anthropic/claude-opus-4.8",  # максимум качества (дорого)
    "openai/gpt-4o-mini",  # дёшево, надёжный tool use
    "openai/gpt-4o",  # сильная, универсал
]
MODEL_CHOICES: list[str] = settings.model_choice_list or _DEFAULT_CHOICES

# Человекочитаемые подписи к slug'ам для кнопок /model (что для чего). Неизвестный slug
# (своя модель / env MODEL_CHOICES) показывается как есть. Slug'и сверены со списком OpenRouter.
MODEL_LABELS: dict[str, str] = {
    "deepseek/deepseek-chat": "🐬 DeepSeek V3 · дёшево (дефолт)",
    "deepseek/deepseek-v4-pro": "🐬 DeepSeek V4 Pro · лучший DeepSeek",
    "anthropic/claude-sonnet-4.6": "🧠 Claude Sonnet 4.6 · топ-тексты",
    "anthropic/claude-opus-4.8": "👑 Claude Opus 4.8 · максимум (дорого)",
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


# Один переиспользуемый AsyncOpenAI на event loop. Зачем: AsyncOpenAI владеет httpx-пулом —
# новый клиент на каждый вызов = холодный TCP+TLS-хендшейк к openrouter.ai каждый раз (~150-400мс
# чистого setup на не-колокированном хосте). Синглтон держит соединение тёплым (keep-alive) →
# хендшейк только на первом запросе. Мемоизация ПО event loop (как ADS-семафор в core.resilience):
# pytest-asyncio даёт свой loop на тест, httpx-клиент из мёртвого loop бросил бы ошибку.
# ВАЖНО (fail-closed, §10): проверка пустого ключа — ВНУТРИ, чтобы пустой ключ не закэшировал
# битый клиент. Ключ читается из SecretStr в момент конструирования и в логи/repr не попадает (§5).
# max_retries=0 + явный timeout: tenacity (core.resilience.call_llm) — ЕДИНСТВЕННЫЙ авторитет
# ретраев, иначе встроенные 2 ретрая SDK вложились бы в 3 попытки tenacity (до 9 HTTP-попыток).
_client_cache: AsyncOpenAI | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _client() -> AsyncOpenAI:
    global _client_cache, _client_loop
    key = settings.openrouter_api_key.get_secret_value()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY не задан в .env")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # вне event loop (не наш рантайм-путь) — не кэшируем по loop
        loop = None
    if _client_cache is None or _client_loop is not loop:
        _client_cache = AsyncOpenAI(
            api_key=key,
            base_url=settings.openrouter_base_url,
            max_retries=0,  # ретраи — только через tenacity (core.resilience), без вложенности
            timeout=httpx.Timeout(LLM_TIMEOUT_S),
        )
        _client_loop = loop
    return _client_cache


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
    extra_body: dict[str, Any] = {"usage": {"include": True}}
    # Provider-routing по скорости ТОЛЬКО для parsing (денежный путь, пользователь ждёт ответ):
    # OpenRouter выберет быстрейший эндпоинт ТОЙ ЖЕ модели (вес не меняется → текст-нейтрально).
    # Config-gated, по умолчанию ВЫКЛ (settings.openrouter_parsing_provider_sort=""). Копирайт не
    # трогаем. Веса/качество модели неизменны — это выбор эндпоинта, не модели.
    # Config уже нормализует значение (core.config._normalize_provider_sort); membership-check тут —
    # defense-in-depth: даже при подменённых в тестах/скриптах settings кривой sort НЕ улетит в API.
    sort = settings.openrouter_parsing_provider_sort
    if role == "parsing" and sort and sort in _VALID_PROVIDER_SORTS:
        extra_body["provider"] = {"sort": sort}
    kwargs["extra_body"] = extra_body
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
