#!/usr/bin/env python3
"""Host-level Aimash health watcher and Telegram operational notifier.

Runs outside Docker so it can report a dead scheduler/database or Hermes gateway. No secret is printed: the bot token is
read from the untracked env file and used only to construct the Telegram request in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ENV_FILES = (
    Path("/opt/aimash/.env.defaults"),
    Path("/opt/aimash/.env"),
    # The operational supergroup belongs to the Hermes contour.  Keep this last so its
    # TELEGRAM_BOT_TOKEN wins over the separate legacy bot token from /opt/aimash/.env.
    Path("/root/.hermes/.env"),
)
STATE_PATH = Path("/var/lib/aimash-ops-watch/state.json")
HERMES_CRON_JOBS_PATH = Path("/root/.hermes/cron/jobs.json")
CONTAINERS = ("aimash-scheduler", "aimash-pg", "aimash-backup")
# Deliberately duplicated from cron_registry.yaml: the host watcher must run on system Python without
# PyYAML. A contract test keeps this tiny emergency allow-list synchronized with the registry.
CRITICAL_HERMES_CRON_JOBS = {
    "478eac21bfe6": "Hourly Watchdog",
    "5db43f3b3d5d": "Daily Budget Check",
}
_CHAT_ID_RE = re.compile(r"-?[1-9][0-9]{4,19}")
_SEVERITY_RANK = {"info": 0, "success": 0, "warning": 1, "critical": 2}


@dataclass(frozen=True)
class Event:
    severity: str
    text: str


def _read_env(paths: tuple[Path, ...] = ENV_FILES) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    values.update({k: v for k, v in os.environ.items() if v})
    return values


def send_telegram(title: str, body: str, severity: str, *, env: dict[str, str]) -> bool:
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("OPS_ALERT_CHAT_ID", "").strip()
    thread_raw = env.get("OPS_ALERT_THREAD_ID", "").strip()
    if not token or _CHAT_ID_RE.fullmatch(chat_id) is None:
        print("[ops-alert] destination is not configured", file=sys.stderr)
        return False
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": (
            f"<b>{html.escape(severity.upper())}: {html.escape(title)}</b>\n{html.escape(body)}"
        )[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if thread_raw:
        try:
            thread_id = int(thread_raw)
        except ValueError:
            print("[ops-alert] OPS_ALERT_THREAD_ID is invalid", file=sys.stderr)
            return False
        if thread_id <= 0:
            print("[ops-alert] OPS_ALERT_THREAD_ID is invalid", file=sys.stderr)
            return False
        payload["message_thread_id"] = thread_id
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:  # noqa: S310 - fixed Telegram API
            result = json.loads(response.read().decode("utf-8"))
        return bool(result.get("ok"))
    except urllib.error.HTTPError as exc:
        description = "Telegram rejected the request"
        try:
            error = json.loads(exc.read().decode("utf-8"))
            if isinstance(error.get("description"), str):
                description = error["description"][:240]
        except (OSError, UnicodeError, ValueError):
            pass
        print(f"[ops-alert] Telegram HTTP {exc.code}: {description}", file=sys.stderr)
        return False
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"[ops-alert] delivery failed: {type(exc).__name__}", file=sys.stderr)
        return False


def _run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        env=env,
    )
    return "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()


_GIT_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_DEPLOY_CHANGE_LABELS = {
    "feat": "Добавлено",
    "fix": "Исправлено",
    "perf": "Ускорено",
    "docs": "Документация",
}


def _deploy_change_line(subject: str) -> str:
    """Turn one conventional commit subject into a short Russian deploy bullet."""
    clean = " ".join(str(subject).split())[:180]
    match = re.match(r"^(?P<kind>[a-z]+)(?:\([^)]{1,40}\))?!?:\s*(?P<body>.+)$", clean)
    if match:
        label = _DEPLOY_CHANGE_LABELS.get(match.group("kind"), "Изменено")
        clean = match.group("body").strip()
    else:
        label = "Изменено"
    return f"• {label} — {clean}" if clean else ""


def deploy_summary(
    from_sha: str,
    to_sha: str,
    *,
    limit: int = 5,
    run: Callable[..., str] = _run,
) -> str:
    """Describe commits included since the previously deployed revision."""
    start = str(from_sha).strip()
    end = str(to_sha).strip()
    if _GIT_SHA_RE.fullmatch(end) is None:
        raise ValueError("invalid to_sha")
    if start and _GIT_SHA_RE.fullmatch(start) is None:
        raise ValueError("invalid from_sha")
    if start and start != end:
        command = ["git", "log", "--reverse", "--format=%s", f"{start}..{end}"]
    else:
        # First install or an explicit redeploy of the same SHA: showing the whole
        # repository history would be noisy and misleading, so name only this revision.
        command = ["git", "log", "-1", "--format=%s", end]
    output = run(command)
    subjects = [line for line in output.splitlines() if line.strip()]
    if not subjects:
        return "• Изменено — служебное обновление"
    bounded = max(1, int(limit))
    lines = [line for item in subjects[:bounded] if (line := _deploy_change_line(item))]
    remaining = len(subjects) - len(lines)
    if remaining > 0:
        lines.append(f"• Ещё изменений: {remaining}")
    return "\n".join(lines)


def _container_state(name: str, run: Callable[..., str] = _run) -> dict[str, Any]:
    try:
        raw = run(["docker", "inspect", "-f", "{{json .State}}|{{.RestartCount}}", name])
        state_raw, restarts_raw = raw.rsplit("|", 1)
        state = json.loads(state_raw)
        restarts = int(restarts_raw)
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        return {"status": "missing", "health": "missing", "restarts": -1}
    health = (state.get("Health") or {}).get("Status") or "none"
    return {
        "status": str(state.get("Status") or "unknown"),
        "health": str(health),
        "restarts": restarts,
    }


def _systemctl_user_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/0")
    return env


def _recent_conflict_marker(run: Callable[..., str] = _run) -> str:
    chunks: list[str] = []
    commands = (
        [
            "journalctl",
            "--user",
            "-u",
            "hermes-gateway.service",
            "--since",
            "-90 seconds",
            "--no-pager",
        ],
    )
    for command in commands:
        try:
            text = run(command, env=_systemctl_user_env())
        except (OSError, subprocess.SubprocessError):
            continue
        chunks.extend(line for line in text.splitlines() if "409 Conflict" in line)
    if not chunks:
        return ""
    return hashlib.sha256("\n".join(chunks).encode("utf-8")).hexdigest()


def _hermes_cron_state(path: Path = HERMES_CRON_JOBS_PATH) -> dict[str, str]:
    """Return only P0 job health; never include prompts, delivery targets or other user data."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("jobs", payload) if isinstance(payload, dict) else payload
        jobs = {str(row.get("id")): row for row in rows if isinstance(row, dict)}
    except (OSError, TypeError, ValueError):
        return {job_id: "unavailable" for job_id in CRITICAL_HERMES_CRON_JOBS}
    states: dict[str, str] = {}
    for job_id in CRITICAL_HERMES_CRON_JOBS:
        job = jobs.get(job_id)
        if job is None:
            states[job_id] = "missing"
        elif not bool(job.get("enabled", True)) or job.get("state") == "paused":
            states[job_id] = "paused"
        elif job.get("last_status") == "error" or job.get("state") == "error":
            states[job_id] = "error"
        else:
            states[job_id] = "scheduled"
    return states


