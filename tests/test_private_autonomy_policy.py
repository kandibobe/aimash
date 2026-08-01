from __future__ import annotations

from ads.service import SUPPORTED_OPERATIONS
from confirm.policy import (
    ALL_ADS_OPS,
    AUTONOMOUS_ADS_OPS,
    AUTONOMOUS_MEMORY_OPS,
    CONFIRM_REQUIRED_ADS_OPS,
    may_execute_autonomously,
    requires_confirmation,
)
from confirm.store import ConfirmStore


def test_policy_is_total_disjoint_and_unknown_fails_closed():
    assert AUTONOMOUS_ADS_OPS.isdisjoint(CONFIRM_REQUIRED_ADS_OPS)
    assert ALL_ADS_OPS == SUPPORTED_OPERATIONS
    assert CONFIRM_REQUIRED_ADS_OPS == frozenset({"update_budget"})
    assert AUTONOMOUS_ADS_OPS == SUPPORTED_OPERATIONS - {"update_budget"}
    assert may_execute_autonomously("update_campaign") is True
    assert may_execute_autonomously("create_search_campaign") is True
    assert requires_confirmation("update_campaign") is False
    assert requires_confirmation("create_search_campaign") is False
    assert requires_confirmation("add_keywords") is False
    assert requires_confirmation("set_geo_location") is False
    assert requires_confirmation("create_rsa") is False
    assert requires_confirmation("update_budget") is True
    assert AUTONOMOUS_MEMORY_OPS == frozenset()
    assert requires_confirmation("profile_save") is True
    assert requires_confirmation("profile_update") is True
    assert requires_confirmation("profile_clear") is True
    assert requires_confirmation("future_unknown_operation") is True
    assert not hasattr(ConfirmStore, "authorize_autonomous")


def test_budget_approval_is_decided_from_attested_live_delta():
    small = {"_before": {"before_micros": 100_000_000, "after_micros": 120_000_000}}
    global_change = {
        "_before": {
            "before_micros": 100_000_000,
            "after_micros": 120_000_000,
            "shared_campaigns": ["Campaign A", "Campaign B"],
        }
    }

    assert requires_confirmation("update_budget", small) is False
    assert may_execute_autonomously("update_budget", small) is True
    assert requires_confirmation("update_budget", global_change) is True
