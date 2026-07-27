"""#10 Наблюдаемость (Волна 1 шаг 10): ПЕРСИСТЕНТНЫЙ per-run учёт cost/latency/итераций.

Чего не дают соседи: `core.usage` — живой срез процесса по РОЛЯМ (сбрасывается на рестарте),
`core.quota` — счётчик API-операций (не денег), `core.resilience` — латентность каждого вызова, но
только в лог-строку (гибнет с ротацией). Здесь строка = один прогон агента, переживает рестарт; из неё
считается deliverable шага 10 «сколько стоит прогон / группа в месяц» (SUM cost_usd GROUP BY customer).

Два потока, честно РАЗДЕЛЁННЫЕ (не смешивать):
  • НАШ процесс (`run_scope` + `record_event`): аккумулятор в contextvar копит события хода В ПАМЯТИ,
    в БД пишем ОДНОЙ транзакцией на закрытии scope — горячий путь `resilience`/`router` остаётся
    без БД-задержки. Вне активного scope `record_event` — no-op.
  • Hermes-прогоны идут МИМО нашего процесса (config.yaml:70-71) — их LLM-траты `core.usage` не видит
    вовсе. Их подшивает `import_hermes_activity` из OpenRouter `/activity` строкой origin='hermes'
    (per-день/per-модель — OpenRouter не отдаёт границы ходов Hermes, это честный максимум гранулярности).

Наблюдаемость НЕ роняет денежный путь (правило 5/10): запись fail-OPEN (как `core.usage.record`) —
любой сбой БД/разбора глотаем, ход продолжается. args_redacted проходит `redact_text` ДО записи
(правило 5). Секретов/PII здесь нет: числа + id аккаунта, ключ OpenRouter сюда не попадает.
"""

from __future__ import annotations

import contextvars
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from core.logging import log, redact_text

_ARGS_MAX = 2000  # потолок длины args_redacted в БД (как _MSG_MAX в core.errors)
_DIGEST_MAX = 255


@dataclass
class _RunAcc:
    """Аккумулятор ОДНОГО прогона: заголовок + события копятся в памяти, пишутся на закрытии scope."""

    run_id: str
    origin: str  # human|machine|hermes|cron
    chat_id: int | None = None
    customer_id: str | None = None
    operation: str | None = None
    model: str | None = None
    iterations_used: int = 0  # число LLM-ходов
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    status: str = "running"  # running|ok|error
    events: list[dict[str, Any]] = field(default_factory=list)


_RUN: contextvars.ContextVar[_RunAcc | None] = contextvars.ContextVar(
    "aimash_agent_run", default=None
)


def current_run() -> _RunAcc | None:
    """Аккумулятор ТЕКУЩЕГО прогона (None вне scope). Для тестов/диагностики."""
    return _RUN.get()


def _redact_args(args: Any) -> str | None:
    """dict/значение аргументов инструмента → редактированная строка (правило 5) с потолком длины.
    None/сбой сериализации ⇒ None (наблюдаемость не роняет ход)."""
    if args is None:
        return None
    try:
        raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 — несериализуемое не должно ронять запись
        raw = str(args)
    try:
        return redact_text(raw)[:_ARGS_MAX]
    except Exception:  # noqa: BLE001
        return None


def record_event(
    kind: str,
    *,
    tool_name: str | None = None,
    latency_ms: int | None = None,
    cost_usd: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    rows_returned: int | None = None,
    args: Any = None,
    result_digest: str | None = None,
    ok: bool = True,
) -> None:
    """Зафиксировать ОДИН шаг (llm|tool|ads_read|ads_mutate) в активном прогоне. Вне scope — no-op
    (событие некуда сшить). Никогда не бросает: наблюдаемость не роняет вызывающего (правило 10).

    `kind='llm'` инкрементит iterations_used. Токены/стоимость суммируются в заголовок прогона."""
    acc = _RUN.get()
    if acc is None:
        return
    try:
        acc.events.append(
            {
                "kind": str(kind)[:16],
                "tool_name": (str(tool_name)[:64] if tool_name else None),
                "latency_ms": (int(latency_ms) if latency_ms is not None else None),
                "cost_usd": float(cost_usd or 0.0),
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "rows_returned": (int(rows_returned) if rows_returned is not None else None),
                "args_redacted": _redact_args(args),
                "result_digest": (str(result_digest)[:_DIGEST_MAX] if result_digest else None),
                "ok": bool(ok),
            }
        )
        acc.cost_usd += float(cost_usd or 0.0)
        acc.prompt_tokens += int(prompt_tokens or 0)
        acc.completion_tokens += int(completion_tokens or 0)
        acc.cached_tokens += int(cached_tokens or 0)
        if kind == "llm":
            acc.iterations_used += 1
    except Exception:  # noqa: BLE001 — учёт шага не критичен, не мешаем ходу
        return


