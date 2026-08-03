from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from mcp_server import tools_workflows as tw


def test_build_mcc_report_publishes_one_verified_artifact(monkeypatch, tmp_path):
    manager_id = "1234567890"
    period = SimpleNamespace(date_from=date(2026, 7, 4), date_to=date(2026, 8, 2))
    summary = SimpleNamespace(
        children=[SimpleNamespace(account=SimpleNamespace(currency="JPY"))],
        skipped=[],
        inactive=[],
        errors=[],
    )

    monkeypatch.setattr("ads.client.ensure_manager_allowed", lambda value: None)
    monkeypatch.setattr(tw, "build_client_async", lambda value: _async_value(object()))
    monkeypatch.setattr("mcp_server.tools_read._period", lambda *args, **kwargs: period)
    monkeypatch.setattr("mcp_server.tools_read._child_period_factory", lambda value: object())
    monkeypatch.setattr(
        "reports.mcc.build_mcc_summary_async",
        lambda *args, **kwargs: _async_value(summary),
    )
    monkeypatch.setattr("mcp_server.artifacts.artifact_path", lambda suffix: tmp_path / "mcc.xlsx")

    def fake_write(_summary, path, _language):
        from pathlib import Path

        Path(path).write_bytes(b"verified-xlsx")
        return path

    monkeypatch.setattr("reports.xlsx.write_mcc_xlsx", fake_write)
    monkeypatch.setattr(
        "mcp_server.artifacts.publish_artifact",
        lambda path, **kwargs: {"filename": kwargs["filename"], "token": "signed"},
    )

    result = asyncio.run(tw.build_mcc_report(manager_id=manager_id))

    assert result["error_code"] is None
    assert result["account_count"] == 1
    assert result["currencies"] == ["JPY"]
    assert result["artifact_status"] == "published"
    assert result["artifact"]["token"] == "signed"
    assert result["artifact"]["filename"].startswith(
        f"aimash_mcc_{manager_id}_2026-07-04_2026-08-02_"
    )


async def _async_value(value):
    return value
