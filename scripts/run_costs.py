"""#10 Наблюдаемость — отчёт «сколько стоит прогон / группа» из agent_runs (руки владельца).

Deliverable шага 10: персистентная стоимость прогонов, а не срез процесса (core.usage сбрасывается на
рестарте). Опц. перед отчётом подтягивает траты автономного Hermes-цикла из OpenRouter /activity
(идёт мимо нашего процесса, config.yaml:70-71) — единственный способ увидеть их стоимость.

Запуск:
  python scripts/run_costs.py                    # 30 дней, группировка по клиенту
  python scripts/run_costs.py --days 7 --group model
  python scripts/run_costs.py --group origin      # human/machine/hermes — откуда деньги
  python scripts/run_costs.py --import-hermes      # сперва подшить /activity (нужен provisioning-ключ)
  python scripts/run_costs.py --import-hermes 2026-07-23   # конкретный UTC-день

Пишет в БД ТОЛЬКО при --import-hermes (идемпотентная сшивка Hermes-строк); без него — чистое чтение.
Windows-консоль (cp1251) роняет кириллицу в выводе → UTF-8.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _win_console import enable_utf8  # noqa: E402

enable_utf8()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import observe  # noqa: E402
from db.session import init_db  # noqa: E402


def _fmt_rows(rows: list[dict], group_by: str) -> str:
    """Табличка «группа → стоимость/итерации/прогоны» + итог. Числа считает cost_report (SQL), здесь
    только формат."""
    if not rows:
        return "  (нет прогонов за окно)"
    width = max(max((len(str(r["group"] or "—")) for r in rows), default=6), len(group_by), 5)
    out = [f"  {group_by:<{width}}  {'cost,$':>10}  {'iters':>7}  {'runs':>6}"]
    tot_cost = tot_iter = tot_runs = 0.0
    for r in rows:
        g = str(r["group"] or "—")
        out.append(f"  {g:<{width}}  {r['cost_usd']:>10.4f}  {r['iterations']:>7}  {r['runs']:>6}")
        tot_cost += r["cost_usd"]
        tot_iter += r["iterations"]
        tot_runs += r["runs"]
    out.append(f"  {'ИТОГО':<{width}}  {tot_cost:>10.4f}  {int(tot_iter):>7}  {int(tot_runs):>6}")
    return "\n".join(out)


async def _main(args: argparse.Namespace) -> int:
    await (
        init_db()
    )  # dev-SQLite: создать таблицы, если процесс поднят впервые (prod — no-op, миграции)
    if args.import_hermes is not None:  # флаг задан (со значением-датой или без)
        n = await observe.import_hermes_activity(args.import_hermes or None)
        tail = "" if n else " (пусто: нет provisioning-ключа или трат — см. RB-3)"
        print(f"Hermes /activity: подшито строк — {n}{tail}")
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    rows = await observe.cost_report(since=since, group_by=args.group)
    print(f"\nСтоимость прогонов за {args.days} дн. (группировка: {args.group}):")
    print(_fmt_rows(rows, args.group))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Отчёт стоимости прогонов агента (#10) из agent_runs.")
    p.add_argument("--days", type=int, default=30, help="окно в днях (по started_at); дефолт 30")
    p.add_argument(
        "--group",
        choices=("customer", "origin", "model"),
        default="customer",
        help="группировка отчёта; дефолт customer",
    )
    p.add_argument(
        "--import-hermes",
        nargs="?",
        const="",
        metavar="YYYY-MM-DD",
        help="перед отчётом подшить траты Hermes из OpenRouter /activity (опц. UTC-дата)",
    )
    return asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
