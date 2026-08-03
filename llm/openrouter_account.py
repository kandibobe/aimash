"""Чтение баланса и трат аккаунта OpenRouter (для команды /balance в боте).

Два REST-эндпоинта (НЕ часть chat-схемы → через httpx, а не openai SDK):
- GET /api/v1/credits → {data:{total_credits, total_usage}} — суммарно по аккаунту. По докам
  может требовать management-ключ (не обычный inference); при 401/403 — просто пропускаем.
- GET /api/v1/key     → {data:{usage, usage_daily, usage_weekly, usage_monthly, limit,
  limit_remaining, is_free_tier, …}} — работает с обычным inference-ключом; usage = потрачено
  этим ключом за всё время, usage_daily/weekly/monthly — срезы по периодам (day сбрасывается
  ежедневно), они «живее» суммарной траты и в /balance показываются как «актуально».

Это ИСТИНА по тратам (переживает рестарты), в отличие от core.usage (живая разбивка процесса).
Ключ берётся только из settings.get_secret_value() в точке вызова и в вывод/логи не попадает
(числа — не секрет). httpx гарантированно есть (транзитивная зависимость openai SDK).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.config import settings

_TIMEOUT = 15.0


@dataclass
class AccountStatus:
    """Снимок состояния счёта OpenRouter. None = поле не удалось получить (эндпоинт недоступен)."""

    total_credits: float | None = None  # /credits: всего куплено
    total_usage: float | None = None  # /credits: всего потрачено по аккаунту
    key_usage: float | None = None  # /key: потрачено этим API-ключом (за всё время)
    key_limit: float | None = None  # /key: лимит ключа (None = безлимит)
    limit_remaining: float | None = None  # /key: остаток лимита ключа
    usage_daily: float | None = None  # /key: потрачено за сутки (сбрасывается ежедневно)
    usage_weekly: float | None = None  # /key: потрачено за неделю
    usage_monthly: float | None = None  # /key: потрачено за месяц
    is_free_tier: bool | None = None  # /key: бесплатный тариф
    errors: list[str] = field(default_factory=list)  # какие эндпоинты не ответили (для диагностики)

    @property
    def balance(self) -> float | None:
        """Остаток средств = куплено − потрачено (по /credits). None, если /credits недоступен."""
        if self.total_credits is None or self.total_usage is None:
            return None
        return self.total_credits - self.total_usage


def _num(v: Any) -> float | None:
    """Терпимое приведение к float (поле может быть null/строкой/отсутствовать).

    Нечисловые литералы JSON (NaN/Infinity — stdlib json их парсит) отбрасываем в None:
    иначе бюджет показал бы «$nan»/«$inf». Fail-closed — как и при отсутствии поля."""
    try:
        f = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    return f if f is not None and math.isfinite(f) else None


async def _get_data(client: httpx.AsyncClient, path: str, key: str) -> dict[str, Any]:
    """GET эндпоинта OpenRouter → распакованный data-объект (или сам ответ, если без обёртки)."""
    r = await client.get(
        f"{settings.openrouter_base_url}{path}",
        headers={"Authorization": f"Bearer {key}"},
    )
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict):
        data = payload.get("data")
        return data if isinstance(data, dict) else payload
    return {}


async def fetch_account() -> AccountStatus:
    """Прочитать баланс/траты со счёта OpenRouter. Частичный отказ НЕ роняет всё: /key и /credits
    запрашиваются независимо, недоступный эндпоинт лишь оставляет свои поля None + строку в errors."""
    key = settings.openrouter_api_key.get_secret_value()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY не задан в .env")

    st = AccountStatus()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # /key — основной путь (обычный inference-ключ): траты ключа + остаток лимита.
        try:
            d = await _get_data(client, "/key", key)
            st.key_usage = _num(d.get("usage"))
            st.key_limit = _num(d.get("limit"))
            st.limit_remaining = _num(d.get("limit_remaining"))
            # Живые срезы по периодам — растут с каждым запросом (day сбрасывается в сутки),
            # потому ДВИГАЮТСЯ заметнее суммарной траты: именно их показываем как «актуально».
            st.usage_daily = _num(d.get("usage_daily"))
            st.usage_weekly = _num(d.get("usage_weekly"))
            st.usage_monthly = _num(d.get("usage_monthly"))
            ft = d.get("is_free_tier")
            st.is_free_tier = bool(ft) if ft is not None else None
        except Exception as e:  # noqa: BLE001 — частичный отказ допустим, отражаем в errors
            st.errors.append(f"/key: {type(e).__name__}")
        # /credits — баланс по аккаунту; может требовать management-ключ (тогда 401/403 → None).
        try:
            d = await _get_data(client, "/credits", key)
            st.total_credits = _num(d.get("total_credits"))
            st.total_usage = _num(d.get("total_usage"))
        except Exception as e:  # noqa: BLE001
            st.errors.append(f"/credits: {type(e).__name__}")
    return st
