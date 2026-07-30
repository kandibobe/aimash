"""C3: пер-юзер дневной потолок LLM-вызовов (анти-абуз / защита OpenRouter-бюджета).

До этого единственным тормозом на дорогие LLM-вызовы были message-throttle (0.7/с, bot.throttle) и
баланс OpenRouter (только показывается в /balance, НЕ enforced). Whitelisted (или скомпрометированный)
оператор мог нагенерить NL-команд и сжечь бюджет. Здесь считаем LLM-команды на chat_id в скользящем
24ч-окне и:
  • на 80% лимита — лог-предупреждение (не спамим — один раз на пересечение);
  • на 100% — ОТКАЗ (LLMBudgetExceededError, fail-closed: до вызова OpenRouter, трат нет).

Счётчик IN-PROCESS (один инстанс; дубль даёт 409 → B2 гард). Потокобезопасен. Сбрасывается при
рестарте — это ранний гард от абуза, не биллинг (авторитетен баланс OpenRouter). Fail-safe: внутренние
ошибки трекинга НИКОГДА не роняют обработку (лимит — вспомогательный). LLM_DAILY_CALLS_PER_USER=0 ⇒
гард ВЫКЛ (дефолт: не удивить владельца-оператора лимитом в разгар работы; включается конфигом).
"""

from __future__ import annotations

import threading
import time
from collections import deque

from core.config import settings
from core.logging import log

_WINDOW_S = 24 * 3600  # скользящее окно 24 часа
_WARN_AT = 0.80  # порог лог-предупреждения

_lock = threading.Lock()
_by_chat: dict[int, deque[float]] = {}  # chat_id → времена LLM-вызовов (wall-clock)
_warned: set[int] = set()  # чтобы warn-лог не спамил после пересечения 80%


class LLMBudgetError(RuntimeError):
    """Базовый бюджет-стоп LLM (счётный лимит или долларовый потолок). Это ОТВЕТ ПОЛЬЗОВАТЕЛЮ, не
    транзиентный сбой: точки «сбой LLM не критичен, есть fallback» обязаны его ПРОБРАСЫВАТЬ
    (`except LLMBudgetError: raise` ПЕРЕД общим `except Exception`) — иначе результат молча
    деградирует без слова о причине (досье «собралось» пустым, профиль — без полей)."""


class LLMBudgetExceededError(LLMBudgetError):
    """Пер-юзер дневной лимит LLM-вызовов исчерпан — новые запросы к ИИ блокируются (fail-closed)."""

    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(f"LLM daily limit reached ({used}/{limit})")


def _limit() -> int:
    return int(settings.llm_daily_calls_per_user or 0)


def _prune(now: float) -> None:
    cutoff = now - _WINDOW_S
    for dq in _by_chat.values():
        while dq and dq[0] < cutoff:
            dq.popleft()


def consume(chat_id: int | None) -> None:
    """Гейт ПЕРЕД LLM-командой: если chat_id за сутки исчерпал лимит — LLMBudgetExceededError
    (fail-closed, ДО вызова OpenRouter). Иначе фиксирует вызов. limit=0 или chat_id None ⇒ no-op
    (гард выключен). При блоке НЕ инкрементим (иначе окно не «выдохнет»). Fail-safe: внутренняя
    ошибка учёта не блокирует (пропускаем — лимит вспомогательный, не должен ронять рабочий путь)."""
    lim = _limit()
    if lim <= 0 or chat_id is None:
        return
    try:
        cid = int(chat_id)
        with _lock:
            now = time.time()
            _prune(now)
            dq = _by_chat.setdefault(cid, deque())
            used = len(dq)
            if used >= lim:
                raise LLMBudgetExceededError(used, lim)
            dq.append(now)
            pct = (used + 1) / lim
    except LLMBudgetExceededError:
        raise
    except Exception:  # noqa: BLE001 — сбой учёта не должен ронять команду (fail-safe, не fail-closed)
        return
    if pct >= _WARN_AT and cid not in _warned:
        _warned.add(cid)
        log.warning(
            "llm-budget: chat %s израсходовал %.0f%% дневного лимита LLM (%d)", cid, pct * 100, lim
        )
    elif pct < _WARN_AT:
        _warned.discard(cid)


_COST_WARN_AT = 0.80  # порог лог-предупреждения по дневной стоимости
_cost_warned = False  # чтобы warn по стоимости не спамил (сбрасывается при падении ниже порога)
# BZ-4: гейт стоит в agent/router.chat — на КАЖДОМ LLM-вызове. HTTP GET /key на каждый — плата
# латентностью и рейт-лимитом OpenRouter, поэтому прочитанная трата (и УСПЕХ, и НЕУДАЧА чтения —
# иначе каждый вызов при лежащем OpenRouter ждал бы таймаут GET) живёт _COST_CACHE_TTL_S секунд.
# Опоздание отсечки на минуту при потолке в целые доллары — приемлемо: backstop серверный (RB-3).
_COST_CACHE_TTL_S = 60.0
_cost_cache: tuple[float, float | None] | None = None  # (monotonic-время чтения, spent | None)


