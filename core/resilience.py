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

Circuit Breaker (PRO-R1, аудит 2026-07-27): при 5+ последовательных сбоях SDK-вызовов
цепь размыкается на 30с — все дальнейшие мутации мгновенно блокируются CircuitBreakerError.
Защита от каскадного сжигания одноразовых claim'ов при повальном сбое Google Ads API.
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

from core.config import settings as _settings
from core.logging import log

T = TypeVar("T")

# ── Параметры (читаются на момент вызова → тесты могут переопределить) ───────────
# 2.6: таймауты — из env (core.config: ADS_TIMEOUT_S/LLM_TIMEOUT_S); модульные имена сохранены
# (тесты/код переопределяют их напрямую, живое значение читается на момент вызова).
ADS_TIMEOUT_S: float = float(_settings.ads_timeout_s)
ADS_MAX_ATTEMPTS: int = 4
ADS_WAIT_MULTIPLIER: float = 0.5
ADS_WAIT_MAX: float = 20.0

LLM_TIMEOUT_S: float = float(_settings.llm_timeout_s)
LLM_MAX_ATTEMPTS: int = 3
LLM_WAIT_MULTIPLIER: float = 0.5
LLM_WAIT_MAX: float = 20.0

# ── Circuit Breaker (PRO-R1, аудит 2026-07-27) ──────────────────────────────────
# 5+ последовательных сбоев ЛЮБОГО SDK-вызова мутации → размыкаем цепь на 30 секунд.
# Все последующие мутации мгновенно получают CircuitBreakerError, не тратя claim.
# Первый успех сбрасывает счётчик.
_CB_THRESHOLD: int = 5
_CB_COOLDOWN_S: float = 30.0
_cb_failures: int = 0
_cb_open_until: float = 0.0


class CircuitBreakerError(RuntimeError):
    """Автоматическая блокировка мутаций из-за каскадных сбоев Google Ads API."""


def _circuit_breaker_observe(ok: bool) -> None:
    """Уведомить circuit breaker об исходе одного SDK-вызова. Успех → сброс."""
    global _cb_failures, _cb_open_until
    if ok:
        _cb_failures = 0
        _cb_open_until = 0.0
        return
    _cb_failures += 1
    if _cb_failures >= _CB_THRESHOLD:
        _cb_open_until = time.monotonic() + _CB_COOLDOWN_S
        log.warning(
            "circuit-breaker: %d последовательных сбоев — цепь разомкнута на %.0fс",
            _cb_failures, _CB_COOLDOWN_S,
        )


def _circuit_breaker_check() -> None:
    """Проверить цепь перед SDK-вызовом мутации. Бросает CircuitBreakerError если разомкнута."""
    if time.monotonic() < _cb_open_until:
        remaining = _cb_open_until - time.monotonic()
        raise CircuitBreakerError(
            f"автоматическая блокировка мутаций: {remaining:.0f}с ожидания до "
            f"восстановления (каскад сбоев Google Ads API)"
        )


def circuit_breaker_reset() -> None:
    """Принудительный сброс цепи (административная команда / тесты)."""
    global _cb_failures, _cb_open_until
    _cb_failures = 0
    _cb_open_until = 0.0


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
# Полный набор — для ЧТЕНИЙ (идемпотентны, повтор безопасен).
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

# 2.9 (аудит 2026-07-06): у МУТАЦИЙ набор УЖЕ. Серверные INTERNAL_ERROR/DEADLINE_EXCEEDED значат
# «исход неизвестен — запрос МОГ примениться на сервере»: авто-повтор неидемпотентного батча
# (add_keywords и т.п.) рискует дублями (раньше спасал только partial_failure-дедуп Google).
# Оставляем коды «запрос точно НЕ выполнен»: rate-limit/квота/явный TRANSIENT.
RETRYABLE_ADS_MUTATE_NAMES: frozenset[str] = frozenset(
    {
        "RESOURCE_EXHAUSTED",
        "RATE_EXCEEDED",
        "RESOURCE_TEMPORARILY_EXHAUSTED",
        "TRANSIENT_ERROR",
    }
)


def _retryable_by_names(exc: BaseException, names: frozenset[str], *, server_5xx: bool) -> bool:
    # Импорт внутри — google.ads тяжёлый; держим модуль дешёвым, если ADS-путь не задействован.
    from core.ads_errors import error_code_names

    try:
        from google.ads.googleads.errors import GoogleAdsException

        if isinstance(exc, GoogleAdsException):
            return bool(error_code_names(exc) & names)
    except Exception:  # pragma: no cover
        pass
    try:
        from google.api_core import exceptions as gapi
    except Exception:  # pragma: no cover
        return False
    transport: tuple = (gapi.ServiceUnavailable, gapi.TooManyRequests)
    if server_5xx:
        transport = (*transport, gapi.DeadlineExceeded, gapi.InternalServerError)
    return isinstance(exc, transport)


