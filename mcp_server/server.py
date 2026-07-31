"""Production FastMCP READ-реестр + construction-time assert И4 + lifespan-bootstrap.

Импорт этого модуля РОНЯЕТ процесс, если в READ-слой просочилась мутация (assert И4) — это зерно
инварианта изоляции, тот же паттерн, что защищает `ANALYSIS_TOOLS` в `agent/tools/schemas.py`.
`agent.tools.schemas` bot-free (гард `tests/test_hermes_isolation.py` / `test_headless_bootstrap.py`),
поэтому импорт `MUTATION_TOOLS` не нарушает развязку headless-контура.

Полный набор `mcp_server.tools_write` существует ready-dark, но намеренно не импортируется и не
регистрируется этим entrypoint: доверенный Telegram reply-transport ещё не принят. Скрыть WRITE
через клиентский `tools.include` недостаточно — в read-фазе его не должно быть физически (И4).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from agent.tools.schemas import MUTATION_TOOLS
from core.guards import require_no_mutations, require_registered_surface
from mcp_server.tools_read import READ_MCP_TOOLS, READ_TOOL_FUNCS

# ── И4 (зерно): READ-инструменты НЕ пересекаются с мутационными ──────────────────
require_no_mutations(
    READ_MCP_TOOLS, MUTATION_TOOLS, rule="И4", subject="READ-инструменты MCP (READ_MCP_TOOLS)"
)


@asynccontextmanager
async def _lifespan(server):  # noqa: ARG001
    """Поднять ads-слой headless НА ТОМ ЖЕ event loop, что и сервер."""
    from app.bootstrap import bootstrap_ads_layer

    await bootstrap_ads_layer()
    yield {}


def _registered_tool_names(mcp) -> frozenset[str]:
    """Имена, которые FastMCP ФАКТИЧЕСКИ зарегистрировал. Fail-closed."""
    tm = getattr(mcp, "_tool_manager", None)
    if tm is None or not hasattr(tm, "list_tools"):
        raise RuntimeError(
            "не удалось прочитать реестр инструментов FastMCP (_tool_manager.list_tools) — "
            "поверхность непроверяема, отказываю (§15.2, fail-closed)."
        )
    return frozenset(t.name for t in tm.list_tools())


def build_server():
    """FastMCP с зарегистрированным ровно READ-набором.

    §15.2: после регистрации сверяем ФАКТИЧЕСКУЮ поверхность с одобренным набором.
    Любой проскочивший инструмент мимо реестра → старт падает fail-fast."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("aimash", lifespan=_lifespan)

    # READ-инструменты
    for name, fn in READ_TOOL_FUNCS.items():
        mcp.tool(name=name, structured_output=False)(fn)

    # Проверка на РАВЕНСТВО: живая поверхность — только production READ.
    require_registered_surface(
        _registered_tool_names(mcp),
        READ_MCP_TOOLS,
        subject="mcp_server.server.build_server (живая MCP-поверхность)",
    )
    return mcp
