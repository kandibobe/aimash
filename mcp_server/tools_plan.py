"""Операторские PLAN-инструменты: управление черновиками без вызова Google Ads."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from confirm.store import ConfirmStore
from core.context import get_context
from core.provenance import get_provenance
from mcp_server.envelope import ok, refused
from mcp_server.tools_ops import OPS_STATE_MCP_TOOLS, OPS_STATE_TOOL_FUNCS
from mcp_server.tools_workflow_state import WORKFLOW_STATE_TOOL_FUNCS


# Hermes already owns dialogue state and can orchestrate the primitive keyword/RSA/campaign tools.
# Publish only state that must survive outside model context or crosses a trusted transport boundary.
# The legacy workflow implementations remain importable for compatibility/tests, but keeping their
# start/state/update/finalize machinery out of the live registry reduces tool-selection noise.
_AGENT_FIRST_WORKFLOW_NAMES = frozenset(
    {
        "start_keyword_research",  # XLSX/Sheets export + ownership round-trip
        "read_keyword_sheet",
        "create_search_term_review",  # WoW evidence + editable human-review sheet
        "read_search_term_review",  # verified approved rows; still no proposal/mutation
        "build_monthly_pdf",  # trusted human command -> read-only monthly PDF artifact
        "ingest_media",  # trusted Telegram attachment path
        "profile_change",
        "profile_clear",
        "start_client_crawl",
    }
)
AGENT_FIRST_WORKFLOW_STATE_TOOL_FUNCS = {
    name: fn
    for name, fn in WORKFLOW_STATE_TOOL_FUNCS.items()
    if name in _AGENT_FIRST_WORKFLOW_NAMES
}
if AGENT_FIRST_WORKFLOW_STATE_TOOL_FUNCS.keys() != _AGENT_FIRST_WORKFLOW_NAMES:
    raise RuntimeError("agent-first workflow registry is incomplete")


def _trusted_actor() -> tuple[int, int] | None:
    prov = get_provenance()
    chat_id = get_context().chat_id
    if not prov.human_turn or prov.actor_user_id is None or chat_id is None:
        return None
    return int(chat_id), int(prov.actor_user_id)


async def list_pending_proposals(
    account: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Показать pending-черновики текущего человека в текущем Telegram-чате."""
    actor = _trusted_actor()
    if actor is None:
        return refused("Нужен доверенный человеческий Telegram-контекст.")
    chat_id, actor_user_id = actor
    safe_limit = max(1, min(int(limit), 50))
    rows = await ConfirmStore().list_pending_for_actor(
        chat_id=chat_id,
        actor_user_id=actor_user_id,
        customer_id=str(account) if account is not None else None,
        limit=safe_limit,
    )
    return ok(
        [
            {
                "confirmation_id": p.confirmation_id,
                "operation": p.operation,
                "customer_id": p.customer_id,
                "preview": p.summary,
                "risk_tier": p.risk_tier,
                "created_at": p.created_at.isoformat(),
            }
            for p in rows
        ],
        limit=safe_limit,
    )


async def cancel_proposal(confirmation_id: str) -> dict[str, Any]:
    """Атомарно отменить собственный pending-черновик; Google Ads не затрагивается."""
    actor = _trusted_actor()
    if actor is None:
        return refused("Нужен доверенный человеческий Telegram-контекст.")
    cid = str(confirmation_id).strip()
    if not cid or len(cid) > 64:
        return refused("Некорректный confirmation_id.", error_code="invalid_argument")
    chat_id, actor_user_id = actor
    changed = await ConfirmStore().reject_by_actor(
        cid,
        chat_id=chat_id,
        actor_user_id=actor_user_id,
    )
    if not changed:
        return refused("Черновик не найден, уже решён или принадлежит другому автору.")
    return {
        "confirmation_id": cid,
        "status": "rejected",
        "error": None,
        "error_code": None,
    }


PLAN_STATE_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "list_pending_proposals": list_pending_proposals,
    "cancel_proposal": cancel_proposal,
    **OPS_STATE_TOOL_FUNCS,
    **AGENT_FIRST_WORKFLOW_STATE_TOOL_FUNCS,
}

PLAN_STATE_MCP_TOOLS: frozenset[str] = frozenset(PLAN_STATE_TOOL_FUNCS)

if not OPS_STATE_MCP_TOOLS <= PLAN_STATE_MCP_TOOLS:
    raise RuntimeError("operational state tools must be part of the trusted PLAN surface")

PLAN_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    **PLAN_STATE_TOOL_FUNCS,
}
PLAN_MCP_TOOLS: frozenset[str] = frozenset(PLAN_TOOL_FUNCS)

if "execute_confirmed" in PLAN_MCP_TOOLS:
    raise RuntimeError("PLAN surface must never expose execute_confirmed")
