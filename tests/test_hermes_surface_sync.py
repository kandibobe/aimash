"""Selective live-config sync must preserve host-local settings and secrets."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

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


def test_surface_sync_model_policy_matches_repository_config():
    config = yaml.safe_load((ROOT / "deploy/hermes/config.yaml").read_text(encoding="utf-8"))

    assert config["model"] == {
        "provider": SYNC.PRIMARY_PROVIDER,
        "default": SYNC.PRIMARY_MODEL,
    }
    assert config["agent"]["max_turns"] == SYNC.PRIMARY_MAX_TURNS
    assert config["agent"]["restart_drain_timeout"] == SYNC.RESTART_DRAIN_TIMEOUT
    assert config["agent"]["reasoning_effort"] == SYNC.PRIMARY_REASONING_EFFORT
    assert config["delegation"]["max_iterations"] == SYNC.DELEGATION_MAX_ITERATIONS
    assert config["platform_toolsets"]["telegram"] == list(SYNC.TELEGRAM_TOOLSETS)
    assert config["skills"]["platform_disabled"]["telegram"] == list(SYNC.TELEGRAM_DISABLED_SKILLS)
    assert config["tool_loop_guardrails"] == SYNC.TOOL_LOOP_GUARDRAILS


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


def test_trusted_operator_policy_is_pinned_without_touching_host_secrets():
    cfg = _config()
    cfg.update(
        {
            "agent": {"disabled_toolsets": ["terminal"]},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "skills": {"inline_shell": True, "custom": "survives"},
        }
    )

    got = SYNC.reconcile_trusted_operator_policy(cfg)

    assert got["model"] == {"provider": "openai-codex", "default": "gpt-5.6-sol"}
    assert got["agent"]["max_turns"] == 40
    assert got["agent"]["restart_drain_timeout"] == 180
    assert got["agent"]["reasoning_effort"] == "high"
    assert got["agent"]["disabled_toolsets"] == list(SYNC.TRUSTED_OPERATOR_DISABLED_TOOLSETS)
    assert got["memory"] == {"memory_enabled": True, "user_profile_enabled": True}
    assert got["skills"] == {
        "inline_shell": False,
        "guard_agent_created": True,
        "write_approval": True,
        "custom": "survives",
        "platform_disabled": {"telegram": list(SYNC.TELEGRAM_DISABLED_SKILLS)},
    }
    assert got["approvals"] == {"mode": "manual", "cron_mode": "deny"}
    assert got["tool_loop_guardrails"] == SYNC.TOOL_LOOP_GUARDRAILS
    assert got["platform_toolsets"]["telegram"] == list(SYNC.TELEGRAM_TOOLSETS)
    assert not {
        "browser",
        "code_execution",
        "computer_use",
        "file",
        "terminal",
    }.intersection(got["platform_toolsets"]["telegram"])
    assert got["delegation"]["provider"] == "openai-codex"
    assert got["delegation"]["model"] == "gpt-5.6-sol"
    assert got["delegation"]["reasoning_effort"] == "high"
    assert got["delegation"]["max_iterations"] == 30
    assert got["delegation"]["orchestrator_enabled"] is True
    assert got["delegation"]["subagent_auto_approve"] is False
    assert got["dashboard"] == {"secret": "must-survive"}


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


def test_skill_sync_replaces_topic_skill_and_retires_conflicting_duplicates(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "skills" / "ad-master"
    retired = tmp_path / "retired"
    for name in SYNC.CANONICAL_SKILLS:
        canonical = source / name
        canonical.mkdir(parents=True)
        canonical.joinpath("SKILL.md").write_text(f"unified {name}\n", encoding="utf-8")
        old_worker = target / name
        old_worker.mkdir(parents=True)
        old_worker.joinpath("SKILL.md").write_text(f"stale {name}\n", encoding="utf-8")
    for name in SYNC.RETIRED_SKILLS:
        folder = target / name
        folder.mkdir(parents=True)
        folder.joinpath("SKILL.md").write_text(f"stale {name}\n", encoding="utf-8")
    unrelated = target / "copywriter"
    unrelated.mkdir(parents=True)
    unrelated.joinpath("SKILL.md").write_text("keep me\n", encoding="utf-8")

    SYNC._sync_skills(source, target, retired)

    for name in SYNC.CANONICAL_SKILLS:
        installed = target / name
        assert installed.joinpath("SKILL.md").read_text(encoding="utf-8") == (f"unified {name}\n")
        assert installed.joinpath("SKILL.md.aimash-prev").read_text(encoding="utf-8") == (
            f"stale {name}\n"
        )
    assert unrelated.joinpath("SKILL.md").read_text(encoding="utf-8") == "keep me\n"
    for name in SYNC.RETIRED_SKILLS:
        assert not (target / name).exists()
        assert len(list(retired.glob(f"{name}-*"))) == 1
