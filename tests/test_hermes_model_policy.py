"""The deploy sync must remain the source of truth for Hermes model policy."""

from __future__ import annotations

import importlib.util
import sys

from tests._docs_paths import ROOT


PATH = ROOT / "deploy/hermes/sync_aimash_surface.py"
SPEC = importlib.util.spec_from_file_location("sync_aimash_model_policy", PATH)
assert SPEC is not None and SPEC.loader is not None
SYNC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SYNC
SPEC.loader.exec_module(SYNC)


def test_sync_overwrites_dashboard_model_drift_and_disables_external_fallbacks():
    cfg = {
        "auxiliary": {"approval": {"provider": "openrouter", "model": "unsafe"}},
        "fallback_providers": [{"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"}],
    }

    got = SYNC.reconcile_trusted_operator_policy(cfg)

    assert got["auxiliary"] == SYNC.AUXILIARY_POLICY
    assert got["fallback_providers"] == []
