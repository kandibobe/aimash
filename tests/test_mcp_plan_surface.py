from mcp_server.plan_server import build_plan_server
from mcp_server.server import _registered_tool_names
from mcp_server.tools_plan import PLAN_MCP_TOOLS
from mcp_server.tools_write import EXECUTE_MCP_TOOLS, PROPOSE_MCP_TOOLS, WRITE_MCP_TOOLS


def test_plan_surface_is_exact_and_cannot_execute():
    registered = _registered_tool_names(build_plan_server())
    assert registered == PLAN_MCP_TOOLS
    assert "execute_confirmed" not in registered
    assert registered.isdisjoint(EXECUTE_MCP_TOOLS)


def test_write_registry_is_split_into_propose_and_execute():
    assert PROPOSE_MCP_TOOLS.isdisjoint(EXECUTE_MCP_TOOLS)
    assert WRITE_MCP_TOOLS == PROPOSE_MCP_TOOLS | EXECUTE_MCP_TOOLS
    assert len(PROPOSE_MCP_TOOLS) == 40
    assert EXECUTE_MCP_TOOLS == {"execute_confirmed"}
