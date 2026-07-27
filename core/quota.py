"""Трекинг дневной квоты операций Google Ads API (§3, защита денежного пути на масштабе).

Basic developer token = 15 000 операций/сутки (GAQL-запрос и каждая mutate-операция — операция).
На ~10 аккаунтах это исчерпывается реальнее, поэтому считаем операции в скользящем 24ч-окне и:
  • на 80% — предупреждаем (лог + пометка в плановом дайджесте);
  • на 95% — БЛОКИРУЕМ новые МУТАЦИИ (QuotaExceededError, fail-closed: без SDK-вызова, без трат).
ЧТЕНИЕ не блокируем НИКОГДА — оно нужно и для безопасности (показать «было», свериться).

Счётчик РАСПРЕДЕЛЁННЫЙ — строки таблицы ads_quota_ops (общий стор), а НЕ модульный deque. Причина:
bot, scheduler и per-session MCP — РАЗНЫЕ процессы; per-process deque даёт три слепых друг к другу
счётчика, и дневной потолок Basic пробивается молча. Для per-session MCP (шаг 5) общий стор —
единственное, что работает: процесс поднимается на сессию и умирает на закрытии stdio, in-process
счётчик стартует пустым каждый раз. Одна строка = один вызов record() (батч из 50 операций = строка
op_count=50). Скользящее окно считается в SQL: SUM(op_count) WHERE ts > now-24h; и запись, и граница
окна идут через db.session.db_dt() (SQLite — naive UTC, Postgres — tz-aware timestamptz), иначе
сравнение naive/tz-aware на SQLite молча даёт мусор.

Fail-safe (правило 10 — квота КУРЬЕРСКИЙ гард, НЕ авторизационная граница): все DB-операции идут под
коротким таймаутом (_DB_OP_TIMEOUT_S) и глотают ошибки. record() на завершённом чтении НЕ должен
задерживать/ронять сам read (safety-critical, незалочен) — лучше потерять учёт, чем блокировать
чтение на исчерпанном пуле (fire-and-forget через create_task НЕЛЬЗЯ: per-session MCP умрёт на
закрытии stdio, не сбросив INSERT). check_mutation_allowed при недоступном сторе РАЗРЕШАЕТ (fail-open)
— и это не дыра: настоящий денежный гейт ConfirmStore.claim (атомарный CAS) исполняется на ТОЙ ЖЕ БД и
ДО квоты, значит без БД мутации нет вовсе; авторитетный потолок — серверный лимит Google 15k.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from core.config import settings
from core.logging import log
from db.models import AdsQuotaOp
from db.session import Session, db_dt, engine

_WINDOW = timedelta(hours=24)  # скользящее окно 24 часа
_WARN_AT = 0.80  # порог предупреждения
_BLOCK_AT = 0.95  # порог блокировки новых мутаций
# Потолок ожидания стора: учёт квоты НИКОГДА не тормозит основной путь дольше этого. Здоровый
# индексированный INSERT/SELECT — единицы мс; таймаут срабатывает лишь при исчерпанном пуле/застрявшей
# БД, и тогда read-путь (незалоченный, safety-critical) должен завершиться, а не ждать соединение до 10с.
_DB_OP_TIMEOUT_S = 2.0

_warned = False  # чтобы лог 80% не спамил на каждом вызове после пересечения
_db_warned = False  # один WARNING на процесс, если стор недоступен (миграция 0031 не применена)


class QuotaExceededError(RuntimeError):
    """Дневной лимит операций почти исчерпан — новые МУТАЦИИ блокируются (fail-closed)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _limit() -> int:
    return int(settings.google_ads_daily_op_limit or 0)


async def _window_sum(account: str | None) -> int:
    """SUM(op_count) за скользящее 24ч-окно (глоб. или по аккаунту), НЕ ветвя по kind — точный паритет
    с историческим in-process счётчиком (read и mutate в одном total). Под таймаутом; любой сбой стора
    → 0 (fail-safe: недоучёт, не блок — см. модульный докстринг). Один WARNING на процесс."""
    global _db_warned
    cutoff = db_dt(_now() - _WINDOW)
    stmt = select(func.coalesce(func.sum(AdsQuotaOp.op_count), 0)).where(AdsQuotaOp.ts > cutoff)
    if account is not None:
        stmt = stmt.where(AdsQuotaOp.account == account)
    try:
        async with asyncio.timeout(_DB_OP_TIMEOUT_S):
            async with Session() as s:
                return int((await s.execute(stmt)).scalar_one())
    except Exception as e:  # noqa: BLE001 — стор недоступен/медлен → fail-safe (0, недоучёт)
        if not _db_warned:
            _db_warned = True
            log.warning(
                "quota: стор ads_quota_ops недоступен (%s) — счёт считаем нулевым (гард ослаблен, "
                "серверный лимит Google backstop). Применить миграцию 0031 (alembic upgrade head).",
                type(e).__name__,
            )
        return 0


