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

Волна 3 (event sourcing) добавляет РОВНО ОДНО исключение из этого fail-open и хэш-цепочку:
`record_money_event` (kind ∈ MONEY_KINDS) пишет СИНХРОННО, своей транзакцией и fail-CLOSED — см. её
докстринг о том, почему батч на закрытии scope для мутаций непригоден в принципе. Неизменяемость
обеспечивает СУБД (триггеры, `db.models.event_immutability_ddl`), а вырезанное звено видно по
`prev_digest`/`payload_digest` (`verify_chain`) — не дисциплиной вызывающего.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from core.logging import log, redact_text

_ARGS_MAX = 2000  # потолок длины args_redacted в БД (как _MSG_MAX в core.errors)
_DIGEST_MAX = 255

# Волна 3: события ДЕНЕЖНОГО пути. Для них запись fail-closed (record_money_event), а удаление
# блокирует триггер СУБД, пока строка моложе пола хранения. Значение — и в SQL-триггерах
# (db.models.event_immutability_ddl), менять надо в обоих местах согласованно (гард — тесты).
MONEY_KINDS = frozenset({"ads_mutate", "compensation"})
MONEY_RETENTION_DAYS = 365


class EventWriteError(RuntimeError):
    """Событие ДЕНЕЖНОГО пути не записано ⇒ мутация исполняться не должна.

    Отдельный тип, потому что это единственный класс наблюдаемости, чей сбой обязан остановить ход:
    вся прочая — fail-open по правилу 10 (авария наблюдаемости не должна становиться аварией бота)."""


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


# ── Хэш-цепочка событий (Волна 3): вырезанное/переписанное звено обязано быть ВИДНО ──────────
# Поля события, входящие в дайджест. `created_at` НЕ входит намеренно: его ставит СУБД
# server_default'ом, и в момент вычисления дайджеста клиент его не знает. Подмену времени ловит не
# хэш, а триггер (UPDATE запрещён целиком) — разделение честное, а не «забыли».
_CHAINED_FIELDS = (
    "kind",
    "tool_name",
    "latency_ms",
    "cost_usd",
    "prompt_tokens",
    "completion_tokens",
    "rows_returned",
    "args_redacted",
    "result_digest",
    "ok",
)


