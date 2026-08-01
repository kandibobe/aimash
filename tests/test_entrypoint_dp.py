"""Регрессия prod-инцидента 2026-07-03 «бот принимает сообщения, но отвечает не так».

Два симптома одной причины — запуск `python -m bot.main` исполняет bot/main.py как модуль
`__main__`, а хендлеры в bot/handlers/* делают `import bot.main as bm`:

1. Без алиаса sys.modules Python не находит 'bot.main' и ИСПОЛНЯЕТ файл повторно вторым
   модулем → ДВА разных Dispatcher, main() поллит пустой → все апдейты «не обработаны»,
   бот молчит.
2. Тот же циклический ре-импорт может создать неверный порядок регистрации. В agent-first схеме
   первыми обязаны стоять `/newcampaign`-alias и non-command `on_text`; slash-команды исключены
   фильтром самого `on_text`.

Фикс — алиас `sys.modules.setdefault("bot.main", sys.modules["__main__"])` в bot/main.py (до
импорта хендлеров). Обычные тесты этого не ловят: они `import bot.main` (как bot.main, не
__main__) и видят один dp в правильном порядке. Поэтому тест запускает СВЕЖИЙ процесс и
воспроизводит именно `python -m bot.main` (runpy, run_name='__main__'), БЕЗ реального polling.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# Свежий процесс = точная имитация `python -m bot.main`: bot.main НЕ импортирован заранее,
# поэтому алиас sys.modules реально решает, один dp или два. asyncio.run патчим, чтобы main()
# не ушёл в polling. Затем читаем dp, который РЕАЛЬНО будет поллиться (__main__.dp).
_CHILD = r"""
import asyncio, runpy
asyncio.run = lambda coro, *a, **k: (coro.close(), None)[1]  # не запускаем polling
runpy.run_module("bot.main", run_name="__main__", alter_sys=True)
import bot.main as bm  # с фиксом это ТОТ ЖЕ модуль (__main__), что поллит main()
names = [h.callback.__name__ for h in bm.dp.message.handlers]
assert names, "dp.message пуст — double-import создал второй пустой Dispatcher"
assert names[:2] == ["newcampaign_react", "on_text"], names[:5]
react = next(h for h in bm.dp.message.handlers if h.callback.__name__ == "on_text")
assert react.filters[0].callback(type("M", (), {"text": "/start"})()) is False
print("ENTRYPOINT_OK handlers=%d" % len(names))
"""


def test_python_m_botmain_polls_populated_dp_in_order():
    env = dict(os.environ, PYTHONPATH=str(_REPO), PYTHONIOENCODING="utf-8")
    res = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=str(_REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert res.returncode == 0, (
        "`python -m bot.main` регистрирует хендлеры неверно "
        f"(double-import).\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )
    assert "ENTRYPOINT_OK" in res.stdout, res.stdout
