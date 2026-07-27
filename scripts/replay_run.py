"""Реплей прогона агента по журналу событий: что агент читал, что решил, что исполнил.

Волна 3 (event sourcing). Журнал `agent_runs` + `agent_run_events` теперь наполняется с трёх сторон
сразу — агентский цикл, тул-слой MCP (`_guarded`) и денежный путь (`ads.service.execute_confirmed`),
— а денежное событие пишется fail-closed ДО вызова Google Ads. Этот скрипт — читающая сторона того
же контура: он восстанавливает ход шаг за шагом, проверяет хэш-цепочку и сверяет заявленные мутации
с audit-row.

Три вопроса, на которые он отвечает, и почему их нельзя закрыть логами:

  1. **Что происходило в ходе X.** Лог-строки ротируются и не сшиты между процессами; события сшиты
     по `run_id` и переживают рестарт.
  2. **Не подделан ли журнал.** `verify_chain` пересчитывает хэш-цепочку и говорит, ГДЕ она рвётся.
     Триггер СУБД запрещает правку, цепочка делает правку ВИДНОЙ — включая случай, когда правивший
     имел права на таблицу и триггер снял.
  3. **Совпадает ли «что собирались сделать» с «что сделано».** Событие `ads_mutate` — это ЗАЯВКА на
     исполнение (пишется до SDK-вызова), исход живёт в `audit_log` (правило 15). Расхождение между
     ними — ровно тот класс, ради которого журнал заведён: заявка есть, audit-row нет ⇒ вызов не
     дошёл или упал молча.

Запуск (только чтение, ни Google Ads, ни LLM не трогаются):

    python scripts/replay_run.py --list                 # последние прогоны
    python scripts/replay_run.py --run-id a1b2c3d4      # разбор одного хода
    python scripts/replay_run.py --run-id a1b2c3d4 --json
    python scripts/replay_run.py --verify --days 30     # цепочки всех прогонов за окно

⚠️ Чего здесь НЕТ и почему: побитового ПЕРЕ-ИСПОЛНЕНИЯ хода (`--reexec` из плана) нет. Для него
нужен сохранённый сырой ответ модели и драйвер поверх агентского цикла — а цикл (`agent/loop.py`)
архивируется, его несёт Hermes, и драйвер, написанный сегодня против уходящего кода, будет выброшен
вместе с ним. Восстанавливается ПУТЬ решения (этот скрипт), а не сэмплинг модели: новая генерация
недетерминирована в принципе, и обещать «побитово тот же ответ» было бы неправдой.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _win_console import enable_utf8  # noqa: E402

_KIND_MARK = {
    "llm": "🧠",
    "tool": "🔧",
    "ads_read": "📖",
    "ads_mutate": "💸",
    "compensation": "↩️",
}


def _cid_of(ev) -> str | None:
    """confirmation_id из редактированных аргументов денежного события (или None).

    Разбираем ЗАЩИЩЁННО: `args_redacted` прошёл `redact_text` и обрезан по потолку длины — на длинных
    аргументах это валидный JSON только до обрезки. Не разобралось ⇒ None, а не падение: сверка с
    audit-row не обязана работать всегда, но обязана не врать."""
    raw = getattr(ev, "args_redacted", None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — обрезанный/нестандартный payload: просто нечего сверять
        return None
    cid = data.get("confirmation_id") if isinstance(data, dict) else None
    return str(cid) if cid else None


async def _audit_rows(cids: list[str]) -> dict[str, list[tuple[str, str]]]:
    """confirmation_id → [(status, operation)] из audit_log. Пустой список ⇒ строки нет вовсе."""
    from sqlalchemy import select

    from db.models import AuditLog
    from db.session import Session

    if not cids:
        return {}
    async with Session() as s:
        rows = (
            await s.execute(
                select(AuditLog.confirmation_id, AuditLog.status, AuditLog.operation)
                .where(AuditLog.confirmation_id.in_(cids))
                .order_by(AuditLog.created_at)
            )
        ).all()
    out: dict[str, list[tuple[str, str]]] = {c: [] for c in cids}
    for cid, status, op in rows:
        out.setdefault(cid, []).append((status, op))
    return out


async def _load(run_id: str):
    from sqlalchemy import select

    from db.models import AgentRun, AgentRunEvent
    from db.session import Session

    async with Session() as s:
        # `.first()`, а не `scalar_one_or_none`: run_id не уникален на уровне схемы, а инструмент
        # форензики обязан ПОКАЗАТЬ грязные данные, а не упасть на них. Заголовков больше одного
        # быть не должно (вложенный scope переиспользует внешний) — но если они есть, увидеть это
        # надо в выводе, а не в трейсбеке.
        run = (
            (
                await s.execute(
                    select(AgentRun).where(AgentRun.run_id == run_id).order_by(AgentRun.id)
                )
            )
            .scalars()
            .first()
        )
        events = list(
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
    return run, events


async def _list_runs(limit: int) -> None:
    from sqlalchemy import desc, select

    from db.models import AgentRun
    from db.session import Session

    async with Session() as s:
        rows = list(
            (await s.execute(select(AgentRun).order_by(desc(AgentRun.started_at)).limit(limit)))
            .scalars()
            .all()
        )
    if not rows:
        print("прогонов нет (журнал пуст)")
        return
    print(f"{'run_id':<18}{'начат':<21}{'origin':<9}{'операция':<20}{'ст':<7}{'итер':>5}{'$':>9}")
    for r in rows:
        started = r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "-"
        print(
            f"{r.run_id:<18}{started:<21}{r.origin:<9}{(r.operation or '-'):<20}"
            f"{r.status:<7}{r.iterations_used:>5}{r.cost_usd:>9.4f}"
        )


async def _replay(run_id: str, *, as_json: bool) -> int:
    """Разбор одного прогона. Возвращает код выхода: 0 — цепочка цела и заявки сошлись с audit-row."""
    from core import observe

    run, events = await _load(run_id)
    chain = await observe.verify_chain(run_id)

    money = [e for e in events if e.kind in observe.MONEY_KINDS]
    cids = [c for c in (_cid_of(e) for e in money) if c]
    audit = await _audit_rows(cids)
    # Заявка без audit-row = вызов не дошёл до записи исхода. Это НЕ «мутации не было»: событие
    # пишется до SDK, и молчание audit_log означает ровно неизвестность, а не отсутствие.
    unmatched = [c for c in cids if not audit.get(c)]

    if as_json:
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "found": run is not None,
                    "chain": chain,
                    "events": [
                        {
                            "seq": e.seq,
                            "kind": e.kind,
                            "tool_name": e.tool_name,
                            "latency_ms": e.latency_ms,
                            "ok": e.ok,
                            "cost_usd": e.cost_usd,
                            "rows_returned": e.rows_returned,
                            "args_redacted": e.args_redacted,
                            "payload_digest": e.payload_digest,
                        }
                        for e in events
                    ],
                    "money_claims": {c: audit.get(c, []) for c in cids},
                    "unmatched_claims": unmatched,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0 if (chain["ok"] and not unmatched) else 1

    if run is None:
        print(f"⚠️  заголовка прогона {run_id} нет — события есть, agent_runs пуста")
    else:
        print(f"прогон  {run.run_id}   origin={run.origin}   операция={run.operation or '-'}")
        print(
            f"        статус={run.status}  итераций={run.iterations_used}  "
            f"токены={run.prompt_tokens}+{run.completion_tokens} (кэш {run.cached_tokens})  "
            f"${run.cost_usd:.4f}  модель={run.model or '-'}"
        )
        print(f"        аккаунт={run.customer_id or '-'}  начат={run.started_at}")
    print()

    if not events:
        print("событий нет")
    else:
        print(f"{'seq':>4}  {'':2} {'kind':<13}{'инструмент':<26}{'ms':>7} {'ok':<3}{'digest':<10}")
        for e in events:
            mark = _KIND_MARK.get(e.kind, "  ")
            dig = (e.payload_digest or "")[:8] or "—"
            lat = str(e.latency_ms) if e.latency_ms is not None else "-"
            print(
                f"{e.seq:>4}  {mark:2} {e.kind:<13}{(e.tool_name or '-'):<26}{lat:>7} "
                f"{('да' if e.ok else 'НЕТ'):<3}{dig:<10}"
            )
            if e.args_redacted:
                print(f"          args: {e.args_redacted[:160]}")
    print()

    mark = "✅" if chain["ok"] else "❌"
    print(
        f"{mark} цепочка: события={chain['events']} вне цепочки={chain['unchained']} "
        f"разрыв={chain['broken_at']} причина={chain['reason'] or '—'}"
    )
    if chain["unchained"]:
        print(
            "   (строки без дайджеста записаны до миграции 0035 — они не сломаны, а непроверяемы)"
        )

    if cids:
        print()
        print("заявки денежного пути ↔ audit_log:")
        for c in cids:
            rows = audit.get(c) or []
            shown = ", ".join(f"{st}/{op}" for st, op in rows) if rows else "audit-row НЕТ"
            print(f"   {'✅' if rows else '❌'} {c}: {shown}")
    return 0 if (chain["ok"] and not unmatched) else 1


async def _verify_window(days: int) -> int:
    """Проверить цепочки ВСЕХ прогонов за окно. Код выхода 1, если хоть одна порвана."""
    from sqlalchemy import select

    from core import observe
    from db.models import AgentRun
    from db.session import Session, db_dt

    cutoff = db_dt(datetime.now(timezone.utc) - timedelta(days=max(1, days)))
    async with Session() as s:
        run_ids = list(
            (await s.execute(select(AgentRun.run_id).where(AgentRun.started_at >= cutoff)))
            .scalars()
            .all()
        )
    broken = []
    for rid in run_ids:
        v = await observe.verify_chain(rid)
        if not v["ok"]:
            broken.append((rid, v))
    print(f"проверено прогонов: {len(run_ids)} за {days} дн.")
    if not broken:
        print("✅ разрывов нет")
        return 0
    for rid, v in broken:
        print(f"❌ {rid}: разрыв на seq={v['broken_at']} ({v['reason']})")
    return 1


async def main_async(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-id", help="разобрать один прогон")
    g.add_argument("--list", action="store_true", help="последние прогоны")
    g.add_argument("--verify", action="store_true", help="проверить цепочки всех прогонов за окно")
    p.add_argument("-n", "--limit", type=int, default=20, help="сколько прогонов в --list")
    p.add_argument("--days", type=int, default=30, help="окно для --verify")
    p.add_argument("--json", action="store_true", help="машиночитаемый вывод для --run-id")
    a = p.parse_args(argv)

    if a.list:
        await _list_runs(a.limit)
        return 0
    if a.verify:
        return await _verify_window(a.days)
    return await _replay(a.run_id, as_json=a.json)


def main() -> int:
    enable_utf8()  # cp1251-консоль иначе роняет вывод UnicodeEncodeError
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
