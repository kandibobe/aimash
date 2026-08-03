from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_deploy_reconnects_hermes_only_after_compose_health_gate():
    body = WORKFLOW.read_text(encoding="utf-8")
    health = body.index('echo "✅ $CNT running (деплой здоров)"')
    restart = body.index("hermes gateway restart")
    live_mcp = body.index("docker inspect -f '{{.State.Status}}' aimash-mcp")

    assert health < restart < live_mcp
    assert "docker compose up -d --build --remove-orphans" in body
    assert "EXPECTED_TOOLS=" in body
    assert "END { print value }" in body
    assert "grep -Eq '^[0-9]+$'" in body
    assert "Could not derive numeric MCP tool count" in body
    assert 'if [ "$LIVE_TOOLS" != "$EXPECTED_TOOLS" ]' in body
    assert "hermes mcp test aimash" not in body
    assert "settings.hermes_write_enabled == ('mcp_server.tools_write' in sys.modules)" in body
    assert "from mcp_server.server import expected_tool_names; expected_tool_names();" in body
    assert "sync_aimash_surface.py" in body


def test_deploy_proves_gateway_pid_changed_and_has_systemd_fallback():
    body = WORKFLOW.read_text(encoding="utf-8")
    restart = body.index("hermes gateway restart")
    fallback = body.index("systemctl --user restart hermes-gateway.service", restart)
    live_mcp = body.index("docker inspect -f '{{.State.Status}}' aimash-mcp", fallback)

    assert "GW_PID_BEFORE=" in body
    assert body.count("GW_PID_AFTER=") >= 2
    assert "GW_STATE=$(systemctl --user is-active hermes-gateway.service || true)" in body
    assert '[ "$GW_PID_AFTER" = "$GW_PID_BEFORE" ]' in body
    assert restart < fallback < live_mcp


def test_deploy_checks_the_single_hermes_poller_for_conflicts():
    body = WORKFLOW.read_text(encoding="utf-8")
    reconnect = body.split("hermes gateway restart", 1)[1]

    assert "docker logs --since 2m aimash-bot" not in reconnect
    assert "journalctl --user -u hermes-gateway.service" in reconnect
    assert reconnect.count('grep -Fq "409 Conflict"') == 1


def test_deploy_recycles_only_a_stale_mcp_child_after_graceful_restart():
    body = WORKFLOW.read_text(encoding="utf-8")
    restart = body.index("hermes gateway restart")
    stale = body.index("Live Hermes MCP surface is stale", restart)
    stop = body.index("hermes gateway stop", stale)
    remove = body.index("docker rm -f aimash-mcp", stop)
    start = body.index("hermes gateway start", remove)
    recovered = body.index("Live Hermes MCP surface mismatch after recovery", start)

    assert restart < stale < stop < remove < start < recovered
    assert "docker compose down" not in body[stale:recovered]
    assert "archive_search" in body[recovered:]
    assert "archive_import_arxiv" in body[recovered:]
