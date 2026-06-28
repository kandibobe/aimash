"""Устойчивость сетевых вызовов: таймаут + ретрай с экспоненциальным backoff+jitter.

ГДЕ применяется (самый внутренний слой — сам сетевой вызов, чтобы гейты безопасности
НЕ ретраились): обёртка над `asyncio.to_thread(<sdk>)` в ads.mutations и над одним вызовом
OpenRouter в agent.router. Ретраит ТОЛЬКО транзиентные ошибки.

Деньги (golden rules): мутации Google Ads НЕ идемпотентны. Поэтому:
- Ретраим только ошибки, означающие, что запрос НЕ дошёл/не выполнен (rate-limit, unavailable)
  или транзиентный отказ, который Google делает атомарно (INTERNAL/TRANSIENT/DEADLINE).
- НИКОГДА не ретраим auth/permission/validation/неизвестный код (неизвестное на денежном
  пути = не повторять).
- Таймаут (`asyncio.timeout`) на ADS даёт TimeoutError и НЕ ретраится (запрос мог пройти).
  Финальный страж от double-spend — одноразовый `ConfirmStore.claim` (compare-and-set).
LLM read-only → таймаут можно повторять.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from core.logging import log

T = TypeVar("T")

# ── Параметры (читаются на момент вызова → тесты могут переопределить) ───────────
ADS_TIMEOUT_S: float = 60.0
ADS_MAX_ATTEMPTS: int = 4
ADS_WAIT_MULTIPLIER: float = 0.5
ADS_WAIT_MAX: float = 20.0

LLM_TIMEOUT_S: float = 45.0
LLM_MAX_ATTEMPTS: int = 3
LLM_WAIT_MULTIPLIER: float = 0.5
LLM_WAIT_MAX: float = 20.0

# ── Ограничение конкурентности к Google Ads ──────────────────────────────────────
# Глобальный потолок ОДНОВРЕМЕННЫХ вызовов к Google Ads (и read, и мутации, идущие через эти
# обёртки): защита от лавины запросов (rate-limit/таймауты под нагрузкой) и контроль денежного
# пути. Слот берётся ДО ретрай-цикла — повторы НЕ освобождают и не перезанимают слот, поэтому
# бюджет in-flight (включая ретраеров на backoff) ограничен ровно потолком.
# Семафор asyncio привязывается к event loop при первом использовании; pytest-asyncio даёт свой
# loop на каждый тест, а долгоживущий семафор из мёртвого loop бросил бы RuntimeError — поэтому
# пересоздаём его при смене running loop ИЛИ ёмкости (тест может переопределить ADS_MAX_CONCURRENCY).
ADS_MAX_CONCURRENCY: int = 4
_ads_sem: "asyncio.Semaphore | None" = None
_ads_sem_loop: "asyncio.AbstractEventLoop | None" = None
_ads_sem_capacity: int = 0


def _get_ads_semaphore() -> asyncio.Semaphore:
    """Семафор конкурентности к Google Ads для ТЕКУЩЕГО event loop (lazy, per-loop)."""
    global _ads_sem, _ads_sem_loop, _ads_sem_capacity
    loop = asyncio.get_running_loop()
    if _ads_sem is None or _ads_sem_loop is not loop or _ads_sem_capacity != ADS_MAX_CONCURRENCY:
        _ads_sem = asyncio.Semaphore(ADS_MAX_CONCURRENCY)
        _ads_sem_loop = loop
        _ads_sem_capacity = ADS_MAX_CONCURRENCY
    return _ads_sem


# Транзиентные коды Google Ads (сравнение по ИМЕНИ enum — версионно-безопасно, как в
# ads.mutations._apply_bid_via_sdk). Auth/permission/validation/неизвестное — НЕ ретраим.
RETRYABLE_ADS_NAMES: frozenset[str] = frozenset(
    {
        "RESOURCE_EXHAUSTED",
        "RATE_EXCEEDED",
        "RESOURCE_TEMPORARILY_EXHAUSTED",
        "INTERNAL_ERROR",
        "TRANSIENT_ERROR",
        "DEADLINE_EXCEEDED",
    }
)


def _ads_error_names(exc: object) -> set[str]:
    """Имена enum-кодов из GoogleAdsException (реальный protobuf или тест-дакт-фейк)."""
    names: set[str] = set()
    failure = getattr(exc, "failure", None)
    for err in getattr(failure, "errors", None) or []:
        code = getattr(err, "error_code", None)
        if code is None:
            continue
        which = getattr(code, "WhichOneof", None)  # реальный protobuf-oneof
        if callable(which):
            field = which("error_code")
            if field:
                nm = getattr(getattr(code, field, None), "name", None)
                if nm:
                    names.add(nm)
            continue
        nm = getattr(code, "name", None)  # дакт-фейк в тестах: error_code.name
        if nm:
            names.add(nm)
    return names


def _is_retryable_ads(exc: BaseException) -> bool:
    # Импорт внутри — google.ads тяжёлый; держим модуль дешёвым, если ADS-путь не задействован.
    try:
        from google.ads.googleads.errors import GoogleAdsException

        if isinstance(exc, GoogleAdsException):
            return bool(_ads_error_names(exc) & RETRYABLE_ADS_NAMES)
    except Exception:  # pragma: no cover - SDK всегда есть, но не падаем из-за импорта
        pass
    try:
        from google.api_core import exceptions as gapi
    except Exception:  # pragma: no cover
        return False
    return isinstance(
        exc,
        (
            gapi.ServiceUnavailable,
            gapi.DeadlineExceeded,
            gapi.InternalServerError,
            gapi.TooManyRequests,
        ),
    )


def _is_retryable_ads_read(exc: BaseException) -> bool:
    """Для ЧТЕНИЙ Google Ads (идемпотентны) ретраим всё, что и для мутаций, ПЛЮС TimeoutError —
    повторный read не «трогает деньги», поэтому таймаут безопасно повторить (в отличие от мутаций)."""
    return isinstance(exc, TimeoutError) or _is_retryable_ads(exc)


def _is_retryable_llm(exc: BaseException) -> bool:
    try:
        from openai import (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )
    except Exception:  # pragma: no cover
        return isinstance(exc, TimeoutError)
    # LLM read-only → таймаут безопасно повторять (в отличие от ADS-пути).
    return isinstance(
        exc,
        (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError, TimeoutError),
    )


async def run_ads_call(
    fn: Callable[..., T], *args: object, label: str | None = None, **kwargs: object
) -> T:
    """Замена `asyncio.to_thread(fn, *args)` для синхронных вызовов google-ads SDK:
    таймаут на попытку + ретрай транзиентных ошибок с backoff+jitter. Логирует запрос к
    Google Ads API (имя, длительность, исход — БЕЗ секретов; ТЗ §15). Сигнатура совместима
    с to_thread (call-site не меняется); `label` — опц. имя для лога."""
    name = label or getattr(fn, "__name__", "ads_call")

    async def _inner() -> T:
        async with asyncio.timeout(ADS_TIMEOUT_S):
            return await asyncio.to_thread(fn, *args, **kwargs)

    retryer: AsyncRetrying = AsyncRetrying(
        wait=wait_random_exponential(multiplier=ADS_WAIT_MULTIPLIER, max=ADS_WAIT_MAX),
        stop=stop_after_attempt(ADS_MAX_ATTEMPTS),
        retry=retry_if_exception(_is_retryable_ads),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    start = time.monotonic()
    try:
        async with _get_ads_semaphore():  # потолок одновременных вызовов к Google Ads
            result: T = await retryer(_inner)
    except Exception as e:
        log.warning("ads-call %s: %s за %dмс", name, type(e).__name__, _ms(start))
        raise
    log.info("ads-call %s: ok за %dмс", name, _ms(start))
    return result


async def run_ads_read_call(
    fn: Callable[..., T], *args: object, label: str | None = None, **kwargs: object
) -> T:
    """Как run_ads_call, но для ЧТЕНИЙ Google Ads (идемпотентны): таймаут на попытку + ретрай
    транзиентных ошибок И TimeoutError (read безопасно повторить). НЕ использовать для мутаций
    (на денежном пути таймаут не повторяем — см. run_ads_call). Логирует запрос (§15)."""
    name = label or getattr(fn, "__name__", "ads_read")

    async def _inner() -> T:
        async with asyncio.timeout(ADS_TIMEOUT_S):
            return await asyncio.to_thread(fn, *args, **kwargs)

    retryer: AsyncRetrying = AsyncRetrying(
        wait=wait_random_exponential(multiplier=ADS_WAIT_MULTIPLIER, max=ADS_WAIT_MAX),
        stop=stop_after_attempt(ADS_MAX_ATTEMPTS),
        retry=retry_if_exception(_is_retryable_ads_read),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    start = time.monotonic()
    try:
        async with _get_ads_semaphore():  # тот же потолок конкурентности, что и для мутаций
            result: T = await retryer(_inner)
    except Exception as e:
        log.warning("ads-read %s: %s за %dмс", name, type(e).__name__, _ms(start))
        raise
    log.info("ads-read %s: ok за %dмс", name, _ms(start))
    return result


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


async def call_llm(coro_factory: Callable[[], Awaitable[T]], *, label: str | None = None) -> T:
    """Вызов OpenRouter с таймаутом + ретраем (rate-limit/timeout/connection/5xx). Логирует
    запрос к LLM (метка модели/роли, длительность, исход — БЕЗ секретов; ТЗ §15).
    `coro_factory` — zero-arg фабрика свежего awaitable: tenacity создаёт корутину заново на
    каждую попытку (одну корутину нельзя await дважды)."""
    name = label or "llm"

    async def _inner() -> T:
        async with asyncio.timeout(LLM_TIMEOUT_S):
            return await coro_factory()

    retryer: AsyncRetrying = AsyncRetrying(
        wait=wait_random_exponential(multiplier=LLM_WAIT_MULTIPLIER, max=LLM_WAIT_MAX),
        stop=stop_after_attempt(LLM_MAX_ATTEMPTS),
        retry=retry_if_exception(_is_retryable_llm),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    start = time.monotonic()
    try:
        result: T = await retryer(_inner)
    except Exception as e:
        log.warning("llm-call %s: %s за %dмс", name, type(e).__name__, _ms(start))
        raise
    log.info("llm-call %s: ok за %dмс", name, _ms(start))
    return result
