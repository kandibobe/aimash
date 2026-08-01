"""Non-blocking structured incident stream for MCP tool failures.

The agent receives a sanitized self-healing envelope immediately.  A background
logging thread writes the same incident as one JSON line, so a slow log sink
cannot delay or break the MCP response path.
"""

from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.logging import redact_text

_LOGGER_NAME = "aimash.tool_failures"
_LOCK = threading.Lock()
_LISTENER: logging.handlers.QueueListener | None = None


def _sink() -> logging.Handler:
    path = os.getenv("AIMASH_TOOL_ERROR_LOG", "").strip()
    if not path:
        return logging.StreamHandler(sys.stderr)

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    return logging.handlers.RotatingFileHandler(
        target,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )


def _logger() -> logging.Logger:
    global _LISTENER
    logger = logging.getLogger(_LOGGER_NAME)
    if _LISTENER is not None:
        return logger

    with _LOCK:
        if _LISTENER is not None:
            return logger
        records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
        sink = _sink()
        sink.setFormatter(logging.Formatter("%(message)s"))
        listener = logging.handlers.QueueListener(records, sink, respect_handler_level=True)
        logger.handlers.clear()
        logger.addHandler(logging.handlers.QueueHandler(records))
        logger.setLevel(logging.ERROR)
        logger.propagate = False
        listener.start()
        _LISTENER = listener
    return logger


def _stop_listener() -> None:
    global _LISTENER
    listener = _LISTENER
    _LISTENER = None
    if listener is not None:
        listener.stop()


atexit.register(_stop_listener)


def log_tool_failure(**fields: Any) -> None:
    """Queue one sanitized JSON incident and never affect the tool response."""
    try:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "mcp_tool_failure",
            **fields,
        }
        serialized = redact_text(json.dumps(payload, ensure_ascii=False, default=str))
        _logger().error(serialized)
    except Exception:  # noqa: BLE001 - observability must never break self-healing
        return
