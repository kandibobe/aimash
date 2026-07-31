"""Ready-dark FastMCP PLAN-сервер: proposal/state tools, без исполнения Google Ads."""

from __future__ import annotations

from core.guards import require_registered_surface
from mcp_server.server import _lifespan, _registered_tool_names
from mcp_server.tools_plan import PLAN_MCP_TOOLS, PLAN_TOOL_FUNCS


def build_plan_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("aimash-plan", lifespan=_lifespan)
    for name, fn in PLAN_TOOL_FUNCS.items():
        mcp.tool(name=name, structured_output=False)(fn)

    require_registered_surface(
        _registered_tool_names(mcp),
        PLAN_MCP_TOOLS,
        subject="mcp_server.plan_server.build_plan_server",
    )
    return mcp


def main() -> None:
    build_plan_server().run(transport="stdio")


if __name__ == "__main__":
    main()
