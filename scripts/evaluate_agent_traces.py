"""Evaluate redacted Hermes traces against lean scenario contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.agent_benchmark import Scenario, evaluate_trace


def _load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, required=True)
    parser.add_argument("--traces", type=Path, required=True)
    args = parser.parse_args()

    scenario_rows = _load_json(args.scenarios)
    trace_rows = _load_json(args.traces)
    if not isinstance(scenario_rows, list) or not isinstance(trace_rows, list):
        raise SystemExit("scenario and trace files must contain JSON arrays")

    parsed = [Scenario.from_dict(row) for row in scenario_rows]
    if len({scenario.id for scenario in parsed}) != len(parsed):
        raise SystemExit("scenario ids must be unique")
    scenarios = {scenario.id: scenario for scenario in parsed}
    traces: dict[str, dict] = {}
    for row in trace_rows:
        if not isinstance(row, dict):
            raise SystemExit("every trace must be a JSON object")
        scenario_id = str(row.get("scenario_id", "")).strip()
        if not scenario_id:
            raise SystemExit("every trace must have scenario_id")
        if scenario_id in traces:
            raise SystemExit(f"duplicate trace scenario_id: {scenario_id}")
        traces[scenario_id] = row
    unknown = sorted(set(traces) - set(scenarios))
    if unknown:
        raise SystemExit(f"unknown trace scenario_id: {','.join(unknown)}")
    results = []
    for scenario_id, scenario in scenarios.items():
        trace = traces.get(scenario_id)
        if trace is None:
            results.append(
                {"scenario_id": scenario_id, "passed": False, "violations": ["trace:missing"]}
            )
        else:
            results.append(evaluate_trace(scenario, trace))
    payload = {
        "passed": all(row["passed"] for row in results),
        "scenario_count": len(results),
        "passed_count": sum(bool(row["passed"]) for row in results),
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
