"""Диагностический MCP-мини-сервер `aimash-probe` — ОДИН инструмент `probe_echo`, ноль доступа к Ads.

Зачем. Все утверждения о рантайме Hermes (доходят ли доверенные метаданные Telegram до нашего кода,
может ли хук переписать аргумент, что делает gateway при падении хука, каков фактический таймаут) —
**[Непроверено]**: `deploy/hermes/HERMES_SPEC.md:1234` сам держит их в списке «проверить на живой
установке». Проверяются они шагами V1–V19 (`deploy/hermes/OPERATIONS.md` §12), а этот сервер — их
измерительный прибор со стороны MCP: он возвращает РОВНО то, что до него доехало.

Почему ОТДЕЛЬНЫЙ сервер, а не 13-й инструмент в `mcp_server.server`:
  • `probe_echo` не READ-инструмент Google Ads; в `READ_TOOL_FUNCS` (`mcp_server/tools_read.py:408`)
    ему не место — он не проходит замок чтения, потому что читать ему нечего;
  • `tests/test_hermes_isolation.py:70` требует `len(READ_MCP_TOOLS) == 12` — регистрация здесь этот
    инвариант не трогает, что и нужно: прибор не должен менять измеряемую систему;
  • отдельный процесс = отдельная строка `mcp_servers` в `~/.hermes/config.yaml`, которую после
    V-прогона **удаляют**, не трогая боевую `aimash`.

Границы (умышленные, не заготовка на будущее):
  • НЕ поднимает ads-слой (никакого `lifespan`/`bootstrap_ads_layer`), НЕ ходит в БД, НЕ импортирует
    `ads.*` — у прибора нет доступа ни к деньгам, ни к OAuth. Живёт на одном `core.logging`;
  • эхо идёт через `redact_text` (правило 5): в аргументы может прилететь всё что угодно, включая
    подставленное хуком из окружения — сырым наружу это уходить не должно;
  • `probe_echo` не мутирует ничего и не создаёт черновиков — в том числе поэтому его безопасно
    оставить видимым модели на время замеров.

Запуск: `python -m mcp_server.probe` (stdio, как боевой сервер; stdout занят JSON-RPC → логи в stderr).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from core.logging import redact_text, setup_logging

# Потолок искусственной задержки. Нужен для V13 — замера, что Hermes реально делает с долгим
# инструментом при `timeout: 120` / `connect_timeout: 30` в конфиге (боевой `get_account_audit`
# гоняет 27 фетчеров и в эти 120 с упирается вполне буднично). Ограничение — чтобы прибор нельзя
# было превратить в подвисание gateway'я на час.
_MAX_DELAY_SECONDS = 300


def _clamp_delay(value: Any) -> int:
    """Задержка в [0, _MAX_DELAY_SECONDS]; мусор → 0. Отдельной функцией, чтобы потолок проверялся
    тестом без реального сна на пять минут."""
    try:
        return max(0, min(_MAX_DELAY_SECONDS, int(value)))
    except (TypeError, ValueError):
        return 0


def _echo_value(value: Any) -> Any:
    """Значение как его увидел MCP-слой, но безопасное наружу: строки — через `redact_text`,
    контейнеры — рекурсивно, остальное — как есть (числа/bool/None секретов не несут)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): _echo_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_echo_value(v) for v in value]
    return value


async def probe_echo(
    note: str = "",
    actor_chat_id: str = "",
    reply_to_message_id: str = "",
    delay_seconds: int = 0,
) -> dict[str, Any]:
    """Диагностика транспорта: возвращает ровно те аргументы, что доехали до MCP-сервера.

    Мутаций не выполняет, данных Google Ads не читает. Используется для проверки того, какие поля
    доходят до инструмента и кем они заполнены (моделью или доверенным каналом рантайма).

    Args:
        note: произвольная пометка вызывающего (что именно измеряем этим вызовом).
        actor_chat_id: id чата-инициатора, если его подставляет доверенный канал.
        reply_to_message_id: id сообщения, на которое отвечают, если его подставляет доверенный канал.
        delay_seconds: искусственная задержка ответа в секундах (0–300) — замер таймаутов.
    """
    started = time.monotonic()
    delay = _clamp_delay(delay_seconds)
    if delay:
        await asyncio.sleep(delay)

    received = {
        "note": _echo_value(note),
        "actor_chat_id": _echo_value(actor_chat_id),
        "reply_to_message_id": _echo_value(reply_to_message_id),
        "delay_seconds": delay,
    }
    # Пустая строка и отсутствие ключа — РАЗНЫЕ исходы V8/V9: «хук не подставил ничего» против
    # «хук подставил пустое». Дефолты аргументов затирают разницу, поэтому фиксируем отдельно, какие
    # поля пришли непустыми — по этому полю и читается протокол замера.
    filled = sorted(k for k, v in received.items() if k != "delay_seconds" and v not in ("", None))
    return {
        "probe": "aimash-probe",
        "received": received,
        "filled_fields": filled,
        # pid позволяет отличить «Hermes поднял новый процесс MCP на каждый вызов» от долгоживущего:
        # от этого зависит, можно ли вообще держать таинт И7 в памяти процесса (Волна 3.5 — нельзя).
        "server_pid": os.getpid(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "error": None,
    }


def build_probe_server():
    """FastMCP с единственным инструментом. Имя сервера — `aimash-probe`, отдельное от боевого
    `aimash`: в `~/.hermes/config.yaml` это отдельная запись `mcp_servers`, удаляемая после замеров."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("aimash-probe")
    mcp.tool(name="probe_echo", structured_output=False)(probe_echo)
    return mcp


def main() -> None:
    setup_logging()  # stdout занят JSON-RPC → логи только в stderr (редакция секретов включена)
    build_probe_server().run(transport="stdio")


if __name__ == "__main__":
    main()