def _canon(value: Any) -> Any:
    """Нормализовать значение ПЕРЕД сериализацией: float — до 6 знаков, контейнеры — рекурсивно,
    несериализуемое — в строку. Без этого дайджест зависит от представления числа, а не от числа."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return str(value)


def canonical_json(payload: Any) -> str:
    """Канонический JSON: сортированные ключи, без пробелов, фиксированная точность float.
    Дайджест обязан воспроизводиться ДРУГИМ процессом и другой версией кода — иначе цепочка
    неверифицируема, а «неизменяемый журнал» проверить нечем."""
    return json.dumps(_canon(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def genesis_digest(run_id: str) -> str:
    """Якорь цепочки прогона. Считается ОТ run_id, а не константа: иначе первое событие можно
    перенести в другой прогон, не порвав ни одной ссылки."""
    return hashlib.sha256(f"aimash:run:{run_id}".encode("utf-8")).hexdigest()


def event_digest(run_id: str, seq: int, ev: dict[str, Any], prev_digest: str) -> str:
    """sha256 по (run_id, seq, prev_digest, содержимое события) — звено цепочки."""
    payload = {"run_id": run_id, "seq": int(seq), "prev": prev_digest}
    payload.update({k: ev.get(k) for k in _CHAINED_FIELDS})
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def chain_events(
    run_id: str, events: list[dict[str, Any]], *, start_seq: int = 0, prev: str | None = None
) -> list[dict[str, Any]]:
    """Проставить подряд идущим событиям одного прогона seq/prev_digest/payload_digest.
    Возвращает НОВЫЕ словари (вход не мутируется) — годные под `AgentRunEvent(run_id=…, **row)`."""
    out: list[dict[str, Any]] = []
    prev_digest = prev or genesis_digest(run_id)
    for i, ev in enumerate(events):
        seq = start_seq + i
        digest = event_digest(run_id, seq, ev, prev_digest)
        out.append({**ev, "seq": seq, "prev_digest": prev_digest, "payload_digest": digest})
        prev_digest = digest
    return out


async def _chain_tail(session: Any, run_id: str) -> tuple[int, str]:
    """(следующий seq, дайджест последнего события) прогона. Пустой прогон ⇒ (0, genesis).

    Строка без `payload_digest` (записана ДО миграции 0035) якорем служить не может — берём genesis;
    разрыв на стыке старого и нового покажет `verify_chain`, а не тихо замажет запись."""
    from sqlalchemy import select

    from db.models import AgentRunEvent

    row = (
        await session.execute(
            select(AgentRunEvent.seq, AgentRunEvent.payload_digest)
            .where(AgentRunEvent.run_id == run_id)
            .order_by(AgentRunEvent.seq.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return 0, genesis_digest(run_id)
    return int(row[0]) + 1, (row[1] or genesis_digest(run_id))


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
    нормальном выходе, 'error' если из блока вылетело исключение (пробрасывается дальше).

    ВЛОЖЕННЫЙ scope ПЕРЕИСПОЛЬЗУЕТ внешний, а не заводит второй прогон. Прогон = один ассистентский
    ход, а ход не содержит внутри себя ходов; иначе `execute_confirmed`, вызванный из агентского
    цикла, дал бы ДВЕ строки agent_runs с одним run_id — и `cost_report` посчитал бы один ход за два.
    Волна 3 открывает scope на денежном пути и в тул-слое, то есть вложенность стала штатной."""
    from core.context import get_context, new_request_id, reset_context, set_context
    from core.provenance import get_provenance

    outer = _RUN.get()
    if outer is not None:
        yield outer  # события блока лягут во внешний прогон; своей транзакции здесь нет
        return

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
            # seq продолжает УЖЕ ЛЕЖАЩИЕ события прогона, а не начинается с нуля: денежные события
            # пишутся синхронно (record_money_event) ДО закрытия scope, и enumerate(0..N) затёр бы
            # их позиции — цепочка разветвилась бы на два звена с одним prev_digest.
            next_seq, prev = await _chain_tail(s, acc.run_id)
            for row in chain_events(acc.run_id, acc.events, start_seq=next_seq, prev=prev):
                s.add(AgentRunEvent(run_id=acc.run_id, **row))
            await s.commit()
    except Exception as e:  # noqa: BLE001 — наблюдаемость fail-open (правило 10)
        log.warning("observe: прогон %s не записан (%s)", acc.run_id, type(e).__name__)


def current_run_id() -> str:
    """run_id ТЕКУЩЕГО хода — одна корреляция на ход (provenance.run_id == context.request_id).
    Нет ни того, ни другого ⇒ заводим свежий: событие без прогона лучше, чем отсутствие события."""
    from core.context import get_context, new_request_id
    from core.provenance import get_provenance

    ctx = get_context()
    prov = get_provenance()
    rid = next((r for r in (prov.run_id, ctx.request_id) if r and r != "-"), "") or new_request_id()
    return rid[:16]


