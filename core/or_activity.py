"""#10 Наблюдаемость: чтение РЕАЛЬНЫХ трат OpenRouter — истина, переживающая рестарты, в отличие от
core.usage (живой срез процесса, сбрасывается) и core.quota (счётчик API-операций, не денег).

Зачем отдельно от agent.openrouter_account (тот питает /balance в боте): здесь фокус на spend-cap ниже
агента и на ЗАХВАТ трат автономного Hermes-цикла, который идёт мимо нашего процесса (config.yaml:70-71)
— core.usage их не видит вовсе. Два источника OpenRouter, с РАЗНОЙ семантикой (сверено с докой, не по
памяти):
  • GET /api/v1/key      — usage_daily/weekly/monthly + limit/limit_remaining/limit_reset. Обычный
                           inference-ключ, доступно всегда. usage_daily = потрачено ключом за ТЕКУЩИЕ
                           UTC-сутки (живое) → это и есть источник ЖИВОГО дневного cap (A.3).
  • GET /api/v1/activity — per-день/per-модель разбивка за 30 ЗАВЕРШЁННЫХ UTC-дней. Требует
                           MANAGEMENT-ключ (provisioning); обычный ключ → 403. Сегодняшний (неполный)
                           день туда НЕ входит — потому для живого cap не годится, только для
                           ИСТОРИЧЕСКОЙ сшивки в agent_runs (origin='hermes', A.4). Нет ключа ⇒ []
                           (fail-soft): зажигается, когда владелец заведёт provisioning-ключ (RB-3).

Наблюдаемость НЕ роняет денежный путь: любой сетевой/HTTP-сбой → None/[], а не исключение наверх.
Ключи берём только в точке вызова из settings.get_secret_value(), в лог/вывод не кладём (числа и
SHA-256-хэш ключа — не секрет). httpx гарантированно есть (транзитив openai SDK).
"""

from __future__ import annotations

import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from core.config import settings
from core.logging import log

_TIMEOUT = 15.0


@dataclass
class HermesKeyUsage:
    """Срез трат/лимита ОДНОГО ключа OpenRouter (GET /key). None = поле не отдано эндпоинтом."""

    usage_daily: float | None = None  # потрачено за ТЕКУЩИЕ UTC-сутки (живое)
    usage_weekly: float | None = None
    usage_monthly: float | None = None
    limit: float | None = None  # серверный потолок ключа (None = безлимит)
    limit_remaining: float | None = None
    limit_reset: str | None = None  # тип сброса лимита ('daily'/…) или None (не сбрасывается)


@dataclass
class ActivityRow:
    """Строка GET /activity: агрегат за ОДИН завершённый UTC-день по (модель, эндпоинт). usage = USD."""

    date: str  # YYYY-MM-DD (UTC)
    model: str
    provider_name: str | None
    usage: float  # стоимость (кредиты OpenRouter = USD)
    requests: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    byok_usage_inference: float | None


def _num(v: Any) -> float | None:
    """Терпимое приведение к float; null/строка/NaN/Inf → None (fail-closed, как в openrouter_account)."""
    try:
        f = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    return f if f is not None and math.isfinite(f) else None


def _int(v: Any) -> int:
    """Терпимое приведение к int; мусор → 0 (счётчики токенов/запросов, не деньги)."""
    n = _num(v)
    return int(n) if n is not None else 0


