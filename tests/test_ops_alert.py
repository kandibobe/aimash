from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops_alert.py"


def _load():
    spec = importlib.util.spec_from_file_location("aimash_ops_alert_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ops = _load()


def _snapshot(
    *, scheduler_health="healthy", scheduler_restarts=0, gateway_pid=10, gateway="active"
):
    return {
        "containers": {
            "aimash-scheduler": {
                "status": "running",
                "health": scheduler_health,
                "restarts": scheduler_restarts,
            },
        },
        "gateway": {"state": gateway, "pid": gateway_pid},
        "backup_timer": "active",
        "disk_percent": 40.0,
        "conflict_marker": "",
        "hermes_cron": {
            "478eac21bfe6": "scheduled",
            "5db43f3b3d5d": "scheduled",
        },
    }


def test_compare_reports_restart_failure_recovery_and_gateway_pid():
    old = _snapshot()
    broken = _snapshot(scheduler_health="unhealthy", scheduler_restarts=1, gateway_pid=11)
    text = "\n".join(event.text for event in ops.compare(old, broken))
    assert "aimash-scheduler перезапустился" in text
    assert "aimash-scheduler недоступен" in text
    assert "Hermes gateway перезапущен" in text

    recovered = _snapshot(scheduler_restarts=1, gateway_pid=11)
    text = "\n".join(event.text for event in ops.compare(broken, recovered))
    assert "aimash-scheduler восстановлен" in text


def test_compare_reports_gateway_down_disk_backup_and_409():
    old = _snapshot()
    current = _snapshot(gateway="inactive")
    current["backup_timer"] = "inactive"
    current["disk_percent"] = 96.0
    current["conflict_marker"] = "new"
    text = "\n".join(event.text for event in ops.compare(old, current))
    assert "gateway остановлен" in text
    assert "backup.timer неактивен" in text
    assert "96.0%" in text
    assert "409 Conflict" in text


def test_compare_reports_p0_cron_failure_and_recovery():
    old = _snapshot()
    broken = _snapshot()
    broken["hermes_cron"]["5db43f3b3d5d"] = "paused"
    text = "\n".join(event.text for event in ops.compare(old, broken))
    assert "Hermes P0 cron Daily Budget Check: paused" in text

    recovered = _snapshot()
    text = "\n".join(event.text for event in ops.compare(broken, recovered))
    assert "Hermes P0 cron Daily Budget Check восстановлен" in text


def test_p0_cron_snapshot_does_not_capture_prompts_or_destinations(tmp_path):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "478eac21bfe6",
                        "enabled": True,
                        "last_status": "ok",
                        "prompt": "client secret context",
                        "deliver": {"chat_id": "sensitive"},
                    },
                    {"id": "5db43f3b3d5d", "enabled": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert ops._hermes_cron_state(jobs) == {
        "478eac21bfe6": "scheduled",
        "5db43f3b3d5d": "paused",
    }


def test_p0_cron_watchlist_matches_registry():
    import yaml

    registry = yaml.safe_load(
        (ROOT / "deploy/hermes/cron_registry.yaml").read_text(encoding="utf-8")
    )
    expected = {job["job_id"] for job in registry["jobs"] if job["criticality"] == "P0"}
    assert set(ops.CRITICAL_HERMES_CRON_JOBS) == expected


def test_send_telegram_targets_explicit_topic_without_leaking_token(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data.decode()
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(ops.urllib.request, "urlopen", fake_urlopen)
    env = {
        "TELEGRAM_BOT_TOKEN": "secret-token",
        "OPS_ALERT_CHAT_ID": "-1004443550627",
        "OPS_ALERT_THREAD_ID": "1",
    }
    assert ops.send_telegram("Deploy", "готово", "success", env=env)
    assert "message_thread_id=1" in captured["data"]
    assert "chat_id=-1004443550627" in captured["data"]


def test_send_telegram_omits_thread_for_general_topic(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured["data"] = request.data.decode()
        return Response()

    monkeypatch.setattr(ops.urllib.request, "urlopen", fake_urlopen)
    env = {
        "TELEGRAM_BOT_TOKEN": "secret-token",
        "OPS_ALERT_CHAT_ID": "-1004443550627",
        "OPS_ALERT_THREAD_ID": "",
    }
    assert ops.send_telegram("Deploy", "ready", "success", env=env)
    assert "message_thread_id" not in captured["data"]


def test_default_env_files_prefer_hermes_bot_token(tmp_path):
    legacy = tmp_path / "legacy.env"
    hermes = tmp_path / "hermes.env"
    legacy.write_text("TELEGRAM_BOT_TOKEN=legacy\nOPS_ALERT_CHAT_ID=-10012345\n", encoding="utf-8")
    hermes.write_text("TELEGRAM_BOT_TOKEN=hermes\n", encoding="utf-8")
    env = ops._read_env((legacy, hermes))
    assert env["TELEGRAM_BOT_TOKEN"] == "hermes"
    assert env["OPS_ALERT_CHAT_ID"] == "-10012345"


def test_failed_delivery_does_not_advance_state(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    old = _snapshot()
    path.write_text(json.dumps(old), encoding="utf-8")
    new = _snapshot(scheduler_restarts=1)
    monkeypatch.setattr(ops, "collect_snapshot", lambda: new)
    monkeypatch.setattr(ops, "send_telegram", lambda *_a, **_kw: False)
    assert ops.run_check(env={}, state_path=path) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == old


def test_baseline_is_silent_and_atomic(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    snap = _snapshot()
    monkeypatch.setattr(ops, "collect_snapshot", lambda: snap)
    monkeypatch.setattr(
        ops,
        "send_telegram",
        lambda *_a, **_kw: pytest.fail("baseline must not notify"),
    )
    assert ops.run_check(env={}, state_path=path) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == snap
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_container_snapshot_reads_top_level_restart_count():
    raw = json.dumps({"Status": "running", "Health": {"Status": "healthy"}}) + "|4"
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return raw

    got = ops._container_state("aimash-scheduler", run)
    assert got == {"status": "running", "health": "healthy", "restarts": 4}
    assert commands == [
        ["docker", "inspect", "-f", "{{json .State}}|{{.RestartCount}}", "aimash-scheduler"]
    ]


def test_systemd_units_are_host_level_and_hardened():
    service = (ROOT / "deploy/hermes/aimash-ops-watch.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy/hermes/aimash-ops-watch.timer").read_text(encoding="utf-8")
    assert "/usr/bin/python3 /opt/aimash/scripts/ops_alert.py check" in service
    assert "StateDirectory=aimash-ops-watch" in service
    assert "NoNewPrivileges=true" in service
    assert "OnUnitActiveSec=1min" in timer


def test_deploy_installs_watcher_and_notifies_both_outcomes():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for required in (
        "install -m 0644 deploy/hermes/aimash-ops-watch.service",
        "systemctl enable --now aimash-ops-watch.timer",
        "trap notify_deploy_exit EXIT",
        '--title "Deploy Aimash failed"',
        '--title "Aimash обновлён"',
        "PREVIOUS_DEPLOY_SHA=$(git rev-parse HEAD",
        "scripts/ops_alert.py deploy-summary",
        "DEPLOY_BODY=$(printf",
        "systemctl start aimash-ops-watch.service",
    ):
        assert required in workflow


def test_deploy_summary_labels_and_bounds_commit_subjects():
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return "\n".join(
            (
                "feat: add keyword export",
                "fix(telegram): deliver report rows",
                "refactor: isolate report builder",
                "docs: explain export",
            )
        )

    summary = ops.deploy_summary("a" * 40, "b" * 40, limit=3, run=run)

    assert summary.splitlines() == [
        "• Добавлено — add keyword export",
        "• Исправлено — deliver report rows",
        "• Изменено — isolate report builder",
        "• Ещё изменений: 1",
    ]
    assert commands == [["git", "log", "--reverse", "--format=%s", f"{'a' * 40}..{'b' * 40}"]]


def test_deploy_summary_rejects_non_sha_arguments():
    with pytest.raises(ValueError, match="invalid from_sha"):
        ops.deploy_summary("HEAD; curl attacker", "b" * 40)


def test_deploy_summary_same_sha_reads_only_one_commit():
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return "chore: redeploy"

    sha = "c" * 40
    assert ops.deploy_summary(sha, sha, run=run) == "• Изменено — redeploy"
    assert commands == [["git", "log", "-1", "--format=%s", sha]]
