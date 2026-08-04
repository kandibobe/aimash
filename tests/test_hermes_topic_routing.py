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
        1: "operational-coordinator",
        153: "google-ads-worker",
        154: "paid-social-advisor",
        155: "paid-social-advisor",
        156: "operational-coordinator",
        1992: "operational-coordinator",
        2135: "operational-coordinator",
        2163: "operational-coordinator",
        2164: "operational-coordinator",
    }

    env = tmp_path / ".env"
    env.write_text("AIMASH_TRUST_HMAC_KEY=secret\n", encoding="utf-8")
    SYNC._set_env_values(env, SYNC.ARTIFACT_ARCHIVE_ENV)
    assert SYNC._env_value(env, "AIMASH_ARTIFACT_ARCHIVE_CHAT_ID") == "-1004443550627"
    assert SYNC._env_value(env, "AIMASH_ARTIFACT_ARCHIVE_THREAD_ID") == "2135"


def test_every_topic_skill_is_deployed_and_every_runtime_skill_has_one_role():
    selected = {item["skill"] for group in SYNC.TELEGRAM_GROUP_TOPICS for item in group["topics"]}

    assert selected <= set(SYNC.CANONICAL_SKILLS)
    assert set(SYNC.RUNTIME_SKILL_ROLES) == set(SYNC.CANONICAL_SKILLS)
    assert SYNC.RUNTIME_SKILL_ROLES["ad-master-agent"] == "delegated deep Google Ads analysis"
    assert SYNC.RUNTIME_SKILL_ROLES["creative-director"] == "Google Ads creative intent"
    for name in SYNC.CANONICAL_SKILLS:
        skill = ROOT / "deploy/hermes/skills/ad-master" / name / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        assert f"name: {name}" in text.split("---", 2)[1]
