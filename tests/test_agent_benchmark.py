from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agent_benchmark import Scenario, evaluate_trace


_SCENARIOS = Path(__file__).parent / "fixtures" / "agent_benchmark_scenarios.json"


def _load() -> list[dict]:
    with _SCENARIOS.open(encoding="utf-8") as stream:
        return json.load(stream)


def test_scenario_corpus_is_small_unique_and_valid() -> None:
    rows = _load()
    assert len(rows) == 20
    scenarios = [Scenario.from_dict(row) for row in rows]
    assert len({scenario.id for scenario in scenarios}) == len(scenarios)


def test_clean_trace_passes() -> None:
    scenario = Scenario.from_dict(_load()[0])
    result = evaluate_trace(
        scenario,
        {
            "tool_calls": ["get_stats"],
            "terminal_state": "answered",
            "confirmation_requested": False,
        },
    )
    assert result["passed"] is True
    assert result["violations"] == []


def test_safety_and_contract_failures_are_all_reported() -> None:
    scenario = Scenario.from_dict(_load()[3])
    result = evaluate_trace(
        scenario,
        {
            "tool_calls": ["execute_confirmed"] * 5,
            "terminal_state": "completed",
            "confirmation_requested": False,
            "safety_violations": ["untrusted_actor"],
        },
    )
    assert result["passed"] is False
    assert "missing_tools:propose_action" in result["violations"]
    assert "forbidden_tools:execute_confirmed" in result["violations"]
    assert "tool_call_limit:5>4" in result["violations"]
    assert "terminal_state:completed" in result["violations"]
    assert "confirmation:missing" in result["violations"]
    assert "safety:untrusted_actor" in result["violations"]


@pytest.mark.parametrize("field,value", [("terminal_states", []), ("confirmation", "sometimes")])
def test_invalid_scenario_is_rejected(field: str, value: object) -> None:
    raw = dict(_load()[0])
    raw[field] = value
    with pytest.raises(ValueError):
        Scenario.from_dict(raw)
