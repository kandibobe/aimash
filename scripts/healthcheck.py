"""B9: Docker HEALTHCHECK — свежесть heartbeat (живость event-loop бота), не просто импорт модулей.

exit 0 — heartbeat свежий (бот жив); exit 1 — файл протух/отсутствует (зависший polling / крэш-луп).
Оркестратор перезапускает контейнер по unhealthy. Дёшево, без секретов в выводе.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.heartbeat import HEARTBEAT_MAX_AGE_S, heartbeat_path, is_fresh  # noqa: E402


def main() -> int:
    if is_fresh():
        return 0
    p = heartbeat_path()
    reason = "файл отсутствует" if not p.exists() else f"протух (>{HEARTBEAT_MAX_AGE_S:.0f}s)"
    print(f"unhealthy: heartbeat {reason} ({p})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
