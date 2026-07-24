"""Измеритель расхода токенов Claude Code — куда уходят лимиты подписки.

Читает локальные транскрипты сессий (``~/.claude/projects/<slug>/*.jsonl``) и считает по полю
``message.usage`` реальный расход. Нужен, чтобы «стало экономнее» было ФАКТОМ, а не верой:
снимаем baseline, меняем настройки/привычки, снимаем повторно.

Главная метрика — НЕ output, а **размер контекста на запрос**: каждый ход перечитывает всю историю
(``cache_read``), поэтому длинная сессия платит за свой мусор снова и снова. По ценовым пропорциям
Anthropic (output ×5, cache_write ×1.25, cache_read ×0.1 от input) именно ``cache_read`` обычно
даёт ~70% расхода. Точная формула лимитов подписки не опубликована — веса тут ЦЕНОВЫЕ, поэтому
доли считаем в «input-эквиваленте» и смотрим на них как на пропорции, а не как на счёт в долларах.

Запуск:  python scripts/claude_usage.py            # последние 12 сессий текущего проекта
         python scripts/claude_usage.py -n 30      # больше сессий
         python scripts/claude_usage.py --all      # все
         python scripts/claude_usage.py -p c--MY-PROJECTS-WEB-maria-breslavska   # другой проект

Только чтение: скрипт ничего не пишет и не удаляет.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _win_console import enable_utf8  # noqa: E402

# Ценовые веса Anthropic относительно input-токена. Не формула лимитов (она закрыта), а
# пропорция, по которой видно, какая статья реально доминирует.
W_OUT, W_CACHE_WRITE, W_CACHE_READ, W_IN = 5.0, 1.25, 0.1, 1.0

# Порог, выше которого контекст считаем раздутым: 200k — окно обычной модели, на котором проект
# жил до [1m]. Всё, что выше, — плата за мусор прошлых задач.
CTX_BLOAT = 200_000
CTX_TARGET = 150_000  # цель по медиане после оптимизации


def project_slug(path: Path) -> str:
    """Каталог транскриптов Claude Code: путь проекта, где все не-алфанумерики → дефис."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path))


def transcripts_dir(project: str | None) -> Path:
    root = Path.home() / ".claude" / "projects"
    if project:
        cand = Path(project)
        slug = project if not cand.is_absolute() else project_slug(cand)
    else:
        slug = project_slug(Path.cwd())
    return root / slug


def collect(files: list[Path]) -> tuple[list[dict], list[dict]]:
    """(строки-ответы модели, посессионные сводки). Битые строки молча пропускаем."""
    rows: list[dict] = []
    sessions: list[dict] = []
    for fp in files:
        s = {"file": fp.stem[:8], "mtime": fp.stat().st_mtime, "msgs": 0, "ctx": [], "cost": 0.0}
        with fp.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "assistant":
                    continue
                u = (o.get("message") or {}).get("usage")
                if not u:
                    continue
                r = {
                    "model": (o.get("message") or {}).get("model") or "?",
                    "side": bool(o.get("isSidechain")),
                    "inp": u.get("input_tokens", 0),
                    "cw": u.get("cache_creation_input_tokens", 0),
                    "cr": u.get("cache_read_input_tokens", 0),
                    "out": u.get("output_tokens", 0),
                }
                r["ctx"] = r["inp"] + r["cw"] + r["cr"]
                r["cost"] = (
                    r["out"] * W_OUT
                    + r["cw"] * W_CACHE_WRITE
                    + r["cr"] * W_CACHE_READ
                    + r["inp"] * W_IN
                )
                rows.append(r)
                s["msgs"] += 1
                s["cost"] += r["cost"]
                if r["ctx"]:
                    s["ctx"].append(r["ctx"])
        if s["msgs"]:
            sessions.append(s)
    return rows, sessions


def pct(vals: list[int], q: float) -> int:
    return vals[min(int(len(vals) * q), len(vals) - 1)] if vals else 0