async def record(account: str | None = None, *, kind: str = "read", count: int = 1) -> None:
    """Зафиксировать выполненные операции (read|mutate) строкой в общем сторе. `count` — фактическое
    число операций батча (Google тарифицирует КАЖДУЮ mutate-операцию, а не вызов: батч из 50 ключей =
    50). Чтение учитывается по вызовам (count=1) — число страниц заранее неизвестно; осознанный
    недоучёт read-пути (гард ранний, авторитетен серверный лимит). Fail-safe: под таймаутом, ошибки
    не роняют основной путь (safety-critical), но делаем ОДИН повтор (P3, аудит 2026-07-27): при
    транзиентном сбое БД/сети неучтённая операция может привести к квотному overshoot на следующем
    вызове check_mutation_allowed."""
    try:
        n = max(1, min(int(count), 100_000))  # кламп: защита от мусорного count
        row = AdsQuotaOp(ts=db_dt(_now()), account=account, kind=str(kind)[:8], op_count=n)
        async with asyncio.timeout(_DB_OP_TIMEOUT_S):
            async with Session() as s:
                s.add(row)
                await s.commit()
    except Exception:  # noqa: BLE001 — трекинг не должен задерживать/ронять основной путь
        try:
            log.warning("quota.record retry: пытаюсь повторный commit (kind=%s, count=%s)", kind, count)
            async with asyncio.timeout(_DB_OP_TIMEOUT_S):
                async with Session() as s:
                    s.add(row)
                    await s.commit()
        except Exception:  # noqa: BLE001 — не роняем основной путь ни при каких условиях
            log.warning("quota.record: повтор тоже не удался — квота не зафиксирована (kind=%s)", kind)


async def usage_pct(account: str | None = None) -> float:
    """Доля израсходованной дневной квоты (0..1+). account=None → глобально (что и есть лимит);
    с account — информативная доля этого аккаунта от общего лимита. limit=0 ⇒ 0 (трекинг выключен).
    Сбой стора → 0.0 (fail-safe → гейт разрешит; см. докстринг модуля)."""
    lim = _limit()
    if lim <= 0:
        return 0.0
    n = await _window_sum(account)
    return n / lim


async def check_mutation_allowed(account: str | None = None) -> None:
    """Гейт ПЕРЕД мутацией: если глобально израсходовано ≥95% дневной квоты — блок (QuotaExceededError,
    fail-closed, ДО SDK-вызова). Чтение этим не трогается. limit=0 ⇒ без гарда. На 80% — лог-предупр.
    Стор недоступен ⇒ pct=0 ⇒ пропуск (fail-OPEN — осознанно: настоящий денежный гейт claim идёт на
    ТОЙ ЖЕ БД раньше квоты, значит без БД мутации нет; см. модульный докстринг)."""
    global _warned
    lim = _limit()
    if lim <= 0:
        return
    pct = await usage_pct()
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


async def snapshot() -> dict:
    """Срез для /quota (без секретов): лимит, израсходовано, доли по аккаунтам за 24ч-окно. Fail-safe
    (сбой/медлительность стора → нули, под тем же таймаутом)."""
    lim = _limit()
    cutoff = db_dt(_now() - _WINDOW)
    total = 0
    per: dict[str, int] = {}
    try:
        async with asyncio.timeout(_DB_OP_TIMEOUT_S):
            async with Session() as s:
                total = int(
                    (
                        await s.execute(
                            select(func.coalesce(func.sum(AdsQuotaOp.op_count), 0)).where(
                                AdsQuotaOp.ts > cutoff
                            )
                        )
                    ).scalar_one()
                )
                rows = (
                    await s.execute(
                        select(AdsQuotaOp.account, func.sum(AdsQuotaOp.op_count))
                        .where(AdsQuotaOp.ts > cutoff, AdsQuotaOp.account.is_not(None))
                        .group_by(AdsQuotaOp.account)
                    )
                ).all()
                per = {a: int(n) for a, n in rows if n}
    except Exception:  # noqa: BLE001 — срез вспомогательный, сбой стора → нули
        total, per = 0, {}
    return {
        "limit": lim,
        "used": total,
        "pct": (total / lim) if lim > 0 else 0.0,
        "by_account": per,
        "window_hours": int(_WINDOW.total_seconds() // 3600),
    }


async def reset() -> None:
    """Полный сброс счётчика (для тестов): DELETE FROM ads_quota_ops + лог-флаги окна. ПРЕДОХРАНИТЕЛЬ:
    только на SQLite (dev/test). Раньше reset() чистил модульный deque и в проде был безвреден; теперь
    это безусловный DELETE — на Postgres он стёр бы реальную историю квоты (неверный DATABASE_URL на
    прод + случайный вызов из теста/хука). Гейт закрывает класс, а не полагается на дисциплину."""
    global _warned, _db_warned
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            "quota.reset() запрещён вне SQLite (dev/test): это DELETE FROM ads_quota_ops — "
            "защита от стирания прод-истории квоты"
        )
    async with Session() as s:
        await s.execute(sa_delete(AdsQuotaOp))
        await s.commit()
    _warned = False
    _db_warned = False
