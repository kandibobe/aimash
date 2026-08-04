"""Forum topic routing is one deploy-time policy, not an editable live accident."""

from __future__ import annotations

import importlib.util
import json
import sys

import yaml

from tests._docs_paths import ROOT

PATH = ROOT / "deploy/hermes/sync_aimash_surface.py"
SPEC = importlib.util.spec_from_file_location("sync_aimash_topic_routing", PATH)
assert SPEC is not None and SPEC.loader is not None
SYNC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SYNC
SPEC.loader.exec_module(SYNC)


def test_forum_topic_routing_matches_operator_layout_and_syncs_to_live_config(tmp_path):
    config = yaml.safe_load((ROOT / "deploy/hermes/config.yaml").read_text(encoding="utf-8"))
    expected = json.loads(config["gateway"]["platforms"]["telegram"]["extra"]["group_topics"])
    got = SYNC.reconcile_trusted_operator_policy({})

    assert json.loads(got["gateway"]["platforms"]["telegram"]["extra"]["group_topics"]) == expected
    topics = {item["thread_id"]: item["skill"] for item in expected[0]["topics"]}
    assert topics == {
        1: "ad-master-agent",
        153: "google-ads-worker",
        154: "ad-master-agent",
        155: "ad-master-agent",
        156: "ad-master-agent",
        1992: "ad-master-agent",
        2135: "ad-master-agent",
        2163: "ad-master-agent",
        2164: "ad-master-agent",
    }

    env = tmp_path / ".env"
    env.write_text("AIMASH_TRUST_HMAC_KEY=secret\n", encoding="utf-8")
    SYNC._set_env_values(env, SYNC.ARTIFACT_ARCHIVE_ENV)
    assert SYNC._env_value(env, "AIMASH_ARTIFACT_ARCHIVE_CHAT_ID") == "-1004443550627"
    assert SYNC._env_value(env, "AIMASH_ARTIFACT_ARCHIVE_THREAD_ID") == "2135"
