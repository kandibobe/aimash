#!/usr/bin/env python3
"""Atomically reconcile the Aimash surface and pinned trusted-operator policy on a live host.

The live config contains host-local dashboard settings and secrets, so deploying the repo template
wholesale is unsafe. This script preserves those host-local values, derives the exact tool surface
from a one-shot ``mcp`` compose service, installs the repository plugin, and pins the approved
model/private-team/context policy so a dashboard edit or deploy cannot drift it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

PLUGIN_NAME = "aimash_trusted_transport"
PRIMARY_PROVIDER = "openai-codex"
PRIMARY_MODEL = "gpt-5.6-terra"
PRIMARY_REASONING_EFFORT = "medium"
REASONING_OVERRIDES = {
    "gpt-5.4-mini": "low",
    "gpt-5.6-sol": "high",
}
PRIMARY_MAX_TURNS = 20
DELEGATION_MODEL = "gpt-5.6-sol"
DELEGATION_REASONING_EFFORT = "high"
RESTART_DRAIN_TIMEOUT = 180
DELEGATION_MAX_ITERATIONS = 30
COMPRESSION_POLICY = {
    "enabled": True,
    "threshold": 0.15,
    "target_ratio": 0.10,
    "protect_last_n": 12,
    "protect_first_n": 0,
}
SESSION_RESET_POLICY = {"mode": "idle", "idle_minutes": 1440, "notify": True}
SESSIONS_POLICY = {
    "auto_prune": True,
    "retention_days": 30,
    "vacuum_after_prune": True,
    "min_interval_hours": 24,
}
AUXILIARY_POLICY = {
    "transient_retries": 2,
    "vision": {"provider": "openai-codex", "model": "gpt-5.4"},
    "web_extract": {"provider": "openai-codex", "model": "gpt-5.4"},
    "compression": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    "approval": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    "title_generation": {
        "enabled": True,
        "provider": "openai-codex",
        "model": "gpt-5.4",
    },
    "memory_query_rewrite": {"provider": "openai-codex", "model": "gpt-5.4"},
    "curator": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
}
CANONICAL_SKILLS = (
    "operational-coordinator",
    "google-ads-worker",
    "ad-master-agent",
    "creative-director",
    "paid-social-advisor",
)
RUNTIME_SKILL_ROLES = {
    "operational-coordinator": "forum operations and triage",
    "google-ads-worker": "Google Ads execution",
    "ad-master-agent": "delegated deep Google Ads analysis",
    "creative-director": "Google Ads creative intent",
    "paid-social-advisor": "Meta and TikTok advisory",
}
TELEGRAM_GROUP_TOPICS = (
    {
        "chat_id": "-1004443550627",
        "topics": (
            {"name": "general", "thread_id": 1, "skill": "operational-coordinator"},
            {"name": "google-ads", "thread_id": 153, "skill": "google-ads-worker"},
            {"name": "meta-ads", "thread_id": 154, "skill": "paid-social-advisor"},
            {"name": "tiktok-ads", "thread_id": 155, "skill": "paid-social-advisor"},
            {"name": "approvals-and-audits", "thread_id": 156, "skill": "operational-coordinator"},
            {"name": "development", "thread_id": 1992, "skill": "operational-coordinator"},
            {"name": "files", "thread_id": 2135, "skill": "operational-coordinator"},
            {"name": "alerts", "thread_id": 2163, "skill": "operational-coordinator"},
            {"name": "tasks", "thread_id": 2164, "skill": "operational-coordinator"},
        ),
    },
)
ARTIFACT_ARCHIVE_ENV = {
    "AIMASH_ARTIFACT_ARCHIVE_CHAT_ID": "-1004443550627",
    "AIMASH_ARTIFACT_ARCHIVE_THREAD_ID": "2135",
}
RETIRED_SKILLS = (
    "ad-master-tools",
    "admaster-confirm-model",
    "google-ads-safety",
)
TRUSTED_OPERATOR_DISABLED_TOOLSETS = (
    "homeassistant",
    "spotify",
    "video_gen",
    "x_search",
    "yuanbao",
    "tts",
)
TELEGRAM_TOOLSETS = (
    "clarify",
    "cronjob",
    "delegation",
    "memory",
    "skills",
    "todo",
    "vision",
    "web",
)
TELEGRAM_DISABLED_SKILLS = (
    "ad-master-context",
    "ad-master-cron-ops",
    "ad-master-routing",
    "ad-master-self-learning",
    "admaster-operations",
    "aimash-architecture",
    "aimash-development",
    "quant",
)
TOOL_LOOP_GUARDRAILS = {
    "warnings_enabled": True,
    "hard_stop_enabled": True,
    "hard_stop_after": {
        "exact_failure": 3,
        "same_tool_failure": 4,
        "idempotent_no_progress": 3,
    },
}
TOOL_SEARCH_POLICY = {
    "enabled": "off",
    "threshold_pct": 5,
    "search_default_limit": 5,
    "max_search_limit": 20,
}
KANBAN_SAFE_POLICY = {
    "dispatch_in_gateway": False,
    "auto_decompose": False,
    "max_in_progress": 2,
    "max_in_progress_per_profile": 1,
}
AIMASH_MCP_COMMAND = "/bin/sh"
AIMASH_MCP_ARGS = ("/opt/aimash/scripts/run_hermes_mcp.sh",)


def reconcile_trusted_operator_policy(config: dict[str, Any]) -> dict[str, Any]:
    """Pin the owner-approved private-team tool, memory, delegation and skill policy."""
    _validate_runtime_skill_policy()
    config["model"] = {
        "provider": PRIMARY_PROVIDER,
        "default": PRIMARY_MODEL,
    }
    agent = config.setdefault("agent", {})
    if not isinstance(agent, dict):
        raise RuntimeError("live Hermes config: agent must be a mapping")
    agent["max_turns"] = PRIMARY_MAX_TURNS
    agent["restart_drain_timeout"] = RESTART_DRAIN_TIMEOUT
    agent["reasoning_effort"] = PRIMARY_REASONING_EFFORT
    # Replace stale provider overrides with the exact Codex-only cost/quality lanes.
    agent["reasoning_overrides"] = dict(REASONING_OVERRIDES)
    agent["disabled_toolsets"] = list(TRUSTED_OPERATOR_DISABLED_TOOLSETS)

    memory = config.setdefault("memory", {})
    if not isinstance(memory, dict):
        raise RuntimeError("live Hermes config: memory must be a mapping")
    memory["memory_enabled"] = True
    memory["user_profile_enabled"] = True

    config["compression"] = dict(COMPRESSION_POLICY)
    config["auxiliary"] = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in AUXILIARY_POLICY.items()
    }
    # Do not silently send client/Ads context to a third-party provider when Codex is unavailable.
    # An unavailable Codex runtime must fail visibly; the owner can decide whether to retry later.
    config["fallback_providers"] = []
    config["session_reset"] = dict(SESSION_RESET_POLICY)
    config["sessions"] = dict(SESSIONS_POLICY)

    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        raise RuntimeError("live Hermes config: skills must be a mapping")
    skills.update(
        {
            "inline_shell": False,
            "guard_agent_created": True,
            "write_approval": True,
        }
    )
    platform_disabled = skills.setdefault("platform_disabled", {})
    if not isinstance(platform_disabled, dict):
        raise RuntimeError("live Hermes config: skills.platform_disabled must be a mapping")
    platform_disabled["telegram"] = list(TELEGRAM_DISABLED_SKILLS)

    gateway = config.setdefault("gateway", {})
    platforms = gateway.setdefault("platforms", {}) if isinstance(gateway, dict) else None
    telegram = platforms.setdefault("telegram", {}) if isinstance(platforms, dict) else None
    if not isinstance(telegram, dict):
        raise RuntimeError("live Hermes config: gateway.platforms.telegram must be a mapping")
    telegram["gateway_restart_notification"] = False
    extra = telegram.setdefault("extra", {})
    if not isinstance(extra, dict):
        raise RuntimeError("live Hermes config: gateway.platforms.telegram.extra must be a mapping")
    extra["group_topics"] = json.dumps(TELEGRAM_GROUP_TOPICS, ensure_ascii=False)

    approvals = config.setdefault("approvals", {})
    if not isinstance(approvals, dict):
        raise RuntimeError("live Hermes config: approvals must be a mapping")
    approvals["mode"] = "manual"
    approvals["cron_mode"] = "deny"

    config["tool_loop_guardrails"] = {
        "warnings_enabled": TOOL_LOOP_GUARDRAILS["warnings_enabled"],
        "hard_stop_enabled": TOOL_LOOP_GUARDRAILS["hard_stop_enabled"],
        "hard_stop_after": dict(TOOL_LOOP_GUARDRAILS["hard_stop_after"]),
    }
    tools = config.setdefault("tools", {})
    if not isinstance(tools, dict):
        raise RuntimeError("live Hermes config: tools must be a mapping")
    tools["tool_search"] = dict(TOOL_SEARCH_POLICY)
    # The production profile is not a Kanban worker. Keep the dispatcher off until a separately
    # installed READ-only profile has passed the golden trace corpus.
    config["kanban"] = dict(KANBAN_SAFE_POLICY)
    platform_toolsets = config.setdefault("platform_toolsets", {})
    if not isinstance(platform_toolsets, dict):
        raise RuntimeError("live Hermes config: platform_toolsets must be a mapping")
    platform_toolsets["telegram"] = list(TELEGRAM_TOOLSETS)

    config["delegation"] = {
        "model": DELEGATION_MODEL,
        "provider": PRIMARY_PROVIDER,
        "inherit_mcp_toolsets": True,
        "max_iterations": DELEGATION_MAX_ITERATIONS,
        "reasoning_effort": DELEGATION_REASONING_EFFORT,
        "max_concurrent_children": 3,
        "max_spawn_depth": 1,
        "orchestrator_enabled": True,
        "subagent_auto_approve": True,
    }
    return config


def _validate_runtime_skill_policy() -> None:
    """Fail deployment if a Telegram topic could select an undeployed skill."""
    canonical = set(CANONICAL_SKILLS)
    if canonical != set(RUNTIME_SKILL_ROLES):
        raise RuntimeError("runtime skill roles must describe every canonical skill exactly once")
    mapped = [topic["skill"] for group in TELEGRAM_GROUP_TOPICS for topic in group["topics"]]
    unknown = sorted(set(mapped) - canonical)
    if unknown:
        raise RuntimeError(f"Telegram topic references undeployed skills: {', '.join(unknown)}")


def sanitize_pinned_config(config: dict[str, Any]) -> dict[str, Any]:
    """Remove keys proven inert in pinned Hermes v0.19.0, preserving host-local choices."""
    config.pop("model_routing", None)

    browser = config.get("browser")
    if isinstance(browser, dict):
        browser.pop("use_gateway", None)

    openrouter = config.get("openrouter")
    if isinstance(openrouter, dict):
        openrouter.pop("extra_headers", None)
        if not openrouter:
            config.pop("openrouter", None)

    guardrails = config.get("tool_loop_guardrails")
    if isinstance(guardrails, dict):
        for key in ("exact_failure", "same_tool_failure", "idempotent_no_progress"):
            guardrails.pop(key, None)

    disabled = config.get("agent", {}).get("disabled_toolsets", [])
    if isinstance(disabled, list) and "delegation" in {str(item) for item in disabled}:
        # Preserve the operator's disabled-toolset decision; remove only the inert config block.
        config.pop("delegation", None)
    return config


def _compose_surface(project_directory: Path) -> dict[str, Any]:
    code = (
        "import hashlib,json;"
        "from core.config import settings;"
        "from mcp_server.server import expected_tool_names;"
        "key=settings.aimash_trust_hmac_key.get_secret_value().encode();"
        "print(json.dumps({'enabled':settings.hermes_write_enabled,"
        "'tools':sorted(expected_tool_names()),"
        "'key_sha256':hashlib.sha256(key).hexdigest() if key else ''}))"
    )
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(project_directory),
            "--profile",
            "mcp",
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "mcp",
            "python",
            "-c",
            code,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in reversed(proc.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("tools"), list):
            return payload
    raise RuntimeError("one-shot mcp service did not return a valid surface manifest")


def _env_value(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            return value
    return ""


def _set_env_values(path: Path, values: dict[str, str]) -> None:
    """Atomically update only checked, non-secret operator settings."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(values)
    output: list[str] = []
    for line in lines:
        match = re.match(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)=", line)
        key = match.group(2) if match else None
        if key in pending:
            output.append(f"{match.group(1)}{key}={pending.pop(key)}")
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in pending.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(output) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def reconcile_config(config: dict[str, Any], *, enabled: bool, tools: list[str]) -> dict[str, Any]:
    """Pin the current Aimash transport plus its plugin activation and include-list."""
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise RuntimeError("live Hermes config: plugins must be a mapping")
    enabled_plugins = [str(x) for x in plugins.get("enabled") or [] if str(x) != PLUGIN_NAME]
    disabled_plugins = [str(x) for x in plugins.get("disabled") or [] if str(x) != PLUGIN_NAME]
    if enabled:
        enabled_plugins.append(PLUGIN_NAME)
    else:
        disabled_plugins.append(PLUGIN_NAME)
    plugins["enabled"] = enabled_plugins
    plugins["disabled"] = disabled_plugins

    servers = config.get("mcp_servers")
    if not isinstance(servers, dict) or not isinstance(servers.get("aimash"), dict):
        raise RuntimeError("live Hermes config has no mcp_servers.aimash mapping")
    aimash = servers["aimash"]
    aimash["command"] = AIMASH_MCP_COMMAND
    aimash["args"] = list(AIMASH_MCP_ARGS)
    tool_cfg = aimash.setdefault("tools", {})
    if not isinstance(tool_cfg, dict):
        raise RuntimeError("live Hermes config: mcp_servers.aimash.tools must be a mapping")
    tool_cfg["include"] = list(tools)
    return config