def _is_retryable_ads(exc: BaseException) -> bool:
    """Предикат ретрая МУТАЦИЙ (2.9): БЕЗ серверных INTERNAL/DEADLINE."""
    return _retryable_by_names(exc, RETRYABLE_ADS_MUTATE_NAMES, server_5xx=False)


def _is_retryable_ads_read(exc: BaseException) -> bool:
    """Для ЧТЕНИЙ Google Ads (идемпотентны) ретраем ПОЛНЫЙ набор (вкл. INTERNAL/DEADLINE)."""
    return isinstance(exc, TimeoutError) or _retryable_by_names(
        exc, RETRYABLE_ADS_NAMES, server_5xx=True
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
    return isinstance(
        exc,
        (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError, TimeoutError),
    )


async def run_ads_call(
    fn: Callable[..., T],
    *args: object,
    label: str | None = None,
    account: str | None = None,
    op_count: int = 1,
    **kwargs: object,
) -> T:
    """Замена `asyncio.to_thread(fn, *args)` для мутаций:
    circuit-breaker check → квота → семафор → ретрай транзиентных ошибок → observe+record."""
    from core import quota

    name = label or getattr(fn, "__name__", "ads_call")

    # PRO-R1: проверяем цепь ДО квоты — не тратим ресурсы на заведомо блокированный вызов.
    _circuit_breaker_check()

    await quota.check_mutation_allowed(account)

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
        async with _get_ads_semaphore():
            result: T = await retryer(_inner)
    except Exception as e:
        log.warning("ads-call %s: %s за %dмс", name, _fail_cause(e), _ms(start))
        _observe_ads(name, start, "ads_mutate", ok=False)
        _circuit_breaker_observe(False)  # PRO-R1: сбой учтён
        raise
    await quota.record(account, kind="mutate", count=op_count)
    log.info("ads-call %s: ok за %dмс", name, _ms(start))
    _observe_ads(name, start, "ads_mutate", ok=True)
    _circuit_breaker_observe(True)  # PRO-R1: успех сбрасывает счётчик
    return result


async def run_ads_create_call(
    fn: Callable[..., T],
    *args: object,
    label: str | None = None,
    account: str | None = None,
    op_count: int = 1,
    **kwargs: object,
) -> T:
    """Как run_ads_call, но БЕЗ РЕТРАЕВ. Circuit-breaker проверка та же."""
    from core import quota

    name = label or getattr(fn, "__name__", "ads_create")

    _circuit_breaker_check()  # PRO-R1

    await quota.check_mutation_allowed(account)

    start = time.monotonic()
    try:
        async with _get_ads_semaphore():
            async with asyncio.timeout(ADS_TIMEOUT_S):
                result: T = await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as e:
        log.warning("ads-create %s: %s за %dмс", name, _fail_cause(e), _ms(start))
        _observe_ads(name, start, "ads_mutate", ok=False)
        _circuit_breaker_observe(False)
        raise
    await quota.record(account, kind="mutate", count=op_count)
    log.info("ads-create %s: ok за %dмс", name, _ms(start))
    _observe_ads(name, start, "ads_mutate", ok=True)
    _circuit_breaker_observe(True)
    return result


async def run_ads_read_call(
    fn: Callable[..., T],
    *args: object,
    label: str | None = None,
    account: str | None = None,
    **kwargs: object,
) -> T:
    """ЧТЕНИЯ — ретраем TimeoutError + полный транзиентный набор. Circuit-breaker не триггерим (чтения не тратят claim)."""
    from core import quota

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
        async with _get_ads_semaphore():
            result: T = await retryer(_inner)
    except Exception as e:
        log.warning("ads-read %s: %s за %dмс", name, _fail_cause(e), _ms(start))
        _observe_ads(name, start, "ads_read", ok=False)
        raise
    await quota.record(account, kind="read")
    log.info("ads-read %s: ok за %dмс", name, _ms(start))
    _observe_ads(name, start, "ads_read", ok=True)
    return result


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _observe_ads(name: str, start: float, kind: str, *, ok: bool) -> None:
    try:
        from core import observe

        observe.record_event(kind, tool_name=name, latency_ms=_ms(start), ok=ok)
    except Exception:  # noqa: BLE001
        pass


def _fail_cause(exc: BaseException) -> str:
    from core.ads_errors import error_code_names

    names = error_code_names(exc)
    if names:
        return ",".join(sorted(names))
    return type(exc).__name__


async def call_llm(coro_factory: Callable[[], Awaitable[T]], *, label: str | None = None) -> T:
    """Вызов OpenRouter с таймаутом + ретраем."""
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