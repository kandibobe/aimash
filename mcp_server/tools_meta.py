"""Credential-free MCP capability contract for diagnostics and flexible clients."""

from __future__ import annotations

from typing import Any, Awaitable, Callable


async def get_bridge_capabilities() -> dict[str, Any]:
    """Return the effective Aimash MCP mode and safety contract without customer data or secrets."""
    from core.config import settings
    from core.killswitch import mutations_disabled
    from mcp_server.tools_plan import PLAN_STATE_MCP_TOOLS
    from mcp_server.tools_read import READ_MCP_TOOLS
    from mcp_server.tools_write import EXECUTE_MCP_TOOLS, PROPOSE_MCP_TOOLS

    write_enabled = bool(settings.hermes_write_enabled)
    stop_reason = mutations_disabled()
    return {
        "bridge": "aimash",
        "contract_version": "2026-07-31.2",
        "mode": "read_plan_write" if write_enabled else "read_only",
        "surface": {
            "read": len(READ_MCP_TOOLS) + len(META_MCP_TOOLS),
            "plan": len(PROPOSE_MCP_TOOLS | PLAN_STATE_MCP_TOOLS) if write_enabled else 0,
            "write": len(EXECUTE_MCP_TOOLS) if write_enabled else 0,
        },
        "safety": {
            "trusted_telegram_hmac": write_enabled,
            "confirm_cas_required": write_enabled,
            "account_lock_required": True,
            "kill_switch_active": stop_reason is not None,
            "external_content_marking": True,
            "external_content_phase_lock": False,
            "one_proposal_per_turn": write_enabled,
        },
        "transport": "stdio",
        "error": None,
        "error_code": None,
    }


META_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "get_bridge_capabilities": get_bridge_capabilities,
}
META_MCP_TOOLS: frozenset[str] = frozenset(META_TOOL_FUNCS)
