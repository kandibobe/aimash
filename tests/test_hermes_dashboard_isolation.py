"""Static contracts for dashboard isolation from the production MCP owner."""

from __future__ import annotations

from tests._docs_paths import ROOT


def test_dashboard_proxy_blocks_only_the_production_aimash_probe():
    config = (ROOT / "deploy/hermes/dashboard/Caddyfile").read_text(encoding="utf-8")

    assert "method POST" in config
    assert "path /api/mcp/servers/aimash/test" in config
    assert "respond @aimash_mcp_probe" in config
    assert " 423" in config
    assert "reverse_proxy 127.0.0.1:9119" in config


def test_dashboard_proxy_apply_is_backed_up_and_does_not_restart_gateway_or_dashboard():
    script = (ROOT / "scripts/sync_hermes_dashboard_proxy.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "MODE=${1:-dry-run}" in script
    assert "Caddyfile.aimash-prev" in script
    assert 'caddy validate --config "$SOURCE"' in script
    assert 'systemctl restart "$SERVICE"' in script
    assert 'while [ "$ATTEMPT" -le 10 ]' in script
    assert "--max-time 2" in script
    assert "sleep 1" in script
    assert "hermes-dashboard.service" not in script
    assert "hermes-gateway.service" not in script
    assert "scripts/sync_hermes_dashboard_proxy.sh --apply" in workflow
