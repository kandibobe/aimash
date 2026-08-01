from __future__ import annotations

import json
import logging.handlers
import os
import subprocess
import sys

from mcp_server import tool_failures


def test_tool_failure_transport_uses_non_blocking_queue(monkeypatch):
    monkeypatch.delenv("AIMASH_TOOL_ERROR_LOG", raising=False)
    tool_failures.log_tool_failure(
        tool="execute_google_ads_query",
        account="1234567890",
        error_type="INVALID_ARGUMENT",
        message="bad field",
    )
    logger = logging.getLogger("aimash.tool_failures")
    # Full-suite pytest may attach its own LogCaptureHandler instances directly to this logger.
    # Assert our transport invariant without coupling the test to the runner's capture plumbing.
    queue_handlers = [
        handler for handler in logger.handlers if isinstance(handler, logging.handlers.QueueHandler)
    ]
    assert len(queue_handlers) == 1
    assert logger.propagate is False


def test_tool_failure_payload_is_json_and_redacted(monkeypatch):
    messages: list[str] = []

    class _Logger:
        def error(self, message: str) -> None:
            messages.append(message)

    monkeypatch.setattr(tool_failures, "_logger", lambda: _Logger())
    tool_failures.log_tool_failure(
        tool="execute_google_ads_query",
        error_type="INTERNAL_ERROR",
        message="token=SUPERSECRETVALUE123",
    )

    payload = json.loads(messages[0])
    assert payload["event"] == "mcp_tool_failure"
    assert payload["tool"] == "execute_google_ads_query"
    assert "SUPERSECRETVALUE123" not in messages[0]


def test_tool_failure_logging_never_breaks_caller(monkeypatch):
    monkeypatch.setattr(tool_failures, "_logger", lambda: (_ for _ in ()).throw(OSError("disk")))
    assert tool_failures.log_tool_failure(tool="x", message="boom") is None


def test_configured_file_sink_receives_incident(tmp_path):
    target = tmp_path / "aimash_tool_errors.log"
    env = dict(os.environ, AIMASH_TOOL_ERROR_LOG=str(target))
    code = (
        "from mcp_server.envelope import err; "
        "err(ValueError('bad GAQL field'), tool_name='execute_google_ads_query', account='123')"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(target.read_text(encoding="utf-8").strip())
    assert payload["event"] == "mcp_tool_failure"
    assert payload["tool"] == "execute_google_ads_query"
    assert payload["error_type"] == "INVALID_ARGUMENT"