class LLMCostCapExceededError(LLMBudgetError):
    """Дневной потолок СТОИМОСТИ LLM (USD) достигнут — дорогие прогоны блокируются (fail-closed на
    известном превышении). Отдельно от LLMBudgetExceededError: тот про число вызовов пер-chat, этот
    про реальные деньги по всему спенд-пулу (включая автономный Hermes-цикл)."""

    def __init__(self, spent: float, cap: float) -> None:
        self.spent = spent
        self.cap = cap
        super().__init__(f"LLM daily cost cap reached (${spent:.2f}/${cap:.2f})")


def _cost_cap() -> float:
    return float(settings.llm_daily_cost_cap_usd or 0.0)


async def check_daily_cost_cap(*, client=None) -> None:
    """Гейт ПЕРЕД LLM-вызовом (BZ-4: живёт в agent/router.chat — единой точке всех наших вызовов,
    плюс ранний человекочитаемый отказ в bot/main._llm_budget_or_reply): если РЕАЛЬНАЯ дневная
    трата (OpenRouter /key usage_daily через core.or_activity) достигла потолка —
    LLMCostCapExceededError (fail-closed на ИЗВЕСТНОМ превышении). cap=0 ⇒ ВЫКЛ (no-op; в prod
    0 → 10 USD автодефолтом core.config._default_llm_cost_cap_in_prod). Трата кэшируется на
    _COST_CACHE_TTL_S — не по GET на вызов (см. комментарий у кэша).

    ⚠️ Трату прочитать НЕ удалось (OpenRouter недоступен) ⇒ ПРОПУСКАЕМ с warning — fail-OPEN, и это
    осознанно, НЕ нарушение правила 10: (1) это БЮДЖЕТНЫЙ рубеж, не security-гейт — денежные МУТАЦИИ
    всё равно за confirm/provenance; (2) жёсткий ЗАКРЫТЫЙ backstop — серверный limit_reset:daily на
    самом ключе OpenRouter (RB-3), он сработает у провайдера независимо от нас; (3) блокировать все
    прогоны из-за недоступности OpenRouter — худший отказ, чем допустить трату до серверного потолка.
    Импорт or_activity ленивый — чтобы core.llm_budget не тянул httpx, когда cap выключен."""
    cap = _cost_cap()
    if cap <= 0:
        return
    global _cost_warned, _cost_cache
    now = time.monotonic()
    if _cost_cache is not None and now - _cost_cache[0] < _COST_CACHE_TTL_S:
        spent = _cost_cache[1]
    else:
        from core import or_activity  # ленивый: без cap-а httpx не нужен

        spent = await or_activity.fetch_daily_cost_usd(client=client)
        _cost_cache = (now, spent)
        if spent is None:
            log.warning(
                "llm-budget: дневная трата OpenRouter недоступна — cost-cap пропущен (fail-open)"
            )
    if spent is None:
        return
    if spent >= cap:
        raise LLMCostCapExceededError(spent, cap)
    if spent >= cap * _COST_WARN_AT and not _cost_warned:
        _cost_warned = True
        log.warning(
            "llm-budget: дневная трата $%.2f ≥ %.0f%% потолка $%.2f",
            spent,
            _COST_WARN_AT * 100,
            cap,
        )
    elif spent < cap * _COST_WARN_AT:
        _cost_warned = False


async def daily_cost_status(*, client=None) -> dict:
    """Срез стоимости для диагностики/отчёта (без секретов): потолок, потрачено, доля. Не бросает —
    непрочитанная трата ⇒ spent=None (fail-soft), как в snapshot()."""
    cap = _cost_cap()
    spent: float | None = None
    if cap > 0:
        try:
            from core import or_activity

            spent = await or_activity.fetch_daily_cost_usd(client=client)
        except Exception:  # noqa: BLE001 — диагностика не роняет вызывающего
            spent = None
    pct = (spent / cap) if (cap > 0 and spent is not None) else 0.0
    return {"cap_usd": cap, "spent_usd": spent, "pct": pct}


def snapshot(chat_id: int) -> dict:
    """Срез для диагностики (без секретов): лимит и израсходовано для chat_id. Fail-safe."""
    lim = _limit()
    try:
        with _lock:
            now = time.time()
            _prune(now)
            used = len(_by_chat.get(int(chat_id), ()))
    except Exception:  # noqa: BLE001
        used = 0
    return {"limit": lim, "used": used, "pct": (used / lim) if lim > 0 else 0.0}


def reset() -> None:
    """Полный сброс счётчиков (для тестов)."""
    global _cost_warned, _cost_cache
    with _lock:
        _by_chat.clear()
        _warned.clear()
        _cost_warned = False
        _cost_cache = None
