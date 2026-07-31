"""Hermes conversation/config backup is a versioned, deployment-enforced contract."""

from tests._docs_paths import ROOT


def test_backup_units_are_versioned_and_point_to_repo_script():
    service = (ROOT / "deploy/hermes/hermes-backup.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy/hermes/hermes-backup.timer").read_text(encoding="utf-8")
    assert "ExecStart=/bin/sh /opt/aimash/scripts/backup_hermes.sh" in service
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer


def test_deploy_installs_runs_and_verifies_hermes_backup():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for required in (
        "systemctl enable --now hermes-backup.timer",
        "systemctl start hermes-backup.service",
        "grep -q '/state.db$'",
        "grep -q '/\\.env$'",
        "chmod 0600 /root/.hermes/state.db",
    ):
        assert required in workflow


def test_backup_has_consistent_python_sqlite_fallback():
    script = (ROOT / "scripts/backup_hermes.sh").read_text(encoding="utf-8")
    assert 'HERMES_PYTHON="${HERMES_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python}"' in script
    assert "source.backup(target)" in script
    assert 'chmod 600 "$OUT"' in script
    assert 'chmod 700 "$OUT_DIR"' in script