def collect_snapshot(run: Callable[..., str] = _run) -> dict[str, Any]:
    containers = {name: _container_state(name, run) for name in CONTAINERS}
    try:
        gateway_state = run(
            ["systemctl", "--user", "is-active", "hermes-gateway.service"],
            env=_systemctl_user_env(),
        )
    except (OSError, subprocess.SubprocessError):
        gateway_state = "inactive"
    try:
        gateway_pid = int(
            run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    "-p",
                    "MainPID",
                    "--value",
                    "hermes-gateway.service",
                ],
                env=_systemctl_user_env(),
            )
            or 0
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        gateway_pid = 0
    try:
        backup_timer = run(["systemctl", "is-active", "hermes-backup.timer"])
    except (OSError, subprocess.SubprocessError):
        backup_timer = "inactive"
    usage = shutil.disk_usage("/")
    disk_percent = round(100 * usage.used / usage.total, 1) if usage.total else 100.0
    return {
        "containers": containers,
        "gateway": {"state": gateway_state, "pid": gateway_pid},
        "backup_timer": backup_timer,
        "disk_percent": disk_percent,
        "conflict_marker": _recent_conflict_marker(run),
        "hermes_cron": _hermes_cron_state(),
    }


def _healthy(container: dict[str, Any]) -> bool:
    return container.get("status") == "running" and container.get("health") in {"healthy", "none"}


