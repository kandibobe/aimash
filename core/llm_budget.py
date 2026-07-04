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


class LLMBudgetExceededError(RuntimeError):
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
    with _lock:
        _by_chat.clear()
        _warned.clear()
