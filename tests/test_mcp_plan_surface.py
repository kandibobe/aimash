from mcp_server.plan_server import build_plan_server
from mcp_server.server import _registered_tool_names, build_server, expected_tool_names
from mcp_server.tools_plan import PLAN_MCP_TOOLS
from mcp_server.tools_write import (
    ACTION_MCP_TOOLS,
    COMPOSITE_MCP_TOOLS,
    EXECUTE_MCP_TOOLS,
    WRITE_MCP_TOOLS,
)


def test_plan_surface_is_exact_and_cannot_execute():
    registered = _registered_tool_names(build_plan_server())
    assert registered == PLAN_MCP_TOOLS
    assert "execute_confirmed" not in registered
    assert registered.isdisjoint(EXECUTE_MCP_TOOLS)


def test_write_registry_is_split_into_actions_and_approval_execution():
    from llm.schemas import MUTATION_TOOLS

    assert ACTION_MCP_TOOLS.isdisjoint(EXECUTE_MCP_TOOLS)
    assert WRITE_MCP_TOOLS == ACTION_MCP_TOOLS | COMPOSITE_MCP_TOOLS | EXECUTE_MCP_TOOLS
    assert len(ACTION_MCP_TOOLS) == 42
    assert ACTION_MCP_TOOLS == MUTATION_TOOLS
    assert all(not name.startswith("propose_") for name in ACTION_MCP_TOOLS)
    assert COMPOSITE_MCP_TOOLS == {"composite_change"}
    assert EXECUTE_MCP_TOOLS == {"execute_confirmed"}


def test_reference_config_allowlists_all_direct_actions_without_legacy_prefix():
    from pathlib import Path

    import yaml

    cfg_path = Path(__file__).resolve().parents[1] / "deploy/hermes/config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    include = set(cfg["mcp_servers"]["aimash"]["tools"]["include"])

    assert ACTION_MCP_TOOLS <= include
    assert COMPOSITE_MCP_TOOLS <= include
    assert len(ACTION_MCP_TOOLS) == 42
    assert not {name for name in include if name.startswith("propose_")}


def test_live_write_surface_includes_owned_proposal_state(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "hermes_write_enabled", True)
    expected = expected_tool_names()
    assert {"list_pending_proposals", "cancel_proposal", "execute_confirmed"} <= expected
    assert {"profile_change", "profile_clear"} <= expected
    assert not {name for name in expected if name.startswith("propose_")}
    assert (
        len(expected) == 87
    )  # 27 READ + 1 META + 15 state + 42 actions + 1 composite + 1 approval execute
    assert (
        not {
            "curation_start",
            "curation_state",
            "curation_apply",
            "curation_finalize",
            "search_wizard_start",
            "search_wizard_state",
            "search_wizard_update",
            "search_wizard_finalize",
        }
        & expected
    )
    assert _registered_tool_names(build_server()) == expected
