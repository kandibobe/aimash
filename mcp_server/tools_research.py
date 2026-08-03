"""Feature-gated research tools. They cannot mutate Google Ads or client memory."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from ads.client import ensure_read_allowed
from core import observe
from core.config import settings
from mcp_server.envelope import err, ok, wrap_external
from research import archive


def _enabled() -> None:
    if not settings.research_archive_enabled:
        raise PermissionError("research archive is disabled")


def _external_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **row,
            "title": wrap_external(str(row.get("title", ""))),
            "abstract": wrap_external(str(row.get("abstract", ""))),
            "trust": "external",
        }
        for row in rows
    ]


async def archive_search(account: str, query: str, limit: int = 10) -> dict[str, Any]:
    """Search versioned public research metadata already stored by the agency."""
    try:
        _enabled()
        ensure_read_allowed(str(account))
        async with observe.run_scope("mcp_research"):
            rows = await archive.search(query, limit)
            result = ok(_external_rows(rows), limit=limit)
            result["code_numbers"] = []
            return result
    except Exception as exc:  # noqa: BLE001 - MCP boundary redacts the exception
        return err(exc, tool_name="archive_search", account=str(account))


async def archive_import_arxiv(account: str, query: str, limit: int = 10) -> dict[str, Any]:
    """Import bounded arXiv metadata; does not download PDFs or change client/Ads state."""
    try:
        _enabled()
        ensure_read_allowed(str(account))
        async with observe.run_scope("mcp_research"):
            rows, inserted = await archive.import_arxiv(query, limit)
            result = ok(
                _external_rows(archive.public_rows(rows)),
                limit=limit,
                extra={"inserted": inserted, "fetched": len(rows)},
            )
            result["code_numbers"] = []
            return result
    except Exception as exc:  # noqa: BLE001 - MCP boundary redacts the exception
        return err(exc, tool_name="archive_import_arxiv", account=str(account))


RESEARCH_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "archive_search": archive_search,
    "archive_import_arxiv": archive_import_arxiv,
}
RESEARCH_MCP_TOOLS = frozenset(RESEARCH_TOOL_FUNCS)
