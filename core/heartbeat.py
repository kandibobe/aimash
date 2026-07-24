"""B9: heartbeat живости event-loop для честного Docker HEALTHCHECK.

Старый healthcheck проверял только ИМПОРТ модулей (`import core.config, db.session`) — зависший
polling / мёртвый event loop оставался «healthy», и оркестратор не перезапускал крэш-луп. Теперь
фоновая задача на ТОМ ЖЕ event loop, что и polling, периодически трогает heartbeat-файл; healthcheck
проверяет его свежесть. Если loop завис/умер — задача перестаёт писать → файл протухает → unhealthy.

Файл — в HEARTBEAT_FILE (env) или системном temp; писатель (бот) и читатель (scripts/healthcheck)
в одном контейнере видят один путь. Никогда не роняем бот из-за heartbeat (наблюдаемость)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

HEARTBEAT_INTERVAL_S = 10.0  # как часто писатель обновляет файл
HEARTBEAT_MAX_AGE_S = 60.0  # старше — считаем loop зависшим (≥ 3 интервалов запаса)


def heartbeat_path() -> Path:
    """Путь heartbeat-файла: env HEARTBEAT_FILE или <tempdir>/aimash.heartbeat (общий в контейнере)."""
    env = os.getenv("HEARTBEAT_FILE", "").strip()
    return Path(env) if env else Path(tempfile.gettempdir()) / "aimash.heartbeat"


def write_heartbeat() -> None:
    """Обновить heartbeat (epoch внутрь + mtime). Best-effort — не бросает."""
    try:
        heartbeat_path().write_text(str(time.time()), encoding="utf-8")
    except Exception:  # noqa: BLE001 — heartbeat не должен ронять бот
        pass


def is_fresh(max_age_s: float = HEARTBEAT_MAX_AGE_S) -> bool:
    """True, если heartbeat-файл существует и моложе max_age_s (для healthcheck)."""
    try:
        p = heartbeat_path()
        if not p.exists():
            return False
        return (time.time() - p.stat().st_mtime) < max_age_s
    except Exception:  # noqa: BLE001
        return False


async def heartbeat_loop(interval_s: float = HEARTBEAT_INTERVAL_S) -> None:
    """Периодически писать heartbeat, пока задача жива. Крутится на event loop бота — если loop
    завис, эта корутина тоже встаёт и файл протухает (что и детектирует healthcheck)."""
    write_heartbeat()  # сразу на старте (start-period healthcheck)
    while True:
        await asyncio.sleep(interval_s)
        write_heartbeat()
