"""Адаптер модели — единая точка ко ВСЕМ LLM через OpenRouter (OpenAI-совместимый API).

Смена модели = строка конфига (.env), не переписывание кода. Дефолт — дешёвая.
Решение по модели принимается по A/B-тесту (scripts/ab_test_models.py), а не по бренду.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.langfuse_tracing import is_enabled as _lf_enabled, get_client as _lf_client

from core.config import _VALID_PROVIDER_SORTS, settings
from core.resilience import (
    LLM_TIMEOUT_S,
    _is_retryable_llm,  # A4: транзиентность ошибки → решаем, стоит ли деградировать на fallback
    call_llm,
)  # таймаут+ретрай на rate-limit/timeout OpenRouter

# Назначение → конкретная модель (дефолты из .env; сменяемо в рантайме через /model)
ROLE_MODELS = {
    "parsing": settings.llm_parsing,  # парсинг команд → function calling (денежный путь)
    "copy": settings.llm_copy,  # генерация RSA-текстов
    "fallback": settings.llm_fallback,
    # Смысловые keyword-задачи (seed/релевантность/минус-слова/кластеризация/профиль §20);
    # пусто в .env ⇒ модель parsing (обратная совместимость). Отделено от латентно-чувствительного
    # командного парсинга — качество RU-семантики важнее скорости на этих РЕДКИХ вызовах.
    "keywords": settings.llm_keywords or settings.llm_parsing,
    # P2: кластеризация/интент keyword research; пусто в .env ⇒ модель parsing (как раньше)
    "clustering": settings.llm_clustering or settings.llm_parsing,
    # P3: аналитик (агентный нарратив /audit); пусто ⇒ модель parsing (дешёвый дефолт)
    "analyst": settings.llm_analyst or settings.llm_parsing,
    # §20 досье, map-фаза: извлечение фактов из чанка краула. ДЕСЯТКИ вызовов на сайт ⇒ дешёвая
    # модель (пусто ⇒ parsing). Ошибки безвредны: выход — JSON, который валидирует и сливает КОД.
    "extract": settings.llm_extract or settings.llm_parsing,
    # §20 досье, reduce-фаза: ОДИН синтез связной прозы на сайт ⇒ сильная модель (пусто ⇒ keywords).
    "dossier": settings.llm_dossier or settings.llm_keywords or settings.llm_parsing,
    # Prompt Injection Guard: своя модель (дешёвая, быстрая), не зависит от основных ролей.
    # Пусто в .env ⇒ guard отключён (prompt_guard_enabled не отменяет отсутствие модели).
    "guard": settings.prompt_guard_model or "",
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
    "keywords": settings.llm_max_tokens_keywords,  # фильтр релевантности на 120 ключей крупнее parsing
    "clustering": settings.llm_max_tokens_parsing,  # ответ — компактный JSON групп
    "analyst": settings.llm_max_tokens_analyst,  # короткий человеческий разбор аудита
    # §20 досье: JSON фактов с чанка и финальная проза — оба заметно крупнее keywords-2048.
    # Дефолт роли — страховка; clients/dossier_* всё равно передают max_tokens ЯВНО (R13).
    "extract": settings.llm_max_tokens_extract,
    "dossier": settings.llm_max_tokens_dossier,
    "guard": 4,  # одно слово SAFE/INJECTION
}


# Семейства моделей, ОТКЛОНЯЮЩИЕ sampling-параметры (temperature/top_p/top_k) на нативном Anthropic
# API (400). Через OpenRouter поведение не гарантировано (может пробросить и словить 400), поэтому
# срезаем на нашей стороне по слагу РЕЗОЛВНУТОЙ модели (см. chat._call). Матч по подстроке —
# терпим и точку, и дефис в версии (anthropic/claude-opus-4.8 ~ ...-4-8). Дешёвая деградация:
# без temperature модель идёт на своём дефолте — качество keyword/copy это не роняет.
_SAMPLING_STRICT_SUBSTRINGS = (
    "claude-opus-4.8",
    "claude-opus-4-8",
    "claude-opus-4.7",
    "claude-opus-4-7",
    "claude-sonnet-5",
    "claude-fable-5",
)


def _omits_sampling(model: str) -> bool:
    """True, если модель (по слагу) отклоняет temperature/top_p/top_k (Opus 4.7+/Sonnet 5/Fable 5).
    Тогда chat() НЕ добавляет temperature в payload (иначе риск 400 при пробросе OpenRouter)."""
    m = (model or "").casefold()
    return any(s in m for s in _SAMPLING_STRICT_SUBSTRINGS)


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
# Кэш AsyncOpenAI клиента — лениво импортируется из langfuse.openai (если трейсинг включён)
# или из openai напрямую. langfuse.openai — прозрачный прокси: тот же API + авто-захват
# model/tokens/стоимости в Langfuse traces. Без Langfuse (нет ключей) — обычный openai.AsyncOpenAI.
# Импорт ВНУТРИ _client(), а не на уровне модуля: Langfuse должен быть инициализирован ДО
# первого импорта langfuse.openai (иначе прокси инициализируется без клиента → no-op).
_client_cache: Any = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _client() -> Any:
    global _client_cache, _client_loop
    key = settings.openrouter_api_key.get_secret_value()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY не задан в .env")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # вне event loop (не наш рантайм-путь) — не кэшируем по loop
        loop = None
    if _client_cache is None or _client_loop is not loop:
        import httpx

        if _lf_enabled():
            # Langfuse-обёрнутый AsyncOpenAI — автоматический захват model/tokens/стоимости
            # в traces + все OpenAI-вызовы становятся generation-спанами.
            from langfuse.openai import AsyncOpenAI as _LFAsyncOpenAI

            _AsyncOpenAI = _LFAsyncOpenAI
        else:
            from openai import AsyncOpenAI as _PlainAsyncOpenAI

            _AsyncOpenAI = _PlainAsyncOpenAI

        _client_cache = _AsyncOpenAI(
            api_key=key,
            base_url=settings.openrouter_base_url,
            max_retries=0,  # ретраи — только через tenacity (core.resilience), без вложенности
            timeout=httpx.Timeout(LLM_TIMEOUT_S),
        )
        _client_loop = loop
    return _client_cache


def finish_reason(msg: Any) -> str:
    """Почему модель остановилась: 'stop' | 'length' (обрыв по max_tokens) | 'tool_calls' | ''.
    '' — неизвестно (старый мок в тестах/чужой объект). Вызывающим, у которых НЕПОЛНЫЙ ответ
    меняет смысл (списочные JSON-ответы keywords), обязаны различать 'length' и 'stop'."""
    return str(getattr(msg, "finish_reason", "") or "")


async def chat(
    messages: list[dict[str, Any]],
    *,
    role: str = "parsing",
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    langfuse_ctx: dict[str, Any] | None = None,
) -> Any:
    """Один вызов модели. `role` выбирает модель из ROLE_MODELS; `model` — явное переопределение.

    Возвращает message ответа (с .content и/или .tool_calls). Тул-исполнение — НЕ здесь:
    mutation-инструменты только предлагают proposal, выполняет код после «да».

    langfuse_ctx: опциональный словарь с контекстом трейсинга —
        session_id: str  — Telegram chat_id (группировка сообщений в сессии)
        user_id: str     — Telegram user_id (атрибуция стоимости)
        tags: list[str]  — теги для фильтрации ('parsing', 'copy', 'mutation')
        metadata: dict   — произвольные метаданные (account_id и т.д.)
    """
    chosen = model or effective_model(role)  # явный model > рантайм-override > дефолт роли
    kwargs: dict[str, Any] = {"messages": messages}  # model проставляется в _call (для fallback)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
        # A5: денежный путь — ОДНО действие за вызов. Без этого модели, умеющие параллельные
        # tool-calls (gpt-4o, deepseek), на «поставь на паузу А и Б» отдают два вызова, а
        # handle_command берёт только tool_calls[0] → пользователь видит ОДИН черновик, а думает,
        # что подтвердил оба (тихая потеря действия на чужих деньгах). Только для parsing (там это
        # критично); для copy/clustering параллельность безвредна.
        if role == "parsing":
            kwargs["parallel_tool_calls"] = False
    # temperature добавляется в _call ПО фактическому слагу (не тут): «строгие» семейства
    # (Opus 4.7+/Sonnet 5/Fable 5) отклоняют sampling-параметры → _omits_sampling их срезает.
    # Это покрывает и fallback-путь (fb может быть другой моделью, чем chosen).
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

    async def _call(model_slug: str) -> Any:
            # call_llm: zero-arg фабрика — tenacity создаёт свежую корутину на каждую попытку.
            # label → лог запроса к LLM (модель/роль, без секретов; ТЗ §15).

            # Langfuse-контекст: session_id/user_id/tags/metadata — обогащают авто-трейс.
            # Делаем ДО вызова API, чтобы langfuse.openai прикрепил generation к правильной трассе.
            # Fail-soft: любая проблема с трейсингом глотается — не мешаем денежному пути.
            if langfuse_ctx and _lf_enabled():
                try:
                    from langfuse import get_client as _get_lf

                    lf = _get_lf()
                    if lf is not None:
                        update_kwargs: dict[str, Any] = {}
                        if "session_id" in langfuse_ctx:
                            update_kwargs["session_id"] = langfuse_ctx["session_id"]
                        if "user_id" in langfuse_ctx:
                            update_kwargs["user_id"] = langfuse_ctx["user_id"]
                        if "tags" in langfuse_ctx:
                            update_kwargs["tags"] = langfuse_ctx["tags"]
                        if "metadata" in langfuse_ctx:
                            update_kwargs["metadata"] = langfuse_ctx["metadata"]
                        if update_kwargs:
                            lf.update_current_trace(**update_kwargs)
                except Exception:  # noqa: BLE001 — трейсинг не должен ронять LLM-вызов
                    pass

            call_kwargs = {**kwargs, "model": _with_price_floor(model_slug)}
            # temperature — только если модель его принимает (Opus 4.7+/Sonnet 5/Fable 5 отклоняют).
            if temperature is not None and not _omits_sampling(model_slug):
                call_kwargs["temperature"] = temperature
            resp = await call_llm(
                lambda: _client().chat.completions.create(**call_kwargs), label=f"{model_slug}/{role}"
            )
            # Учёт расхода (токены + реальная стоимость OpenRouter) для /balance. Наблюдаемость не
            # должна ронять денежный путь — любую проблему с usage глотаем (record() и сам в try).
            try:
                from core.usage import record

                record(role, getattr(resp, "usage", None))
            except Exception:  # noqa: BLE001
                pass
            choice = resp.choices[0]
            msg = choice.message
            # finish_reason нужен вызывающим, чья корректность ЗАВИСИТ от полноты ответа: обрыв по
            # max_tokens отдаёт валидный, но НЕПОЛНЫЙ текст, и fail-open-парсер молча решает, что
            # «модель ничего не нашла» (keywords/filter.py: все ключи «релевантны»). Кладём на message
            # (SDK-модель допускает extra-поля); присвоение под try — контракт chat() не должен падать
            # из-за наблюдаемости. Читать — через finish_reason(msg).
            try:
                msg.finish_reason = getattr(choice, "finish_reason", None) or ""
            except Exception:  # noqa: BLE001 — не наш путь (чужой объект сообщения)
                pass
            return msg

    try:
        return await _call(chosen)
    except Exception as e:
        # A4: деградация при ТРАНЗИЕНТНОМ отказе основной модели (rate-limit/timeout/5xx после
        # исчерпания ретраев call_llm) — ОДНА попытка на резервную модель, если она отлична от уже
        # испробованной. Нетранзиентные (BadRequest/невалидная схема) НЕ фолбэчим (та же ошибка).
        # Раньше роль fallback была объявлена в конфиге и /balance, но не вызывалась нигде — деградации
        # LLM не было; сбой основной модели просто ронял парсинг.
        fb = ROLE_MODELS.get("fallback") or settings.llm_fallback
        if _is_retryable_llm(e) and fb and fb != chosen:
            import logging

            logging.getLogger("aimash.router").warning(
                "основная модель %s недоступна (%s) — переключаюсь на резервную %s",
                chosen,
                type(e).__name__,
                fb,
            )
            return await _call(fb)
        raise
