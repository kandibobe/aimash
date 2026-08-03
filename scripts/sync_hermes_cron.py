"""Reconcile version-controlled Aimash cron policy with Hermes live state.

Dry-run is the default.  ``--apply`` creates a timestamped backup of ``cron/jobs.json`` before the
first edit.  The script intentionally manages only registry entries that declare ``schedule`` or
``prompt_file``; unrelated/user-owned Hermes jobs are left untouched.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _load_registry(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jobs = data.get("jobs") or []
    seen: set[str] = set()
    managed: list[dict] = []
    for job in jobs:
        job_id = str(job.get("job_id", "")).strip()
        if not job_id or job_id in seen:
            raise ValueError(f"invalid or duplicate job_id: {job_id!r}")
        seen.add(job_id)
        if job.get("schedule") or job.get("prompt_file"):
            managed.append(job)
    return managed


def _prompt(registry: Path, job: dict) -> str | None:
    relative = job.get("prompt_file")
    if not relative:
        return None
    path = (registry.parent / str(relative)).resolve()
    if registry.parent.resolve() not in path.parents:
        raise ValueError(f"prompt_file escapes registry directory: {relative}")
    return path.read_text(encoding="utf-8").strip()


def _live_jobs(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("jobs", data) if isinstance(data, dict) else data
    return {str(item["id"]): item for item in rows}


def build_plan(registry: Path, jobs_path: Path) -> list[dict]:
    live = _live_jobs(jobs_path)
    plan: list[dict] = []
    for desired in _load_registry(registry):
        job_id = str(desired["job_id"])
        current = live.get(job_id)
        if current is None:
            raise ValueError(f"managed Hermes job is missing: {job_id}")
        changes: dict[str, str] = {}
        schedule = str(desired.get("schedule", "")).strip()
        current_schedule = str((current.get("schedule") or {}).get("expr", "")).strip()
        if schedule and schedule != current_schedule:
            changes["schedule"] = schedule
        name = str(desired.get("name", "")).strip()
        if name and name != str(current.get("name", "")).strip():
            changes["name"] = name
        prompt = _prompt(registry, desired)
        if prompt is not None and prompt != str(current.get("prompt", "")).strip():
            changes["prompt"] = prompt
        resume = bool(desired.get("auto_resume")) and not bool(current.get("enabled"))
        if changes or resume:
            plan.append(
                {
                    "job_id": job_id,
                    "name": name or current.get("name", ""),
                    "changes": changes,
                    "resume": resume,
                }
            )
    return plan


def apply_plan(plan: list[dict], jobs_path: Path, hermes: str) -> Path | None:
    if not plan:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = jobs_path.with_name(f"jobs.json.bak.{stamp}")
    shutil.copy2(jobs_path, backup)
    for item in plan:
        changes = item["changes"]
        if changes:
            cmd = [hermes, "cron", "edit", item["job_id"]]
            for key in ("schedule", "name", "prompt"):
                if key in changes:
                    cmd.extend([f"--{key}", changes[key]])
            subprocess.run(cmd, check=True)
        if item["resume"]:
            subprocess.run([hermes, "cron", "resume", item["job_id"]], check=True)
    return backup


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=root / "deploy/hermes/cron_registry.yaml",
    )
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--hermes-bin", default="hermes")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    jobs_path = args.hermes_home / "cron/jobs.json"
    plan = build_plan(args.registry.resolve(), jobs_path.resolve())
    safe = [
        {
            "job_id": item["job_id"],
            "name": item["name"],
            "fields": sorted(item["changes"]),
            "resume": item["resume"],
        }
        for item in plan
    ]
    print(
        json.dumps(
            {"mode": "apply" if args.apply else "dry-run", "changes": safe}, ensure_ascii=False
        )
    )
    if args.apply:
        backup = apply_plan(plan, jobs_path.resolve(), args.hermes_bin)
        if backup:
            print(json.dumps({"backup": str(backup)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
