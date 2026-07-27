"""Тесты финансового аудитора (core.auditor)."""

import pytest
from core.auditor import (
    AuditResult,
    heuristic_audit,
    audit_proposal,
    _suggest_actions,
)


class TestSuggestActions:
    def test_low_risk(self):
        actions = _suggest_actions("low", [])
        assert any("автоматически" in a.lower() for a in actions)

    def test_medium_risk(self):
        actions = _suggest_actions("medium", ["warning"])
        assert any("мониторинг" in a.lower() for a in actions)

    def test_high_risk(self):
        actions = _suggest_actions("high", ["critical"])
        assert any("approvals" in a.lower() for a in actions)
        assert any("человека" in a.lower() for a in actions)


class TestHeuristicAudit:
    def test_normal_budget_change(self):
        proposal = {
            "operation": "budget_change",
            "params": {"new_budget_daily": 110, "old_budget_daily": 100},
        }
        context = {"daily_budget_limit": 500, "target_cpa": 10, "current_metrics": {"cpa": 8}}
        result = heuristic_audit(proposal, context)
        assert result.risk == "low"

    def test_high_delta_budget(self):
        proposal = {
            "operation": "budget_change",
            "params": {"new_budget_daily": 200, "old_budget_daily": 100},
        }
        context = {"daily_budget_limit": 500, "target_cpa": 10, "current_metrics": {"cpa": 8}}
        result = heuristic_audit(proposal, context)
        # Δ=100% (>50%) → risk_score=5 → medium (3-6 band)
        # high requires ≥7 (e.g., Δ>50% + another risk factor)
        assert result.risk == "medium"

    def test_budget_exceeds_daily_limit(self):
        proposal = {
            "operation": "budget_change",
            "params": {"new_budget_daily": 600, "old_budget_daily": 500},
        }
        context = {"daily_budget_limit": 500, "target_cpa": 10, "current_metrics": {"cpa": 8}}
        result = heuristic_audit(proposal, context)
        assert result.risk in ("medium", "high")

    def test_pause_campaign_with_conversions(self):
        proposal = {"operation": "pause_campaign", "params": {}}
        context = {"current_metrics": {"conversions": 50, "cost": 5000}}
        result = heuristic_audit(proposal, context)
        # Есть конверсии + расход > $100 → минимум medium
        assert result.risk in ("medium", "high")

    def test_pause_campaign_no_conversions(self):
        proposal = {"operation": "pause_campaign", "params": {}}
        context = {"current_metrics": {"conversions": 0, "cost": 0}}
        result = heuristic_audit(proposal, context)
        assert result.risk == "low"

    def test_duplicate_operation(self):
        proposal = {
            "operation": "budget_change",
            "params": {"new_budget_daily": 200, "old_budget_daily": 100},
        }
        context = {
            "daily_budget_limit": 500,
            "target_cpa": 10,
            "current_metrics": {"cpa": 8},
            "recent_operations": [
                {
                    "operation": "budget_change",
                    "params": {"new_budget_daily": 200, "old_budget_daily": 100},
                }
            ],
        }
        result = heuristic_audit(proposal, context)
        assert result.risk == "high"

    def test_high_cpa_with_budget_increase(self):
        proposal = {
            "operation": "budget_change",
            "params": {"new_budget_daily": 120, "old_budget_daily": 100},
        }
        context = {
            "daily_budget_limit": 500,
            "target_cpa": 10,
            "current_metrics": {"cpa": 15},  # 50% выше target
        }
        result = heuristic_audit(proposal, context)
        assert result.risk == "medium"


class TestAuditProposal:
    def test_integration(self):
        proposal = {
            "operation": "budget_change",
            "params": {"new_budget_daily": 110, "old_budget_daily": 100},
        }
        context = {"daily_budget_limit": 500, "target_cpa": 10, "current_metrics": {"cpa": 8}}
        result = audit_proposal(proposal, context)
        assert isinstance(result, AuditResult)
        assert result.risk in ("low", "medium", "high")
        assert result.reason