def main() -> None:
    enable_utf8()
    ap = argparse.ArgumentParser(description="Расход токенов Claude Code по транскриптам сессий")
    ap.add_argument(
        "-n", "--sessions", type=int, default=12, help="сколько последних сессий (по умолчанию 12)"
    )
    ap.add_argument("--all", action="store_true", help="все сессии")
    ap.add_argument(
        "-p", "--project", help="слаг каталога или путь проекта (по умолчанию — текущий)"
    )
    args = ap.parse_args()

    d = transcripts_dir(args.project)
    if not d.is_dir():
        print(f"Нет каталога транскриптов: {d}")
        avail = sorted(
            p.name for p in (Path.home() / ".claude" / "projects").glob("*") if p.is_dir()
        )
        print("Доступные проекты:\n  " + "\n  ".join(avail))
        raise SystemExit(1)

    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not args.all:
        files = files[-args.sessions :]
    rows, sessions = collect(files)
    if not rows:
        print(f"В {d} нет данных usage.")
        raise SystemExit(1)

    total = sum(r["cost"] for r in rows)
    ctx = sorted(r["ctx"] for r in rows if r["ctx"])

    print(f"\nПроект: {d.name}   сессий: {len(sessions)}   ответов модели: {len(rows)}\n")

    print("=== СЕССИИ ===")
    print(f"{'сессия':10s} {'дата':11s} {'ответов':>8s} {'медиана ctx':>12s} {'доля расхода':>13s}")
    for s in sorted(sessions, key=lambda x: -x["cost"]):
        c = sorted(s["ctx"])
        med = pct(c, 0.5)
        day = datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d")
        print(
            f"{s['file']:10s} {day:11s} {s['msgs']:8,d} {med:12,d} {s['cost'] / total * 100:12.1f}%"
        )

    print("\n=== ИЗ ЧЕГО СКЛАДЫВАЕТСЯ РАСХОД (input-эквивалент) ===")
    stat = (
        ("cache_read — перечитывание контекста", sum(r["cr"] for r in rows) * W_CACHE_READ),
        ("cache_write — запись префикса/хвоста", sum(r["cw"] for r in rows) * W_CACHE_WRITE),
        ("output — ответы + thinking", sum(r["out"] for r in rows) * W_OUT),
        ("input — некэшированный ввод", sum(r["inp"] for r in rows) * W_IN),
    )
    for name, val in stat:
        print(f"  {name:38s} {val / total * 100:5.1f}%")

    print("\n=== РАЗМЕР КОНТЕКСТА НА ЗАПРОС (главный рычаг) ===")
    for label, q in (("медиана", 0.5), ("p75", 0.75), ("p90", 0.90), ("p99", 0.99)):
        print(f"  {label:8s} {pct(ctx, q):>9,d}")
    print(f"  {'максимум':8s} {ctx[-1]:>9,d}")
    bloat = [c for c in ctx if c > CTX_BLOAT]
    if bloat:
        share = sum(bloat) * W_CACHE_READ / total * 100
        print(
            f"\n  запросов с контекстом >{CTX_BLOAT:,d}: {len(bloat):,d} "
            f"({len(bloat) / len(ctx) * 100:.0f}%) — на них ~{share:.0f}% всего расхода"
        )
    med = pct(ctx, 0.5)
    verdict = "✅ в цели" if med <= CTX_TARGET else f"❌ выше цели в {med / CTX_TARGET:.1f}×"
    print(f"  медиана {med:,d} против цели {CTX_TARGET:,d} — {verdict}")

    print("\n=== ГЛАВНЫЙ ПОТОК vs СУБАГЕНТЫ ===")
    for label, want in (("главный поток", False), ("субагенты", True)):
        sub = [r for r in rows if r["side"] is want]
        cost = sum(r["cost"] for r in sub)
        print(f"  {label:16s} ответов={len(sub):5,d}  доля расхода={cost / total * 100:5.1f}%")
    if not any(r["side"] for r in rows):
        print("  ⚠ субагентов нет: вся разведка по коду оседает в главном контексте и")
        print("    перечитывается на каждом ходу. Делегируй поиск/чтение агенту Explore.")

    print("\n=== ПО МОДЕЛЯМ ===")
    models = sorted({r["model"] for r in rows})
    for m in models:
        sub = [r for r in rows if r["model"] == m]
        cost = sum(r["cost"] for r in sub)
        out_per = sum(r["out"] for r in sub) // len(sub)
        print(
            f"  {m:28s} ответов={len(sub):5,d}  out/ответ={out_per:6,d}  доля расхода={cost / total * 100:5.1f}%"
        )
    print()


if __name__ == "__main__":
    main()
