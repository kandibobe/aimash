"""Трекинг дневной квоты операций Google Ads API (§3, защита денежного пути на масштабе).

Basic developer token = 15 000 операций/сутки (GAQL-запрос и каждая mutate-операция — операция).
На ~10 аккаунтах это исчерпывается реальнее, поэтому считаем операции в скользящем 24ч-окне и:
  • на 80% — предупреждаем (лог + пометка в плановом дайджесте);
  • на 95% — БЛОКИРУЕМ новые МУТАЦИИ (QuotaExceededError, fail-closed: без SDK-вызова, без трат).
ЧТЕНИЕ не блокируем НИКОГДА — оно нужно и для безопасности (показать «было», свериться).

Счётчик IN-PROCESS (один инстанс бота; дубль даёт 409). Потокобезопасен (вызовы прилетают из
asyncio.to_thread-воркеров, как core.usage). Сбрасывается при рестарте — это ранний гард, не
расчётная палата; авторитетен серверный лимит Google. Для мульти-реплики нужен общий стор (TODO).

Fail-safe: внутренние ошибки трекинга НИКОГДА не роняют read/мутацию (квота — вспомогательная).
"""

from __future__ import annotations

import threading
import time
from collections import deque

from core.config import settings
from core.logging import log

_WINDOW_S = 24 * 3600  # скользящее окно 24 часа
_WARN_AT = 0.80  # порог предупреждения
_BLOCK_AT = 0.95  # порог блокировки новых мутаций

_lock = threading.Lock()
# Времена операций (monotonic-независимо: wall-clock time.time, чтобы окно было реальным «за сутки»).
_events: deque[float] = deque()
_by_account: dict[str, deque[float]] = {}
_warned = False  # чтобы лог 80% не спамил на каждом вызове после пересечения


class QuotaExceededError(RuntimeError):
    """Дневной лимит операций почти исчерпан — новые МУТАЦИИ блокируются (fail-closed)."""


def _prune(now: float) -> None:
    cutoff = now - _WINDOW_S
    while _events and _events[0] < cutoff:
        _events.popleft()
    for dq in _by_account.values():
        while dq and dq[0] < cutoff:
            dq.popleft()


def _limit() -> int:
    return int(settings.google_ads_daily_op_limit or 0)


def record(account: str | None = None, *, kind: str = "read") -> None:
    """Зафиксировать одну выполненную операцию (read|mutate). Fail-safe: ошибки глотаем."""
    try:
        with _lock:
            now = time.time()
            _events.append(now)
            if account:
                _by_account.setdefault(account, deque()).append(now)
            _prune(now)
    except Exception:  # noqa: BLE001 — трекинг не должен ронять основной путь
        pass


def usage_pct(account: str | None = None) -> float:
    """Доля израсходованной дневной квоты (0..1+). account=None → глобально (что и есть лимит);
    с account — информативная доля этого аккаунта от общего лимита. limit=0 ⇒ 0 (трекинг выключен)."""
    lim = _limit()
    if lim <= 0:
        return 0.0
    try:
        with _lock:
            now = time.time()
            _prune(now)
            if account is None:
                n = len(_events)
            else:
                n = len(_by_account.get(account, ()))
        return n / lim
    except Exception:  # noqa: BLE001
        return 0.0


def check_mutation_allowed(account: str | None = None) -> None:
    """Гейт ПЕРЕД мутацией: если глобально израсходовано ≥95% дневной квоты — блок (QuotaExceededError,
    fail-closed, ДО SDK-вызова). Чтение этим не трогается. limit=0 ⇒ без гарда. На 80% — лог-предупреждение."""
    global _warned
    lim = _limit()
    if lim <= 0:
        return
    pct = usage_pct()
    if pct >= _BLOCK_AT:
        raise QuotaExceededError(
            f"дневной лимит операций Google Ads почти исчерпан ({pct * 100:.0f}% из {lim}) — "
            "новые изменения временно заблокированы; чтение работает. Повтори позже."
        )
    if pct >= _WARN_AT and not _warned:
        _warned = True
        log.warning("quota: израсходовано %.0f%% дневного лимита операций (%d)", pct * 100, lim)
    elif pct < _WARN_AT:
        _warned = False  # окно «выдохнуло» — позволяем предупредить снова при следующем всплеске


def snapshot() -> dict:
    """Срез для /quota (без секретов): лимит, израсходовано, доли по аккаунтам. Fail-safe."""
    lim = _limit()
    try:
        with _lock:
            now = time.time()
            _prune(now)
            total = len(_events)
            per = {a: len(dq) for a, dq in _by_account.items() if dq}
    except Exception:  # noqa: BLE001
        total, per = 0, {}
    return {
        "limit": lim,
        "used": total,
        "pct": (total / lim) if lim > 0 else 0.0,
        "by_account": per,
        "window_hours": _WINDOW_S // 3600,
    }


def reset() -> None:
    """Полный сброс счётчиков (для тестов)."""
    global _warned
    with _lock:
        _events.clear()
        _by_account.clear()
        _warned = False