def _install_plugin(source: Path, target: Path) -> None:
    if not (source / "plugin.yaml").is_file() or not (source / "__init__.py").is_file():
        raise RuntimeError(f"trusted plugin source is incomplete: {source}")
    for name in ("plugin.yaml", "__init__.py"):
        _atomic_copy(source / name, target / name)


def _sync_skills(source_root: Path, target_root: Path, retired_root: Path) -> None:
    """Install the SPEC-aligned topic skill and move proven-conflicting duplicates out of discovery.

    Retiring is recoverable: old folders move outside ``~/.hermes/skills`` and remain in the normal
    Hermes backup. We do not touch unrelated skills because topic/cron references are host state.
    """
    canonical_sources = [source_root / name / "SKILL.md" for name in CANONICAL_SKILLS]
    for source in canonical_sources:
        if not source.is_file():
            raise RuntimeError(f"canonical Hermes skill is absent: {source}")
    for name in RETIRED_SKILLS:
        source = target_root / name
        if source.exists() and not (source / "SKILL.md").is_file():
            raise RuntimeError(f"retired Hermes skill is malformed: {source}")

    for name, source in zip(CANONICAL_SKILLS, canonical_sources, strict=True):
        _atomic_copy(source, target_root / name / "SKILL.md")

    retired_root.mkdir(parents=True, exist_ok=True)
    for name in RETIRED_SKILLS:
        source = target_root / name
        if not source.exists():
            continue
        digest = hashlib.sha256((source / "SKILL.md").read_bytes()).hexdigest()[:12]
        destination = retired_root / f"{name}-{digest}"
        suffix = 2
        while destination.exists():
            destination = retired_root / f"{name}-{digest}-{suffix}"
            suffix += 1
        shutil.move(str(source), str(destination))


