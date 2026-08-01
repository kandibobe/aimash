#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

try:
    import yaml
except Exception as e:  # pragma: no cover
    print(json.dumps({"error": f"PyYAML import failed: {e}"}, ensure_ascii=False, indent=2))
    raise

MODEL_PATTERNS = [
    "gpt-5.4",
    "gpt-5.6-terra",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash",
    "google/gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "openai-codex",
    "openrouter",
]


@dataclass
class Finding:
    drift_code: str
    severity: str
    source_a: str
    source_b: str
    current_live: str
    issue: str
    recommended_fix: str


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_lines_with_patterns(path: Path, patterns: list[str]) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
        if any(p in line for p in patterns):
            rows.append((i, line.strip()))
    return rows


def maybe_add(findings: list[Finding], condition: bool, finding: Finding) -> None:
    if condition:
        findings.append(finding)


def _args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Check Aimash runtime/config/cron drift")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.getenv("AIMASH_ROOT", Path(__file__).resolve().parents[1])),
        help="Aimash checkout/deploy root (default: checkout containing this script)",
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")),
        help="Hermes state directory (default: HERMES_HOME or ~/.hermes)",
    )
    parser.add_argument(
        "--static",
        action="store_true",
        help="Check repository registries/config/docs only; skip live jobs and installed skills",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    root = args.root.resolve()
    hermes_home = args.hermes_home.resolve()
    runtime_registry = root / "deploy/hermes/runtime_registry.yaml"
    cron_registry_path = root / "deploy/hermes/cron_registry.yaml"
    deploy_config = root / "deploy/hermes/config.yaml"
    live_jobs = hermes_home / "cron/jobs.json"
    skill_files = [
        hermes_home / "skills/ad-master/ad-master-agent/SKILL.md",
        hermes_home / "skills/ad-master/aimash-development/SKILL.md",
        hermes_home / "skills/ad-master/ad-master-cron-ops/SKILL.md",
    ]
    doc_files = [
        root / "README.md",
        root / "SPEC.md",
        root / "deploy/hermes/README.md",
        root / "deploy/hermes/OPERATIONS.md",
    ]

    runtime = load_yaml(runtime_registry)
    cron_registry = load_yaml(cron_registry_path)
    deploy = load_yaml(deploy_config)
    jobs = [] if args.static else load_json(live_jobs).get("jobs", [])

    findings: list[Finding] = []

    canonical_provider = runtime["primary_runtime"]["provider"]
    canonical_model = runtime["primary_runtime"]["model"]
    deploy_provider = deploy.get("model", {}).get("provider")
    deploy_model = deploy.get("model", {}).get("default")

    maybe_add(
        findings,
        (canonical_provider != deploy_provider or canonical_model != deploy_model),
        Finding(
            drift_code="DRIFT-RUNTIME-001",
            severity="P1",
            source_a=str(runtime_registry),
            source_b=str(deploy_config),
            current_live=f"registry={canonical_provider}/{canonical_model}; deploy={deploy_provider}/{deploy_model}",
            issue="Canonical runtime registry disagrees with deploy template main model/provider.",
            recommended_fix="Either reconcile deploy/hermes/config.yaml to the canonical runtime or explicitly label it as a template-only stale exception.",
        ),
    )

    pinned_exception_models = {
        (item.get("provider"), item.get("model")) for item in runtime.get("pinned_exceptions", [])
    }
    live_job_pairs = {(job.get("provider"), job.get("model")) for job in jobs}
    for pair in sorted(live_job_pairs):
        provider, model = pair
        if pair != (canonical_provider, canonical_model) and pair not in pinned_exception_models:
            findings.append(
                Finding(
                    drift_code="DRIFT-RUNTIME-002",
                    severity="P1",
                    source_a=str(runtime_registry),
                    source_b=str(live_jobs),
                    current_live=f"unlisted live job runtime={provider}/{model}",
                    issue="A live cron runtime differs from canonical runtime and is not listed as pinned_exception.",
                    recommended_fix="Add the live runtime to pinned_exceptions or migrate the affected jobs to the canonical runtime.",
                )
            )

    cron_by_id = {job["job_id"]: job for job in cron_registry.get("jobs", [])}
    for live in jobs:
        job_id = live.get("id")
        reg = cron_by_id.get(job_id)
        if not reg:
            findings.append(
                Finding(
                    drift_code="DRIFT-CRON-001",
                    severity="P1",
                    source_a=str(cron_registry_path),
                    source_b=str(live_jobs),
                    current_live=f"missing job_id={job_id} name={live.get('name')}",
                    issue="Live cron job is missing from cron_registry.yaml.",
                    recommended_fix="Add this live job to cron_registry.yaml with criticality and expected state.",
                )
            )
            continue
        actual_state = (
            "paused"
            if not live.get("enabled", True) or live.get("state") == "paused"
            else live.get("state")
        )
        if reg.get("criticality") == "P0" and actual_state == "paused":
            findings.append(
                Finding(
                    drift_code="DRIFT-CRON-002",
                    severity="P0",
                    source_a=str(cron_registry_path),
                    source_b=str(live_jobs),
                    current_live=f"job_id={job_id} actual_state={actual_state}",
                    issue="P0 cron job is paused in live scheduler state.",
                    recommended_fix="Resume the P0 job immediately or explicitly downgrade/remove its criticality if the pause is intentional.",
                )
            )
        if reg.get("expected_state") != "scheduled" and actual_state == "scheduled":
            findings.append(
                Finding(
                    drift_code="DRIFT-CRON-003",
                    severity="P2",
                    source_a=str(cron_registry_path),
                    source_b=str(live_jobs),
                    current_live=f"job_id={job_id} registry_expected={reg.get('expected_state')} actual={actual_state}",
                    issue="Cron registry expected state differs from live state.",
                    recommended_fix="Update cron_registry.yaml or change the live job state so they match.",
                )
            )

    for path in [] if args.static else skill_files:
        rows = extract_lines_with_patterns(path, MODEL_PATTERNS)
        text = path.read_text(encoding="utf-8")
        has_old_runtime = "gpt-5.4" in text and canonical_model != "gpt-5.4"
        has_exception_label = any(
            marker in text
            for marker in [
                "runtime_registry.yaml",
                "pinned exception",
                "pinned_exception",
                "historical_context",
                "live pin",
            ]
        )
        if has_old_runtime and not has_exception_label:
            findings.append(
                Finding(
                    drift_code="DRIFT-SKILL-001",
                    severity="P1",
                    source_a=str(runtime_registry),
                    source_b=str(path),
                    current_live=f"found gpt-5.4 in {path.name} at lines {[n for n, _ in rows if 'gpt-5.4' in _]}",
                    issue="Skill contains present-tense old runtime wording that conflicts with canonical runtime registry.",
                    recommended_fix="Patch the skill to reference runtime_registry.yaml or label the old model mention as pinned_exception/historical_context.",
                )
            )

    for path in doc_files:
        text = path.read_text(encoding="utf-8")
        if canonical_model in text and deploy_model in text and canonical_model != deploy_model:
            findings.append(
                Finding(
                    drift_code="DRIFT-DOC-001",
                    severity="P2",
                    source_a=str(runtime_registry),
                    source_b=str(path),
                    current_live=f"doc mentions canonical model {canonical_model} and deploy-template model {deploy_model}",
                    issue="Document mixes canonical current runtime and stale/deploy-template runtime without clear status separation.",
                    recommended_fix="Split the wording into canonical_current vs pinned_exception/template_context, or remove the stale reference.",
                )
            )

    summary = {
        "ok": not findings,
        "mode": "static" if args.static else "live",
        "root": str(root),
        "checks_skipped": ["live_jobs", "installed_skills"] if args.static else [],
        "finding_count": len(findings),
        "by_severity": {
            sev: sum(1 for f in findings if f.severity == sev) for sev in ["P0", "P1", "P2", "P3"]
        },
        "findings": [asdict(f) for f in findings],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