def compare(previous: dict[str, Any], current: dict[str, Any]) -> list[Event]:
    events: list[Event] = []
    old_containers = previous.get("containers") or {}
    for name, now in (current.get("containers") or {}).items():
        old = old_containers.get(name) or {}
        if int(now.get("restarts", -1)) > int(old.get("restarts", -1)) >= 0:
            events.append(
                Event(
                    "warning",
                    f"♻️ {name} перезапустился: {old['restarts']} → {now['restarts']}",
                )
            )
        if _healthy(old) and not _healthy(now):
            events.append(
                Event(
                    "critical",
                    f"🔴 {name} недоступен: {now.get('status')}/{now.get('health')}",
                )
            )
        elif old and not _healthy(old) and _healthy(now):
            events.append(Event("success", f"🟢 {name} восстановлен"))

    old_gateway = previous.get("gateway") or {}
    now_gateway = current.get("gateway") or {}
    if old_gateway.get("state") == "active" and now_gateway.get("state") != "active":
        events.append(Event("critical", "🔴 Hermes gateway остановлен"))
    elif (
        old_gateway
        and old_gateway.get("state") != "active"
        and now_gateway.get("state") == "active"
    ):
        events.append(Event("success", "🟢 Hermes gateway восстановлен"))
    elif (
        old_gateway.get("state") == "active"
        and now_gateway.get("state") == "active"
        and old_gateway.get("pid")
        and now_gateway.get("pid") != old_gateway.get("pid")
    ):
        events.append(
            Event(
                "info",
                f"🔄 Hermes gateway перезапущен: PID {old_gateway['pid']} → {now_gateway['pid']}",
            )
        )

    if previous.get("backup_timer") == "active" and current.get("backup_timer") != "active":
        events.append(Event("warning", "⚠️ hermes-backup.timer неактивен"))
    elif (
        previous.get("backup_timer") not in {None, "active"}
        and current.get("backup_timer") == "active"
    ):
        events.append(Event("success", "🟢 hermes-backup.timer восстановлен"))

    old_disk = float(previous.get("disk_percent") or 0)
    now_disk = float(current.get("disk_percent") or 0)
    if now_disk >= 95 and old_disk < 95:
        events.append(Event("critical", f"💽 Диск заполнен на {now_disk:.1f}%"))
    elif now_disk >= 85 and old_disk < 85:
        events.append(Event("warning", f"💽 Диск заполнен на {now_disk:.1f}%"))
    elif old_disk >= 85 and now_disk < 80:
        events.append(Event("success", f"🟢 Заполнение диска снизилось до {now_disk:.1f}%"))

    marker = current.get("conflict_marker")
    if marker and marker != previous.get("conflict_marker"):
        events.append(
            Event("critical", "🚫 Обнаружен Telegram 409 Conflict — вероятен второй poller")
        )

    old_cron = previous.get("hermes_cron") or {}
    new_cron = current.get("hermes_cron") or {}
    for job_id, name in CRITICAL_HERMES_CRON_JOBS.items():
        old_state = old_cron.get(job_id)
        new_state = new_cron.get(job_id)
        if new_state in {"missing", "paused", "error"} and old_state != new_state:
            events.append(Event("critical", f"🔴 Hermes P0 cron {name}: {new_state}"))
        elif old_state in {"missing", "paused", "error"} and new_state == "scheduled":
            events.append(Event("success", f"🟢 Hermes P0 cron {name} восстановлен"))
    return events


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save_state(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def run_check(*, env: dict[str, str], state_path: Path = STATE_PATH) -> int:
    current = collect_snapshot()
    previous = _load_state(state_path)
    if previous is None:
        _save_state(state_path, current)
        print("[ops-alert] baseline saved")
        return 0
    events = compare(previous, current)
    if not events:
        _save_state(state_path, current)
        return 0
    severity = max(events, key=lambda event: _SEVERITY_RANK[event.severity]).severity
    body = "\n".join(event.text for event in events)
    if not send_telegram("Aimash infrastructure", body, severity, env=env):
        return 1  # state is intentionally not advanced: retry on the next timer tick
    _save_state(state_path, current)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", action="append", default=[])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    send = sub.add_parser("send")
    send.add_argument("--severity", choices=tuple(_SEVERITY_RANK), default="info")
    send.add_argument("--title", required=True)
    send.add_argument("--body", required=True)
    summary = sub.add_parser("deploy-summary")
    summary.add_argument("--from-sha", default="")
    summary.add_argument("--to-sha", required=True)
    summary.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    paths = tuple(Path(path) for path in args.env_file) or ENV_FILES
    env = _read_env(paths)
    if args.command == "check":
        state_path = Path(env.get("OPS_ALERT_STATE_PATH", str(STATE_PATH)))
        return run_check(env=env, state_path=state_path)
    if args.command == "deploy-summary":
        print(deploy_summary(args.from_sha, args.to_sha, limit=args.limit))
        return 0
    return 0 if send_telegram(args.title, args.body, args.severity, env=env) else 1


if __name__ == "__main__":
    raise SystemExit(main())
