from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_deploy_reconnects_hermes_only_after_compose_health_gate():
    body = WORKFLOW.read_text(encoding="utf-8")
    health = body.index('echo "✅ $CNT running (деплой здоров)"')
    restart = body.index("hermes gateway restart")
    mcp_test = body.index("hermes mcp test aimash")

    assert health < restart < mcp_test
    assert "EXPECTED_TOOLS=" in body
    assert 'grep -Fq "Tools discovered: $EXPECTED_TOOLS"' in body
    assert "settings.hermes_write_enabled == ('mcp_server.tools_write' in sys.modules)" in body
    assert "sync_aimash_surface.py" in body


def test_deploy_checks_both_telegram_pollers_for_conflicts():
    body = WORKFLOW.read_text(encoding="utf-8")
    reconnect = body.split("hermes gateway restart", 1)[1]

    assert "docker logs --since 2m aimash-bot" in reconnect
    assert "journalctl --user -u hermes-gateway.service" in reconnect
    assert reconnect.count('grep -Fq "409 Conflict"') == 2
