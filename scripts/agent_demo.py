"""Демо агент-цикла (офлайн от Google Ads, но на реальной модели через OpenRouter).

Показывает: русская команда → правильный tool-call → сводка «было→станет» + confirm,
или уточняющий вопрос при неоднозначности. Мутации НЕ выполняются (только черновик).

Запуск:  python scripts/agent_demo.py     (нужен OPENROUTER_API_KEY в .env)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.loop import handle_command  # noqa: E402

COMMANDS = [
    "повысь бюджет кампании Лето на 20%",
    "поставь на паузу кампанию Зима",
    "добавь точное соответствие в кампанию Поиск: ремонт обуви киев",
    "покажи статистику по аккаунту 123 за последние 30 дней",
    "увеличь бюджет",  # неоднозначно → должен переспросить
]


def render(cmd: str, res: dict) -> str:
    t = res.get("type")
    if t == "proposal":
        return (
            f"💬 «{cmd}»\n"
            f"   → [{res['operation']}] ЧЕРНОВИК (не выполнен):\n"
            f"     {res['summary']}\n"
            f"     confirmation_id={res['confirmation_id'][:8]}…  → {res['confirm_prompt']}"
        )
    if t == "clarify":
        return f"💬 «{cmd}»\n   → ❓ уточнение: {res['question']}"
    if t == "read":
        s = res.get("stats", {})
        return (
            f"💬 «{cmd}»\n   → 📊 read аккаунт {res['account']} за {res['days']}д "
            f"(ЖИВЫЕ данные Google Ads): {s}"
        )
    return f"💬 «{cmd}»\n   → {res.get('text')}"


async def main() -> None:
    for cmd in COMMANDS:
        try:
            res = await handle_command(cmd)
        except Exception as e:
            print(f"💬 «{cmd}»\n   → ОШИБКА: {type(e).__name__}: {e}\n")
            continue
        print(render(cmd, res) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
