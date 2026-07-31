"""Selective live-config sync must preserve host-local settings and secrets."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._docs_paths import ROOT

PATH = ROOT / "deploy/hermes/sync_aimash_surface.py"
SPEC = importlib.util.spec_from_file_location("sync_aimash_surface", PATH)
assert SPEC is not None and SPEC.loader is not None
SYNC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SYNC
SPEC.loader.exec_module(SYNC)


def _config():
    return {
        "model": {"provider": "host-local", "default": "host-model"},
        "dashboard": {"secret": "must-survive"},
        "plugins": {"enabled": ["other"], "disabled": []},
        "mcp_servers": {
            "aimash": {
                "command": "docker",
                "tools": {"include": ["old"], "exclude": ["never-touch"]},
            }
        },
    }


def test_enable_changes_only_plugin_and_aimash_include():
    cfg = SYNC.reconcile_config(_config(), enabled=True, tools=["read", "execute_confirmed"])
    assert cfg["model"] == {"provider": "host-local", "default": "host-model"}
    assert cfg["dashboard"]["secret"] == "must-survive"
    assert cfg["plugins"] == {
        "enabled": ["other", "aimash_trusted_transport"],
        "disabled": [],
    }
    assert cfg["mcp_servers"]["aimash"]["tools"] == {
        "include": ["read", "execute_confirmed"],
        "exclude": ["never-touch"],
    }


def test_disable_removes_plugin_and_keeps_read_manifest():
    cfg = _config()
    cfg["plugins"] = {
        "enabled": ["other", "aimash_trusted_transport"],
        "disabled": ["aimash_trusted_transport"],
    }
    got = SYNC.reconcile_config(cfg, enabled=False, tools=["read"])
    assert got["plugins"]["enabled"] == ["other"]
    assert got["plugins"]["disabled"] == ["aimash_trusted_transport"]
    assert got["mcp_servers"]["aimash"]["tools"]["include"] == ["read"]


def test_sanitize_pinned_config_removes_only_proven_inert_keys():
    cfg = {
        "model": {"provider": "openai-codex", "default": "gpt-5.6"},
        "model_routing": {"r1": {"provider": "openai-codex", "model": "gpt-5.4"}},
        "browser": {"cloud_provider": "local", "use_gateway": False},
        "openrouter": {"extra_headers": "ignored"},
        "tool_loop_guardrails": {
            "warnings_enabled": True,
            "hard_stop_after": {"exact_failure": 3},
            "exact_failure": 3,
            "same_tool_failure": 4,
            "idempotent_no_progress": 3,
        },
        "agent": {"disabled_toolsets": ["delegation", "terminal"]},
        "delegation": {"provider": "openai-codex", "model": "gpt-5.6"},
        "dashboard": {"theme": "dark"},
    }

    result = SYNC.sanitize_pinned_config(cfg)

    assert result["model"] == {"provider": "openai-codex", "default": "gpt-5.6"}
    assert "model_routing" not in result
    assert result["browser"] == {"cloud_provider": "local"}
    assert "openrouter" not in result
    assert result["tool_loop_guardrails"] == {
        "warnings_enabled": True,
        "hard_stop_after": {"exact_failure": 3},
    }
    assert "delegation" not in result
    assert result["dashboard"] == {"theme": "dark"}


def test_env_parser_never_needs_to_source_shell(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("# x\nexport AIMASH_TRUST_HMAC_KEY='" + "k" * 32 + "'\n", encoding="utf-8")
    assert SYNC._env_value(env, "AIMASH_TRUST_HMAC_KEY") == "k" * 32


def test_atomic_copy_preserves_previous_live_soul(tmp_path: Path):
    source = tmp_path / "source.md"
    target = tmp_path / "SOUL.md"
    source.write_text("merged rules\n", encoding="utf-8")
    target.write_text("live operator rules\n", encoding="utf-8")

    SYNC._atomic_copy(source, target)

    assert target.read_text(encoding="utf-8") == "merged rules\n"
    assert target.with_suffix(".md.aimash-prev").read_text(encoding="utf-8") == (
        "live operator rules\n"
    )
