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


async def run_ads_call(fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Замена `asyncio.to_thread(fn, *args)` для синхронных вызовов google-ads SDK:
    таймаут на попытку + ретрай транзиентных ошибок с backoff+jitter. Сигнатура совместима
    с to_thread — call-site меняется один-в-один."""

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
    return await retryer(_inner)


async def call_llm(coro_factory: Callable[[], Awaitable[T]]) -> T:
    """Вызов OpenRouter с таймаутом + ретраем (rate-limit/timeout/connection/5xx).
    `coro_factory` — zero-arg фабрика свежего awaitable: tenacity создаёт корутину заново на
    каждую попытку (одну корутину нельзя await дважды)."""

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
    return await retryer(_inner)
