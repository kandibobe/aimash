"""Live-демо БЛОКА E (keyword research) на ТЕСТ-аккаунте 7753643025. READ-ONLY (advisory).

Демонстрирует: замок аккаунта (чужой customer_id → PermissionError ДО запроса) → подбор идей
через KeywordPlanIdeaService (объём/конкуренция приходят реально; оценки ставок на тесте нулевые)
→ AI-кластеризация по интенту (fallback на одну группу, если LLM недоступен) → экспорт .xlsx.

Ничего в аккаунте не меняется (мутаций нет, confirm-гейт не нужен). Запуск:
    python scripts/exec_demo_keywords.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows-консоль (cp1251) роняет emoji/кириллицу в выводе → UTF-8 (общий хелпер).
from _win_console import enable_utf8  # noqa: E402

enable_utf8()

from ads.client import DRAFT_ACCOUNT_ID, build_client  # noqa: E402
from ads.keyword_plan import generate_keyword_ideas  # noqa: E402
from keywords.cluster import cluster_keywords  # noqa: E402
from keywords.export import write_keywords_xlsx  # noqa: E402

_SEEDS = ["доставка цветов", "купить букет"]


async def main() -> None:
    client = build_client()

    print("── замок аккаунта (golden rule #9) ──")
    try:
        generate_keyword_ideas(client, "1234567890", seeds=_SEEDS)
        print("  ❌ чужой аккаунт НЕ заблокирован — замок сломан!")
    except PermissionError:
        print("  ✅ чужой customer_id отклонён ДО запроса (ensure_allowed)")

    print(f"\n── подбор идей на {DRAFT_ACCOUNT_ID} (сиды: {', '.join(_SEEDS)}) ──")
    ideas = await asyncio.to_thread(
        generate_keyword_ideas, client, DRAFT_ACCOUNT_ID, seeds=_SEEDS, language="ru"
    )
    print(f"  получено идей: {len(ideas)}")
    for i in ideas[:8]:
        print(f"    {i.text!r}: {i.avg_monthly_searches}/мес, конкуренция {i.competition}")

    if not ideas:
        print("  идей нет — проверь доступ/сиды")
        return

    print("\n── кластеризация по интенту ──")
    clusters = await cluster_keywords([i.text for i in ideas], "ru")
    for cl in clusters:
        intent = f" [{cl.intent}]" if cl.intent else ""
        print(f"  • {cl.name}{intent}: {len(cl.keywords)} ключ.")

    print("\n── экспорт .xlsx ──")
    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="aimash_keywords_demo_")
    os.close(fd)
    await asyncio.to_thread(write_keywords_xlsx, clusters, ideas, path, seeds=_SEEDS, language="ru")
    print(f"  таблица сохранена: {path} ({os.path.getsize(path)} байт)")
    print("\nOK: keyword research работает вживую (read-only)")


if __name__ == "__main__":
    asyncio.run(main())