@asynccontextmanager
async def _ensure_client(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """Дать переданный клиент (тестовый seam с MockTransport) либо открыть свой на время вызова."""
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(timeout=_TIMEOUT) as own:
        yield own


async def _get_data(client: httpx.AsyncClient, path: str, key: str, **params: str) -> Any:
    """GET эндпоинта OpenRouter → распакованный `data` (dict или list), сырой ответ иначе."""
    r = await client.get(
        f"{settings.openrouter_base_url}{path}",
        headers={"Authorization": f"Bearer {key}"},
        params={k: v for k, v in params.items() if v},
    )
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


async def fetch_key_usage(*, client: httpx.AsyncClient | None = None) -> HermesKeyUsage | None:
    """Траты/лимит ключа этого процесса (GET /key, inference-ключ). None, если ключа нет или сбой.

    Ключ settings.openrouter_api_key — это ключ, которым ЭТОТ процесс платит за LLM. Если бот и Hermes
    делят один ключ (единый спенд-пул, рекомендуемо), usage_daily покрывает и Hermes; если ключи
    разные — /key видит ключ процесса, а изоляцию по ключу Hermes даёт только /activity+key_hash
    (завершённые дни). Наблюдаемость fail-soft: сбой → None, не исключение."""
    key = settings.openrouter_api_key.get_secret_value()
    if not key:
        return None
    try:
        async with _ensure_client(client) as c:
            d = await _get_data(c, "/key", key)
    except Exception as e:  # noqa: BLE001 — наблюдаемость не роняет денежный путь (правило 5/10)
        log.warning("or_activity: /key failed: %s", type(e).__name__)
        return None
    if not isinstance(d, dict):
        return None
    return HermesKeyUsage(
        usage_daily=_num(d.get("usage_daily")),
        usage_weekly=_num(d.get("usage_weekly")),
        usage_monthly=_num(d.get("usage_monthly")),
        limit=_num(d.get("limit")),
        limit_remaining=_num(d.get("limit_remaining")),
        limit_reset=(str(d["limit_reset"]) if d.get("limit_reset") is not None else None),
    )


async def fetch_daily_cost_usd(*, client: httpx.AsyncClient | None = None) -> float | None:
    """ЖИВАЯ трата за текущие UTC-сутки (USD) — источник дневного spend-cap (A.3). None = не удалось
    прочитать (fail-soft: тогда cap НЕ срабатывает, решение о поведении — на вызывающем гейте).

    Источник — /key usage_daily (единственное живое «сколько потрачено сегодня»; /activity сегодняшний
    неполный день не отдаёт). Это трата КЛЮЧА процесса; жёсткий per-key потолок — на самом ключе
    OpenRouter (limit_reset:daily, RB-3)."""
    usage = await fetch_key_usage(client=client)
    return usage.usage_daily if usage else None


async def fetch_activity(
    date: str | None = None, *, client: httpx.AsyncClient | None = None
) -> list[ActivityRow]:
    """Per-день/per-модель траты за 30 завершённых UTC-дней (GET /activity, MANAGEMENT-ключ). Для
    ИСТОРИЧЕСКОЙ сшивки в agent_runs (A.4), не для живого cap. Пустой список, если:
      • provisioning-ключ не задан (фича opt-in, ждёт RB-3) — тихо, это не ошибка;
      • 403/сетевой сбой — с warning-логом (тип, не текст: правило 5).

    date — фильтр по одной UTC-дате (YYYY-MM-DD, ≤30 дней). key_hash из настроек изолирует конкретный
    ключ (напр. Hermes) от остального аккаунта."""
    key = settings.openrouter_provisioning_key.get_secret_value()
    if not key:
        return []  # opt-in: без management-ключа /activity не зовём (fail-soft, не ошибка)
    try:
        async with _ensure_client(client) as c:
            data = await _get_data(
                c, "/activity", key, date=date or "", api_key_hash=settings.openrouter_key_hash
            )
    except Exception as e:  # noqa: BLE001 — 403 без mgmt-прав / сеть: наблюдаемость fail-soft
        log.warning("or_activity: /activity failed: %s", type(e).__name__)
        return []
    if not isinstance(data, list):
        return []
    rows: list[ActivityRow] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        d = item.get("date")
        model = item.get("model")
        if not d or not model:
            continue  # без даты/модели строка бесполезна для сшивки
        rows.append(
            ActivityRow(
                date=str(d),
                model=str(model),
                provider_name=(str(item["provider_name"]) if item.get("provider_name") else None),
                usage=_num(item.get("usage")) or 0.0,
                requests=_int(item.get("requests")),
                prompt_tokens=_int(item.get("prompt_tokens")),
                completion_tokens=_int(item.get("completion_tokens")),
                reasoning_tokens=_int(item.get("reasoning_tokens")),
                byok_usage_inference=_num(item.get("byok_usage_inference")),
            )
        )
    return rows
