"""Общий шим кодировки консоли для dev/ops-скриптов.

Windows-консоль часто cp1251 → emoji (✅/⚠️) и кириллица в выводе роняют скрипт
``UnicodeEncodeError``. ``enable_utf8()`` принудительно переводит stdout/stderr в UTF-8
(``errors="replace"`` — не падаем на экзотике). Идемпотентно, без побочных эффектов кроме
реконфигурации потоков.

Раньше этот блок копипастился дословно в 11 скриптах (два слегка разных варианта). Новые
скрипты: ``from _win_console import enable_utf8`` затем ``enable_utf8()`` в начале файла
(каталог ``scripts/`` — первым в ``sys.path`` при запуске ``python scripts/<name>.py``).
"""

from __future__ import annotations

import sys


def enable_utf8() -> None:
    """stdout/stderr → UTF-8 с ``errors="replace"``. Тихо no-op, если поток не TextIOWrapper
    или уже сконфигурирован."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # не TextIOWrapper / уже сконфигурирован
            pass
