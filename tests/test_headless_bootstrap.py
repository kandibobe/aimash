"""Гард РАЗВЯЗКИ headless-контура (пивот Hermes/MCP): ads+confirm поднимаются БЕЗ bot/aiogram.

`app/bootstrap.py` сеет модульные глобалы `ads/client.py` для MCP-сервера (Контур A), который
`bot` НЕ импортирует. Инвариант, на котором стоит вся форма `mcp_server/` как «тонкой обёртки»:
ни `app.bootstrap`, ни `ads.read/client/mutations`, ни `confirm.gate/store` не тянут `bot.*` /
`aiogram.*` в `sys.modules`. Ломается ТИХО — кто-то добавил `from bot ...` в `ads/`/`confirm/`, и
MCP-процесс молча потащит весь Telegram-слой (в пределе — второй event loop бота). Draft этого не
покажет: там всё в одном процессе.

Проверяется в ОТДЕЛЬНОМ интерпретаторе: под pytest `conftest` уже импортирует `bot`, поэтому
in-process проверка `sys.modules` была бы ложноположительной. subprocess даёт чистый старт.

Родственно будущему `tests/test_hermes_isolation.py` (И4/И5): недоступность bot-путей из
headless-контура — предпосылка изоляции инструментов.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Тело зонда: импортируется в чистом процессе, падает assert'ом при утечке bot/aiogram.
_PROBE = """
import sys
import app.bootstrap
import db.session, ads.client, ads.read, ads.mutations, confirm.gate, confirm.store

leaked = sorted(
    m for m in sys.modules
    if m == "bot" or m.startswith("bot.") or m == "aiogram" or m.startswith("aiogram.")
)
assert not leaked, "headless-слой подтянул: " + ", ".join(leaked)
assert callable(app.bootstrap.bootstrap_ads_layer)
print("clean")
"""


def test_headless_ads_layer_imports_without_bot() -> None:
    # ENV=dev: тестируем чистоту импорта, не prod-конфиг (в prod core.config fail-fast на пустом
    # whitelist/ключе). PYTHONPATH: subprocess стартует не из pytest-контекста.
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "ENV": "dev", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",  # Windows: иначе cp1251 ломает кириллицу в сообщении assert'а
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"headless-импорт не чист:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "clean" in proc.stdout