def _atomic_copy(source: Path, target: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"deployment source is absent: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_suffix(target.suffix + ".aimash-prev")
    if target.exists():
        shutil.copy2(target, backup)
        os.chmod(backup, 0o600)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    tmp = Path(raw_tmp)
    os.close(fd)
    try:
        shutil.copyfile(source, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _atomic_yaml(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + ".aimash-prev")
    if path.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path.home() / ".hermes/config.yaml")
    parser.add_argument("--hermes-env", type=Path, default=Path.home() / ".hermes/.env")
    parser.add_argument("--project-directory", type=Path, default=Path("/opt/aimash"))
    parser.add_argument(
        "--plugin-source",
        type=Path,
        default=Path(__file__).resolve().parent / "plugins" / PLUGIN_NAME,
    )
    parser.add_argument(
        "--plugin-target",
        type=Path,
        default=Path.home() / ".hermes/plugins" / PLUGIN_NAME,
    )
    parser.add_argument(
        "--soul-source", type=Path, default=Path(__file__).resolve().parent / "SOUL.md"
    )
    parser.add_argument("--soul-target", type=Path, default=Path.home() / ".hermes/SOUL.md")
    parser.add_argument(
        "--skills-source",
        type=Path,
        default=Path(__file__).resolve().parent / "skills" / "ad-master",
    )
    parser.add_argument(
        "--skills-target",
        type=Path,
        default=Path.home() / ".hermes/skills/ad-master",
    )
    parser.add_argument(
        "--retired-skills-target",
        type=Path,
        default=Path.home() / ".hermes/retired-skills/ad-master",
    )
    args = parser.parse_args()

    manifest = _compose_surface(args.project_directory)
    enabled = bool(manifest["enabled"])
    tools = [str(x) for x in manifest["tools"]]
    if enabled:
        host_key = _env_value(args.hermes_env, "AIMASH_TRUST_HMAC_KEY")
        if len(host_key.encode("utf-8")) < 32:
            raise RuntimeError("Hermes trusted transport key is absent or shorter than 32 bytes")
        if hashlib.sha256(host_key.encode()).hexdigest() != manifest.get("key_sha256"):
            raise RuntimeError("Hermes and Aimash trusted transport keys do not match")
    _set_env_values(args.hermes_env, ARTIFACT_ARCHIVE_ENV)

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise RuntimeError("live Hermes config must be a mapping")
    # Validate and compose the complete live config before touching any installed surface file.
    config = reconcile_trusted_operator_policy(sanitize_pinned_config(config))
    config = reconcile_config(config, enabled=enabled, tools=tools)
    if not args.soul_source.is_file():
        raise RuntimeError(f"deployment source is absent: {args.soul_source}")
    for name in CANONICAL_SKILLS:
        source = args.skills_source / name / "SKILL.md"
        if not source.is_file():
            raise RuntimeError(f"canonical Hermes skill is absent: {source}")
    _install_plugin(args.plugin_source, args.plugin_target)
    _atomic_copy(args.soul_source, args.soul_target)
    _sync_skills(args.skills_source, args.skills_target, args.retired_skills_target)
    _atomic_yaml(args.config, config)
    print(
        f"Aimash Hermes surface reconciled: mode={'WRITE' if enabled else 'READ'}, tools={len(tools)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