def note_model(model: str | None) -> None:
    """Проставить разрезолвленную модель прогона (первую известную). No-op вне scope / при пустом."""
    acc = _RUN.get()
    if acc is None or not model:
        return
    if not acc.model:
        acc.model = str(model)[:64]


@asynccontextmanager
async def run_scope(
    operation: str, *, origin: str | None = None, model: str | None = None
) -> AsyncIterator[_RunAcc]:
    """Открыть прогон агента на время блока. run_id/chat_id/customer_id берутся из `core.context`,
    origin — из `core.provenance` (human_turn) либо явно. Если корреляции ещё нет ('-'), заводим
    свежий request_id И проставляем его в контекст (чтобы лог-строки шага несли тот же run_id).

    На выходе — ОДНА best-effort транзакция: заголовок AgentRun (с суммами и finished_at) + события
    AgentRunEvent по порядку. Сбой записи глотаем (наблюдаемость не роняет ход). Статус: 'ok' при
    нормальном выходе, 'error' если из блока вылетело исключение (пробрасывается дальше)."""
    from core.context import get_context, new_request_id, reset_context, set_context
    from core.provenance import get_provenance

    ctx = get_context()
    prov = get_provenance()
    run_id = (
        next((r for r in (prov.run_id, ctx.request_id) if r and r != "-"), "") or new_request_id()
    )
    run_id = run_id[:16]
    acc = _RunAcc(
        run_id=run_id,
        origin=(origin or ("human" if prov.human_turn else "machine")),
        chat_id=ctx.chat_id,
        customer_id=ctx.customer_id,
        operation=(operation[:64] if operation else None),
        model=(model[:64] if model else None),
    )
    # Заполнить контекст только если он ПУСТ — не затирать существующую корреляцию хода.
    ctx_token = (
        set_context(request_id=run_id, operation=operation) if ctx.request_id == "-" else None
    )
    run_token = _RUN.set(acc)
    try:
        yield acc
        acc.status = "ok"
    except BaseException:
        acc.status = "error"
        raise
    finally:
        _RUN.reset(run_token)
        if ctx_token is not None:
            reset_context(ctx_token)
        await _persist_run(acc)


async def _persist_run(acc: _RunAcc) -> None:
    """Записать заголовок прогона + события ОДНОЙ транзакцией. Best-effort (правило 10): сбор
    наблюдаемости не роняет ход — любую ошибку логируем типом и выходим."""
    try:
        from db.models import AgentRun, AgentRunEvent
        from db.session import Session, db_dt

        finished = db_dt(datetime.now(timezone.utc))
        async with Session() as s:
            s.add(
                AgentRun(
                    run_id=acc.run_id,
                    origin=acc.origin,
                    chat_id=acc.chat_id,
                    customer_id=acc.customer_id,
                    operation=acc.operation,
                    model=acc.model,
                    iterations_used=acc.iterations_used,
                    prompt_tokens=acc.prompt_tokens,
                    completion_tokens=acc.completion_tokens,
                    cached_tokens=acc.cached_tokens,
                    cost_usd=acc.cost_usd,
                    status=acc.status,
                    finished_at=finished,
                )
            )
            for seq, ev in enumerate(acc.events):
                s.add(AgentRunEvent(run_id=acc.run_id, seq=seq, **ev))
            await s.commit()
    except Exception as e:  # noqa: BLE001 — наблюдаемость fail-open (правило 10)
        log.warning("observe: прогон %s не записан (%s)", acc.run_id, type(e).__name__)


