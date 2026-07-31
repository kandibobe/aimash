#!/usr/bin/env python3
"""Atomically reconcile only the Aimash MCP surface and trusted plugin on a live Hermes host.

The live config contains host-local provider/dashboard settings and secrets, so deploying the repo
template wholesale is unsafe.  This script preserves every unrelated key, derives the exact tool
surface from the running ``aimash-bot`` container, installs the repository plugin, and enables it
iff the application-side ``HERMES_WRITE_ENABLED`` flag is true.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

PLUGIN_NAME = "aimash_trusted_transport"


def _container_surface(container: str) -> dict[str, Any]:
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
        ["docker", "exec", "-i", container, "python", "-c", code],
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
    raise RuntimeError("aimash-bot did not return a valid surface manifest")


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


def reconcile_config(config: dict[str, Any], *, enabled: bool, tools: list[str]) -> dict[str, Any]:
    """Mutate only the plugin activation and Aimash include-list."""
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
    tool_cfg = servers["aimash"].setdefault("tools", {})
    if not isinstance(tool_cfg, dict):
        raise RuntimeError("live Hermes config: mcp_servers.aimash.tools must be a mapping")
    tool_cfg["include"] = list(tools)
    return config


def _install_plugin(source: Path, target: Path) -> None:
    if not (source / "plugin.yaml").is_file() or not (source / "__init__.py").is_file():
        raise RuntimeError(f"trusted plugin source is incomplete: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.yaml", "__init__.py"):
        shutil.copy2(source / name, target / name)


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
    parser.add_argument("--container", default="aimash-bot")
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
    args = parser.parse_args()

    manifest = _container_surface(args.container)
    enabled = bool(manifest["enabled"])
    tools = [str(x) for x in manifest["tools"]]
    if enabled:
        host_key = _env_value(args.hermes_env, "AIMASH_TRUST_HMAC_KEY")
        if len(host_key.encode("utf-8")) < 32:
            raise RuntimeError("Hermes trusted transport key is absent or shorter than 32 bytes")
        if hashlib.sha256(host_key.encode()).hexdigest() != manifest.get("key_sha256"):
            raise RuntimeError("Hermes and Aimash trusted transport keys do not match")

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise RuntimeError("live Hermes config must be a mapping")
    _install_plugin(args.plugin_source, args.plugin_target)
    _atomic_copy(args.soul_source, args.soul_target)
    _atomic_yaml(args.config, reconcile_config(config, enabled=enabled, tools=tools))
    print(
        f"Aimash Hermes surface reconciled: mode={'WRITE' if enabled else 'READ'}, tools={len(tools)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
