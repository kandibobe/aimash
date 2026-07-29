"""FastMCP-реестр READ-инструментов дашборда Hermes + барьер поверхности.

Тот же паттерн, что в [mcp_server/server.py](../mcp_server/server.py): после регистрации сверяем
ФАКТИЧЕСКУЮ поверхность FastMCP с одобренным набором через `core.guards.require_registered_surface`
(равенство, не «⊆»). Проверка читает реестр FastMCP, а не `HERMES_TOOL_FUNCS`, — иначе она
тавтологична и не увидела бы регистрацию мимо набора.

Lifespan здесь нет: слой не поднимает ни БД, ни ads-контур — только HTTP-клиент на вызов.
"""

from __future__ import annotations

from core.guards import require_registered_surface
from hermes_ops.tools import HERMES_READ_TOOLS, HERMES_TOOL_FUNCS


def _registered_tool_names(mcp) -> frozenset[str]:
    """Имена, которые FastMCP ФАКТИЧЕСКИ зарегистрировал. Не прочитали реестр — бросаем (правило 10):
    непроверяемая поверхность хуже отсутствующей. `_tool_manager.list_tools()` — внутренний синхронный
    аксессор FastMCP (публичного sync-листинга нет); сменится API — падаем громко здесь и в
    `tests/test_hermes_ops_surface.py`, а не тихо пропускаем гейт."""
    tm = getattr(mcp, "_tool_manager", None)
    if tm is None or not hasattr(tm, "list_tools"):
        raise RuntimeError(
            "не удалось прочитать реестр инструментов FastMCP (_tool_manager.list_tools) — "
            "поверхность непроверяема, отказываю (fail-closed)."
        )
    return frozenset(t.name for t in tm.list_tools())


def build_server():
    """FastMCP `hermes` с 12 READ-инструментами. `structured_output=False` — конверт отдаём JSON-текстом,
    без навязанной output-схемы из generic `-> dict` (как в `mcp_server`)."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("hermes")
    for name, fn in HERMES_TOOL_FUNCS.items():
        mcp.tool(name=name, structured_output=False)(fn)
    require_registered_surface(
        _registered_tool_names(mcp),
        HERMES_READ_TOOLS,
        subject="hermes_ops.server.build_server (живая MCP-поверхность дашборда Hermes)",
    )
    return mcp
