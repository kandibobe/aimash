"""Распределённый размыкатель (circuit breaker) с арендой пробы — Волна 2.

ЧЕГО он НЕ делает: джиттер. `core.resilience` уже ретраит с `wait_random_exponential` (AWS «Full
Jitter») во всех трёх политиках — строить нечего. Но джиттер разносит попытки ВНУТРИ одного вызова,
а thundering herd делают РАЗНЫЕ вызывающие: bot, scheduler и per-session MCP — три процесса, каждый
по сотне аккаунтов. После сбоя Google все они всё равно пойдут пробовать, просто вразнобой.

ЧТО он делает: после N подряд отказов «дальняя сторона недоступна» цепь размыкается — вызовы
отсекаются БЕЗ похода в сеть. По истечении окна остывания право на ОДИН пробный запрос берётся
атомарным UPDATE по `probe_lease_until` (`rowcount == 1` ⇒ ты единственный пробник — тот же приём,
что `ConfirmStore.claim`). Проба прошла — цепь замкнута для всех; проба упала — окно остывания
отсчитывается заново. Состояние общее (таблица `circuit_state`), иначе три процесса пробуют втроём.

Что считается отказом (`counts_as_failure`) — узко и намеренно: ТОЛЬКО «сервис недоступен или душит
нас» (транспортные 503/429, квотные RESOURCE_EXHAUSTED/RATE_EXCEEDED, TRANSIENT_ERROR; для LLM —
rate-limit/connection/5xx). НЕ считаются:
  • `is_account_access_error` — аккаунт деактивирован/нет прав. Это состояние АККАУНТА; размыкать
    цепь по нему значит запереть исправный API из-за одного отключённого клиента.
  • `_OUTCOME_UNKNOWN_CODES` (INTERNAL_ERROR/DEADLINE_EXCEEDED) и голый `TimeoutError` — исход
    НЕИЗВЕСТЕН (мутация могла примениться), и наш собственный дедлайн `asyncio.timeout` вообще не
    свидетельство о состоянии Google. Такой отказ уже обработан честнее — `needs_review`.
  • валидация/авторизация/неизвестный код — это НАШ дефект. Размыкатель бы его замаскировал и
    вдобавок заблокировал здоровый трафик.

⚠️ ОСОЗНАННОЕ ИСКЛЮЧЕНИЕ ИЗ ПРАВИЛА 10 (fail-closed везде): сбой самого стора размыкателя →
fail-OPEN, вызов проходит. Размыкатель — про доступность, а не про безопасность; отказав закрыто, он
сам стал бы аварией (не применилась миграция 0033 ⇒ встал весь Google Ads). Ни одного гейта
безопасности он не заменяет: замок аккаунта, confirm-гейт, провенанс и квота стоят отдельно и
fail-closed каждый. Прецедент в коде — `core.quota`. Записано в docs/SECURITY.md явным исключением.

Пороги — модульные константы (как ADS_MAX_ATTEMPTS в `core.resilience`), читаются на момент вызова:
тест переопределяет их напрямую. Env-ключей нет намеренно — новый ключ Settings потребовал бы правки
`.env.example`, а порог размыкателя не операционная настройка, а свойство протокола.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

from sqlalchemy import case, delete as sa_delete, select, update
from sqlalchemy.engine import CursorResult

from core.logging import log
from db.models import CircuitState
from db.session import Session, db_dt, engine

# Сколько ПОДРЯД отказов «дальняя сторона недоступна» размыкают цепь. Считаются вызовы (после
# исчерпания ретраев), не попытки: ретраи внутри вызова — уже отработанный джиттером всплеск.
FAILURE_THRESHOLD: int = 5
# Сколько цепь остаётся разомкнутой до первой пробы. Отсчёт от opened_at; неудачная проба его сдвигает.
OPEN_COOLDOWN_S: float = 60.0
# Аренда пробы. Это АРЕНДА, а не блокировка: пробник, умерший вместе с процессом, задерживает
# восстановление ровно на этот срок, а не навсегда. Должна покрывать пробный вызов целиком
# (ADS_TIMEOUT_S=60 × ретраи), иначе второй процесс начнёт пробовать поверх ещё живой пробы.
PROBE_LEASE_S: float = 180.0
# Потолок ожидания стора — как в core.quota: размыкатель НИКОГДА не тормозит основной путь дольше.
_DB_OP_TIMEOUT_S = 2.0

_db_warned = False  # один WARNING на процесс, если стор недоступен (миграция 0033 не применена)


class CircuitOpenError(RuntimeError):
    """Цепь разомкнута — вызов отсечён БЕЗ похода в сеть (окно остывания не истекло либо пробу уже
    взял другой процесс). Не путать с `clients.crawl_fetch.CircuitOpen` — тот локальный, для краула."""


def circuit_name(kind: str, key: str | None) -> str:
    """Имя цепи: `ads:<customer_id>` / `llm:<model_slug>`. Пустой ключ ⇒ `-` (общая цепь вида).

    Гранулярность пер-аккаунт (а не одна цепь на весь Google Ads) сознательна: аккаунт, стабильно
    упирающийся в свой лимит, не должен запирать остальные 16. Обрезка до длины колонки — чтобы
    длинный слаг модели не ронял INSERT."""
    return f"{kind}:{key or '-'}"[:96]


# Коды Google Ads «сервис недоступен/душит нас». Строгое подмножество RETRYABLE_ADS_MUTATE_NAMES:
# ретрай и размыкание отвечают на разные вопросы, но оба исключают «исход неизвестен».
_OUTAGE_ADS_NAMES: frozenset[str] = frozenset(
    {
        "RESOURCE_EXHAUSTED",
        "RATE_EXCEEDED",
        "RESOURCE_TEMPORARILY_EXHAUSTED",
        "TRANSIENT_ERROR",
    }
)


def counts_as_failure(exc: BaseException) -> bool:
    """Считать ли отказ свидетельством «дальняя сторона недоступна» (см. докстринг модуля).

    Порядок веток значим: «исход неизвестен» отсеивается ДО всего остального, иначе транспортная
    ветка `google.api_core` подхватила бы DeadlineExceeded как «сервис лежит». Классификатор берём
    готовый — `is_outcome_unknown_after_mutate` (TimeoutError + INTERNAL_ERROR/DEADLINE_EXCEEDED +
    транспортные 500/deadline); имя у него про мутации, но множество ровно то, что нужно и здесь."""
    from core.ads_errors import (
        error_code_names,
        is_account_access_error,
        is_outcome_unknown_after_mutate,
    )

    if is_outcome_unknown_after_mutate(exc):  # наш дедлайн / «могло примениться» — не свидетельство
        return False
    if is_account_access_error(exc):  # состояние аккаунта, а не сервиса
        return False
    names = error_code_names(exc)
    if names:  # это GoogleAdsException с разобранными кодами — решаем по ним и не идём дальше
        return bool(names & _OUTAGE_ADS_NAMES)
    try:
        from google.api_core import exceptions as gapi

        # ТОЛЬКО 503/429. InternalServerError(500)/DeadlineExceeded отсеяны веткой выше.
        if isinstance(exc, (gapi.ServiceUnavailable, gapi.TooManyRequests)):
            return True
    except Exception:  # pragma: no cover — SDK всегда есть, но не падаем из-за импорта
        pass
    try:
        from openai import APIConnectionError, InternalServerError, RateLimitError
    except Exception:  # pragma: no cover
        return False
    # APITimeoutError — подкласс APIConnectionError: таймаут HTTP-клиента к провайдеру И ЕСТЬ
    # «дальняя сторона не отвечает» (в отличие от нашего asyncio.timeout выше по стеку).
    return isinstance(exc, (RateLimitError, APIConnectionError, InternalServerError))


@dataclass(frozen=True, slots=True)
class _Ticket:
    """Что `_check` узнал о цепи. `clean` — строки нет или она «closed & failure_count == 0», значит
    успех НИЧЕГО не меняет и писать в БД не нужно (здоровая тропа = один SELECT на вызов, без UPDATE).
    `probe` — взята аренда пробы."""

    name: str
    clean: bool
    probe: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _check(name: str) -> _Ticket:
    """Пустить ли вызов. Разомкнутая цепь без свободной пробы ⇒ CircuitOpenError. Сбой стора ⇒
    пропуск (fail-OPEN, см. докстринг модуля)."""
    global _db_warned
    try:
        async with asyncio.timeout(_DB_OP_TIMEOUT_S):
            async with Session() as s:
                row = (
                    await s.execute(select(CircuitState).where(CircuitState.name == name))
                ).scalar_one_or_none()
                if row is None:
                    return _Ticket(name, clean=True, probe=False)
                if row.state == "closed":
                    return _Ticket(name, clean=row.failure_count == 0, probe=False)
                now = _now()
                # Аренда пробы ОДНИМ UPDATE: условия — окно остывания истекло И аренда свободна.
                # Проверять условия отдельным SELECT нельзя: между чтением и записью пробу возьмут
                # все, кто читал одновременно, и half-open превратится в тот же herd.
                res = await s.execute(
                    update(CircuitState)
                    .where(
                        CircuitState.name == name,
                        CircuitState.state.in_(("open", "half_open")),
                        # `IS NULL` — страховка от навсегда застрявшей цепи: opened_at всегда
                        # пишется вместе с state='open', но NULL <= x даёт NULL (не TRUE), и
                        # рассинхрон запер бы API навсегда — ровно та авария, от которой размыкатель.
                        (CircuitState.opened_at.is_(None))
                        | (
                            CircuitState.opened_at
                            <= db_dt(now - timedelta(seconds=OPEN_COOLDOWN_S))
                        ),
                        (CircuitState.probe_lease_until.is_(None))
                        | (CircuitState.probe_lease_until <= db_dt(now)),
                    )
                    .values(
                        state="half_open",
                        probe_lease_until=db_dt(now + timedelta(seconds=PROBE_LEASE_S)),
                        updated_at=db_dt(now),
                    )
                )
                if cast(CursorResult, res).rowcount == 1:
                    await s.commit()
                    return _Ticket(name, clean=False, probe=True)
                await s.rollback()  # аренду взял кто-то другой / окно не истекло → отказ ниже
    except Exception as e:  # noqa: BLE001 — стор недоступен/медлен → fail-OPEN (доступность)
        _warn_store(e)
        return _Ticket(name, clean=True, probe=False)
    raise CircuitOpenError(
        f"канал {name} временно отключён после серии отказов — повтори через "
        f"{int(OPEN_COOLDOWN_S)}с (пробный запрос уже идёт или окно остывания не истекло)"
    )


async def _on_success(t: _Ticket) -> None:
    """Успех замыкает цепь и обнуляет счётчик. На здоровой тропе (`clean`) не пишет вовсе."""
    if t.clean:
        return
    try:
        async with asyncio.timeout(_DB_OP_TIMEOUT_S):
            async with Session() as s:
                await s.execute(
                    update(CircuitState)
                    .where(CircuitState.name == t.name)
                    .values(
                        state="closed",
                        failure_count=0,
                        opened_at=None,
                        probe_lease_until=None,
                        updated_at=db_dt(_now()),
                    )
                )
                await s.commit()
    except Exception as e:  # noqa: BLE001 — учёт не смеет ронять успешно завершённый вызов
        _warn_store(e)


async def _on_failure(t: _Ticket, exc: BaseException) -> None:
    """Отказ, если он свидетельствует о недоступности, инкрементит счётчик; на пороге — размыкает.

    Инкремент и решение о размыкании — ОДНИМ UPDATE (`failure_count = failure_count + 1` + CASE):
    read-modify-write в питоне два процесса выполнили бы поверх друг друга и порог сдвинулся бы вдвое.
    Неудачная проба размыкает сразу: в half-open счётчик уже на пороге, CASE срабатывает."""
    if not counts_as_failure(exc):
        return
    now = db_dt(_now())
    nxt = CircuitState.failure_count + 1
    try:
        async with asyncio.timeout(_DB_OP_TIMEOUT_S):
            async with Session() as s:
                res = await s.execute(
                    update(CircuitState)
                    .where(CircuitState.name == t.name)
                    .values(
                        failure_count=nxt,
                        state=case((nxt >= FAILURE_THRESHOLD, "open"), else_=CircuitState.state),
                        opened_at=case(
                            (nxt >= FAILURE_THRESHOLD, now), else_=CircuitState.opened_at
                        ),
                        probe_lease_until=None,  # проба (если это была она) закончена
                        updated_at=now,
                    )
                )
                if (
                    cast(CursorResult, res).rowcount == 0
                ):  # первая ошибка этой цепи — заводим строку
                    s.add(
                        CircuitState(
                            name=t.name,
                            state="open" if FAILURE_THRESHOLD <= 1 else "closed",
                            failure_count=1,
                            opened_at=now if FAILURE_THRESHOLD <= 1 else None,
                            updated_at=now,
                        )
                    )
                await s.commit()
    except Exception as e:  # noqa: BLE001 — гонка INSERT/недоступный стор → отказ не учтён, не блок
        _warn_store(e)


@asynccontextmanager
async def guard(name: str):
    """Обернуть вызов размыкателем: `async with breaker.guard(circuit_name("ads", cid)): ...`.

    Ставится ВОКРУГ ретрай-цикла, а не внутри: один вызов = одно свидетельство. Исключение наружу
    проходит как есть — размыкатель ничего не глотает и ничего не подменяет."""
    t = await _check(name)
    try:
        yield
    except BaseException as e:
        await _on_failure(t, e)
        raise
    await _on_success(t)


def _warn_store(e: BaseException) -> None:
    """Один WARNING на процесс: без стора размыкатель молча выключен, и знать об этом надо."""
    global _db_warned
    if _db_warned:
        return
    _db_warned = True
    log.warning(
        "breaker: стор circuit_state недоступен (%s) — размыкатель выключен (fail-open, вызовы "
        "проходят). Применить миграцию 0033 (alembic upgrade head).",
        type(e).__name__,
    )


async def snapshot() -> list[dict]:
    """Срез состояния цепей (для /diag; без секретов). Сбой стора → []."""
    try:
        async with asyncio.timeout(_DB_OP_TIMEOUT_S):
            async with Session() as s:
                rows = (await s.execute(select(CircuitState))).scalars().all()
        return [
            {
                "name": r.name,
                "state": r.state,
                "failure_count": r.failure_count,
                "opened_at": r.opened_at.isoformat() if r.opened_at else None,
            }
            for r in rows
        ]
    except Exception:  # noqa: BLE001 — срез вспомогательный
        return []


async def reset() -> None:
    """Полный сброс цепей (для тестов). ПРЕДОХРАНИТЕЛЬ — только SQLite (dev/test): на Postgres это
    стёрло бы живое состояние размыкателя при неверном DATABASE_URL. Тот же гейт, что quota.reset()."""
    global _db_warned
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            "breaker.reset() запрещён вне SQLite (dev/test): это DELETE FROM circuit_state"
        )
    async with Session() as s:
        await s.execute(sa_delete(CircuitState))
        await s.commit()
    _db_warned = False
