"""Production FastMCP registry + construction-time guards + lifespan bootstrap.

Импорт этого модуля РОНЯЕТ процесс, если в READ-слой просочилась мутация (assert И4) — это зерно
инварианта изоляции, тот же паттерн, что защищает `ANALYSIS_TOOLS` в `agent/tools/schemas.py`.
`llm.schemas` bot-free (гард `tests/test_hermes_isolation.py` / `test_headless_bootstrap.py`),
поэтому импорт `MUTATION_TOOLS` не нарушает развязку headless-контура.

PLAN/WRITE импортируется и регистрируется только при ``HERMES_WRITE_ENABLED=true``. Каждый такой
callable оборачивается обязательной HMAC-проверкой доверенного Telegram event. При выключенном флаге
модуль ``mcp_server.tools_write`` физически не импортируется (И4); клиентский ``tools.include`` не
считается границей безопасности.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from llm.schemas import MUTATION_TOOLS
from core.config import settings
from core.guards import require_no_mutations, require_registered_surface
from mcp_server.tools_read import READ_MCP_TOOLS, READ_TOOL_FUNCS
from mcp_server.tools_meta import META_MCP_TOOLS, META_TOOL_FUNCS

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


def expected_tool_names() -> frozenset[str]:
    """Authoritative live surface for the configured server mode."""
    if not settings.hermes_write_enabled:
        return READ_MCP_TOOLS | META_MCP_TOOLS
    from mcp_server.tools_write import PLAN_WRITE_MCP_TOOLS
    from mcp_server.tools_plan import PLAN_STATE_MCP_TOOLS

    return READ_MCP_TOOLS | META_MCP_TOOLS | PLAN_WRITE_MCP_TOOLS | PLAN_STATE_MCP_TOOLS


def build_server():
    """FastMCP with exactly the configured READ or READ+trusted PLAN/WRITE surface.

    §15.2: после регистрации сверяем ФАКТИЧЕСКУЮ поверхность с одобренным набором.
    Любой проскочивший инструмент мимо реестра → старт падает fail-fast."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("aimash", lifespan=_lifespan)

    # READ-инструменты
    for name, fn in READ_TOOL_FUNCS.items():
        mcp.tool(name=name, structured_output=False)(fn)
    for name, fn in META_TOOL_FUNCS.items():
        mcp.tool(name=name, structured_output=False)(fn)

    if settings.hermes_write_enabled:
        from mcp_server.tools_plan import PLAN_STATE_TOOL_FUNCS
        from mcp_server.tools_write import PLAN_WRITE_TOOL_FUNCS
        from mcp_server.trusted_transport import trusted_tool

        for name, fn in {**PLAN_WRITE_TOOL_FUNCS, **PLAN_STATE_TOOL_FUNCS}.items():
            mcp.tool(name=name, structured_output=False)(trusted_tool(name, fn))

    # Проверка на РАВЕНСТВО: ни allow-list клиента, ни wrapper не скрывают случайно лишний tool.
    require_registered_surface(
        _registered_tool_names(mcp),
        expected_tool_names(),
        subject="mcp_server.server.build_server (живая MCP-поверхность)",
    )
    return mcp