def _hermes_run_id(date: str, model: str) -> str:
    """Детерминированный run_id для строки Hermes-активности: 'hz'+YYYYMMDD+6hex(model) = 16 симв.
    Один и тот же (date, model) даёт один id → импорт идемпотентен (повторный запуск обновляет,
    не двоит)."""
    import hashlib

    ymd = date.replace("-", "")[:8]
    h = hashlib.blake2s(model.encode("utf-8"), digest_size=3).hexdigest()  # 6 hex
    return f"hz{ymd}{h}"[:16]


async def import_hermes_activity(date: str | None = None, *, client: Any = None) -> int:
    """Подшить траты Hermes-прогонов из OpenRouter `/activity` в agent_runs строками origin='hermes'
    (закрывает открытый хвост §20: LLM-трафик Hermes идёт мимо нашего процесса). Идемпотентно по
    (date, model). Возвращает число обработанных строк.

    Требует provisioning-ключ (`OPENROUTER_PROVISIONING_KEY`, RB-3) — иначе ридер отдаёт [] (opt-in),
    и импорт тихо обрабатывает 0 строк. Это отдельный поток от `run_scope`: OpenRouter не отдаёт
    границы ходов Hermes, потому гранулярность — per-день/per-модель, а не per-прогон."""
    from sqlalchemy import select

    from core import or_activity
    from db.models import AgentRun
    from db.session import Session, db_dt

    rows = await or_activity.fetch_activity(date, client=client)
    if not rows:
        return 0
    n = 0
    async with Session() as s:
        for r in rows:
            rid = _hermes_run_id(r.date, r.model)
            existing = (
                await s.execute(select(AgentRun).where(AgentRun.run_id == rid))
            ).scalar_one_or_none()
            try:
                started = db_dt(datetime.strptime(r.date, "%Y-%m-%d").replace(tzinfo=timezone.utc))
            except ValueError:
                continue  # кривая дата от эндпоинта — пропускаем строку, не роняем импорт
            if existing is None:
                s.add(
                    AgentRun(
                        run_id=rid,
                        origin="hermes",
                        operation="activity",
                        model=r.model[:64],
                        iterations_used=r.requests,
                        prompt_tokens=r.prompt_tokens,
                        completion_tokens=r.completion_tokens,
                        cost_usd=r.usage,
                        status="ok",
                        started_at=started,
                        finished_at=started,
                    )
                )
            else:  # тот же (date, model) — обновляем цифры (импорт идемпотентен)
                existing.iterations_used = r.requests
                existing.prompt_tokens = r.prompt_tokens
                existing.completion_tokens = r.completion_tokens
                existing.cost_usd = r.usage
                existing.model = r.model[:64]
            n += 1
        await s.commit()
    return n


_GROUP_COLS = {"customer": "customer_id", "origin": "origin", "model": "model"}


async def cost_report(
    *, since: datetime, until: datetime | None = None, group_by: str = "customer"
) -> list[dict[str, Any]]:
    """Отчёт «сколько стоит прогон / группа за окно»: SUM(cost_usd)/SUM(iterations)/COUNT по группе
    (customer|origin|model), окно по started_at. Сортировка — по стоимости убыв. Deliverable шага 10.

    Границы окна приводятся к диалекту БД (`db_dt`) — фильтр идёт в SQL, а не сканом в Python."""
    from sqlalchemy import func, select

    from db.models import AgentRun
    from db.session import Session, db_dt

    col_name = _GROUP_COLS.get(group_by, "customer_id")
    col = getattr(AgentRun, col_name)
    stmt = (
        select(
            col.label("group"),
            func.sum(AgentRun.cost_usd).label("cost_usd"),
            func.sum(AgentRun.iterations_used).label("iterations"),
            func.count(AgentRun.id).label("runs"),
        )
        .where(AgentRun.started_at >= db_dt(since))
        .group_by(col)
        .order_by(func.sum(AgentRun.cost_usd).desc())
    )
    if until is not None:
        stmt = stmt.where(AgentRun.started_at < db_dt(until))
    async with Session() as s:
        result = await s.execute(stmt)
        return [
            {
                "group": row.group,
                "cost_usd": float(row.cost_usd or 0.0),
                "iterations": int(row.iterations or 0),
                "runs": int(row.runs or 0),
            }
            for row in result.all()
        ]
