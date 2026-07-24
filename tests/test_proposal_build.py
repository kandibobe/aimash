"""Инвариант: сборка черновика мутации не тянет транспорт.

Класс бага, который тест закрывает: гейты до кнопок (замок аккаунта, валютная сверка,
аттестация свежести) исторически жили в aiogram-хендлере `bot.main._present_proposal`. Пока
`bot/proposal.py` от aiogram свободен, второй контур подтверждения (MCP-WRITE, свой поллер)
переиспользует ТЕ ЖЕ гейты. Стоит вернуть сюда импорт транспорта — и второму контуру придётся
дублировать гейты руками, а продублированный руками гейт расходится с оригиналом МОЛЧА.

Проверка идёт в отдельном процессе намеренно: в общем прогоне `aiogram` уже импортирован
десятком других тестов, поэтому `"aiogram" in sys.modules` в этом процессе ничего не докажет.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent

_PROBE = """
import sys
import bot.proposal  # noqa: F401
leaked = sorted(m for m in sys.modules if m == "aiogram" or m.startswith("aiogram."))
print(",".join(leaked))
"""


def test_build_is_transport_free():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"импорт bot.proposal упал:\n{proc.stderr}"
    leaked = [m for m in proc.stdout.strip().split(",") if m]
    assert not leaked, (
        f"bot.proposal подтянул транспорт: {leaked}. Гейты до кнопок обязаны оставаться "
        f"вызываемыми из MCP-WRITE и любого второго контура подтверждения без aiogram"
    )
