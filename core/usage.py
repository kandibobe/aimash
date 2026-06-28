"""Учёт расхода LLM «с запуска процесса»: токены + РЕАЛЬНАЯ стоимость от OpenRouter.

Зачем: каждый ответ OpenRouter несёт `usage` с токенами и — особенность OpenRouter —
фактической стоимостью запроса (`usage.cost`, в кредитах = USD) и числом токенов, прочитанных
из кэша (`usage.prompt_tokens_details.cached_tokens`). Раньше этот объект выбрасывался
(`agent.router` брал только message). Теперь агрегируем по ролям — чтобы в боте (/balance)
показать: сколько и где потрачено и работает ли кэш-скидка.

Источник ИСТИНЫ по суммарным тратам/балансу аккаунта — сам OpenRouter
(`agent.openrouter_account`, эндпоинты /credits и /key): он переживает рестарты. Этот модуль —
лишь живая разбивка ТЕКУЩЕГО процесса (обнуляется при рестарте; в UI помечено «с запуска»).

Секретов здесь нет: токены/стоимость — числа, не креды. Ключ OpenRouter сюда не попадает.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class RoleUsage:
    """Накопитель по одной роли (parsing/copy/fallback) или итог."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0  # из prompt_tokens_details.cached_tokens (попадание в кэш-скидку)
    cost: float = 0.0  # сумма usage.cost (кредиты OpenRouter = USD)

    def add(self, *, prompt: int, completion: int, cached: int, cost: float) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cached_tokens += cached
        self.cost += cost


# Аккумулятор процесса. Лок — дёшево и корректно (запись может прийти из to_thread-потока).
_LOCK = threading.Lock()
_BY_ROLE: dict[str, RoleUsage] = {}


def _get(obj: Any, name: str, default: Any = 0) -> Any:
    """Достать поле из pydantic-объекта SDK / dict / model_extra (доп. поля OpenRouter: cost…).

    openai SDK типизирует prompt/completion/total, но cost и cached_tokens приходят как
    доп. поля — у pydantic v2 они в .model_extra. Терпимо к версии SDK и к dict-ответу."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        v = obj.get(name)
    else:
        v = getattr(obj, name, None)
        if v is None:
            extra = getattr(obj, "model_extra", None)
            if isinstance(extra, dict):
                v = extra.get(name)
    return default if v is None else v


def extract(usage: Any) -> dict[str, float]:
    """response.usage (SDK-объект / dict / None) → нормализованные числа.

    cost/cached_tokens лежат в доп-полях OpenRouter — читаем терпимо к версии."""
    prompt = int(_get(usage, "prompt_tokens", 0) or 0)
    completion = int(_get(usage, "completion_tokens", 0) or 0)
    cost = float(_get(usage, "cost", 0.0) or 0.0)
    details = _get(usage, "prompt_tokens_details", None)
    cached = int(_get(details, "cached_tokens", 0) or 0)
    return {"prompt": prompt, "completion": completion, "cached": cached, "cost": cost}


def record(role: str, usage: Any) -> None:
    """Учесть один вызов модели. Наблюдаемость НЕ должна ронять денежный путь — любую
    проблему разбора usage глотаем молча (вызывается из agent.router в обёртке try)."""
    try:
        v = extract(usage)
    except Exception:  # noqa: BLE001 — учёт затрат не критичен, не мешаем основному потоку
        return
    with _LOCK:
        _BY_ROLE.setdefault(role or "?", RoleUsage()).add(
            prompt=v["prompt"], completion=v["completion"], cached=v["cached"], cost=v["cost"]
        )


def snapshot() -> dict[str, Any]:
    """Срез «с запуска»: {'roles': {role: RoleUsage}, 'total': RoleUsage}. Для texts.fmt_balance."""
    with _LOCK:
        roles = {k: RoleUsage(**vars(v)) for k, v in _BY_ROLE.items()}  # копии, не ссылки
    total = RoleUsage()
    for u in roles.values():
        total.calls += u.calls
        total.prompt_tokens += u.prompt_tokens
        total.completion_tokens += u.completion_tokens
        total.cached_tokens += u.cached_tokens
        total.cost += u.cost
    return {"roles": roles, "total": total}


def reset() -> None:
    """Сброс накопителя (для тестов)."""
    with _LOCK:
        _BY_ROLE.clear()