async def record_money_event(
    kind: str,
    *,
    operation: str | None = None,
    confirmation_id: str | None = None,
    customer_id: str | None = None,
    args: Any = None,
    result_digest: str | None = None,
    ok: bool = True,
) -> str:
    """Записать событие ДЕНЕЖНОГО пути СИНХРОННО, СОБСТВЕННОЙ транзакцией и FAIL-CLOSED.

    Почему не `record_event`. Тот копит событие в памяти и пишет всё скопом на закрытии `run_scope`
    — то есть УЖЕ ПОСЛЕ вызова к Google Ads. Сделать ту запись fail-closed бессмысленно: деньги
    потрачены, откатывать нечего. Событие мутации обязано лечь в БД ДО SDK-вызова, иначе
    «неизменяемый журнал» не покрывает ровно тот класс, ради которого заведён.

    Почему fail-closed здесь НЕ заводит нового класса аварий. Денежный путь и так жёстко зависит от
    ЭТОЙ БД: в ней живёт `confirm_store`, и без неё `claim` не выполнится в любом случае. Отказ
    здесь ничего не открывает и ничего дополнительно не роняет — он лишь отказывает раньше.

    Что событие ЗНАЧИТ: заявку на исполнение, а не факт исполнения. Исход мутации живёт в audit-row,
    и «выполнено» пользователю репортится оттуда (правило 15), а не отсюда.

    Работает и вне `run_scope` (мутацию может звать headless-путь без агентского цикла): run_id
    берётся из provenance/context. Возвращает run_id, под которым событие записано.

    Raises:
        EventWriteError: событие не записано. Вызывающий обязан НЕ выполнять мутацию.
    """
    from db.models import AgentRunEvent
    from db.session import Session

    from core.context import get_context

    run_id = current_run_id()
    ev = {
        "kind": str(kind)[:16],
        "tool_name": (str(operation)[:64] if operation else None),
        "latency_ms": None,
        "cost_usd": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "rows_returned": None,
        "args_redacted": _redact_args(
            {
                "confirmation_id": confirmation_id,
                "customer_id": customer_id or get_context().customer_id,
                "args": args,
            }
        ),
        "result_digest": (str(result_digest)[:_DIGEST_MAX] if result_digest else None),
        "ok": bool(ok),
    }
    try:
        async with Session() as s:
            next_seq, prev = await _chain_tail(s, run_id)
            row = chain_events(run_id, [ev], start_seq=next_seq, prev=prev)[0]
            s.add(AgentRunEvent(run_id=run_id, **row))
            await s.commit()
    except Exception as e:  # noqa: BLE001 — наружу ТИП, не str(e) (правило 5: в DSN пароль)
        raise EventWriteError(
            f"событие '{kind}' денежного пути не записано ({type(e).__name__}) — мутация отклонена"
        ) from None
    return run_id


async def verify_chain(run_id: str) -> dict[str, Any]:
    """Пересчитать хэш-цепочку прогона и сказать, ГДЕ она рвётся: замена доверия проверкой.

    Ловит и переписанное звено (дайджест не сойдётся), и вырезанное (`prev_digest` следующего не
    совпадёт с `payload_digest` предыдущего, либо разорвётся нумерация seq) — даже если писавший
    имел права на таблицу и триггер обошёл.

    Строки, записанные ДО миграции 0035, дайджеста не имеют. Они не «сломаны» — они вне цепочки:
    считаем их отдельно (`unchained`) и проверку начинаем с первой подписанной. Врать «всё сошлось»
    про непроверяемый хвост нельзя, молча его проверять — тем более.

    Returns:
        {ok, events, unchained, broken_at, reason} — `broken_at` = seq первого расхождения."""
    from sqlalchemy import select

    from db.models import AgentRunEvent
    from db.session import Session

    async with Session() as s:
        rows = list(
            (
                await s.execute(
                    select(AgentRunEvent)
                    .where(AgentRunEvent.run_id == run_id)
                    .order_by(AgentRunEvent.seq)
                )
            )
            .scalars()
            .all()
        )
    unchained = 0
    while unchained < len(rows) and not rows[unchained].payload_digest:
        unchained += 1
    chained = rows[unchained:]
    prev_digest: str | None = None
    prev_seq: int | None = None
    for row in chained:
        if not row.payload_digest:  # подписанное, потом неподписанное — дырка в середине
            return {
                "ok": False,
                "events": len(rows),
                "unchained": unchained,
                "broken_at": row.seq,
                "reason": "missing_digest",
            }
        if prev_seq is not None and row.seq != prev_seq + 1:
            return {
                "ok": False,
                "events": len(rows),
                "unchained": unchained,
                "broken_at": row.seq,
                "reason": "seq_gap",
            }
        if prev_digest is not None and row.prev_digest != prev_digest:
            return {
                "ok": False,
                "events": len(rows),
                "unchained": unchained,
                "broken_at": row.seq,
                "reason": "prev_mismatch",
            }
        ev = {k: getattr(row, k) for k in _CHAINED_FIELDS}
        if event_digest(run_id, row.seq, ev, row.prev_digest or "") != row.payload_digest:
            return {
                "ok": False,
                "events": len(rows),
                "unchained": unchained,
                "broken_at": row.seq,
                "reason": "payload_mismatch",
            }
        prev_digest, prev_seq = row.payload_digest, row.seq
    return {
        "ok": True,
        "events": len(rows),
        "unchained": unchained,
        "broken_at": None,
        "reason": None,
    }


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
