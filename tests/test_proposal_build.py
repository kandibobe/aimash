"""Инвариант: сборка черновика мутации не тянет транспорт.

Класс бага, который тест закрывает: гейты до кнопок (замок аккаунта, валютная сверка,
аттестация свежести) исторически жили в aiogram-хендлере `bot.main._present_proposal`. Пока
`mcp_server/propose.py` от aiogram свободен, второй контур подтверждения (MCP-WRITE, свой поллер)
переиспользует ТЕ ЖЕ гейты. Стоит вернуть сюда импорт транспорта — и второму контуру придётся
дублировать гейты руками, а продублированный руками гейт расходится с оригиналом МОЛЧА.

Модуль переехал `bot/proposal.py` → `mcp_server/propose.py` (Волна 2): его зовёт `mcp_server.
tools_write`, а импорт `bot.*` из MCP-процесса протащил бы весь Telegram-слой. Здесь проверяем
только aiogram-чистоту точечно; полную bot-развязку тул-слоя держит `test_headless_bootstrap.py`.

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
import mcp_server.propose  # noqa: F401
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
    assert proc.returncode == 0, f"импорт mcp_server.propose упал:\n{proc.stderr}"
    leaked = [m for m in proc.stdout.strip().split(",") if m]
    assert not leaked, (
        f"mcp_server.propose подтянул транспорт: {leaked}. Гейты до кнопок обязаны оставаться "
        f"вызываемыми из MCP-WRITE и любого второго контура подтверждения без aiogram"
    )
