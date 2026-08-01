"""Lightweight Telegram compatibility layer for the Hermes ReAct gateway.

The production runtime is Hermes-first.  This module only owns the small aiogram
surface still used by tests and local integrations: ``/start``, ``/help`` and
free-text forwarding to :mod:`bot.handlers.react_gateway`.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from aiogram import Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, Message

from agent.loop import handle_command
from bot import ux
from core import i18n, texts

if __name__ == "__main__":
    # Avoid a second Dispatcher when this compatibility module is executed with
    # ``python -m bot.main`` and handler modules import ``bot.main``.
    sys.modules.setdefault("bot.main", sys.modules[__name__])

log = logging.getLogger(__name__)
dp = Dispatcher()

WELCOME_IMG = Path(__file__).with_name("assets") / "welcome.png"
_welcome_file_id: str | None = None

_CHAT_CTX_HISTORY = 12
_CHAT_CTX: dict[int, dict[str, Any]] = {}
_PENDING_CONTEXT: dict[int, dict[str, str]] = {}


def _chat_ctx_note(
    chat_id: int,
    *,
    campaign: str | None = None,
    customer_id: str | None = None,
    user_text: str | None = None,
) -> None:
    """Keep a compact per-chat context for pronoun resolution by Hermes."""
    ctx = _CHAT_CTX.setdefault(chat_id, {"campaign": "", "customer_id": "", "history": []})
    if campaign and campaign.strip():
        ctx["campaign"] = campaign.strip()
    if customer_id and str(customer_id).strip():
        ctx["customer_id"] = str(customer_id).strip()
    if user_text and user_text.strip():
        history = ctx.setdefault("history", [])
        history.append(user_text.strip())
        del history[:-_CHAT_CTX_HISTORY]


def _build_agent_context(chat_id: int) -> dict[str, Any]:
    ctx = _CHAT_CTX.get(chat_id) or {}
    return {
        "last_campaign": ctx.get("campaign") or "",
        "last_account": ctx.get("customer_id") or "",
        "history": list(ctx.get("history") or []),
    }


async def _llm_budget_or_reply(message: Message) -> bool:
    """Apply the shared LLM quota before starting a new ReAct cycle."""
    from core import llm_budget

    try:
        await llm_budget.check_daily_cost_cap()
        llm_budget.consume(message.chat.id)
        return False
    except llm_budget.LLMBudgetError as exc:
        await message.answer(ux.llm_budget_text(exc))
        return True


def _fallback_result_text(result: dict[str, Any]) -> str:
    for key in ("text", "summary", "question", "message"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    payload = {k: v for k, v in result.items() if k not in {"confirmation_id", "params"}}
    return (
        json.dumps(payload, ensure_ascii=False, default=str)
        if payload
        else i18n.t("loop_unrecognized")
    )


async def _dispatch_command_result(
    message: Message,
    result: dict[str, Any],
    state: FSMContext,
    *,
    external_context: bool = False,
) -> None:
    """Render an agent result without reviving legacy wizard or callback flows."""
    del state, external_context
    notice = result.get("notice")
    if isinstance(notice, str) and notice.strip():
        await message.answer(notice.strip())

    if result.get("type") == "read" and isinstance(result.get("stats"), dict):
        date_from, date_to = result.get("date_from"), result.get("date_to")
        period_label = ""
        if date_from and date_to:
            period_label = str(date_from) if date_from == date_to else f"{date_from} — {date_to}"
        await message.answer(
            texts.fmt_stats(
                result.get("account", ""),
                result.get("days", 30),
                result["stats"],
                result.get("currency", ""),
                name=result.get("account_name", ""),
                period_label=period_label,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    text = _fallback_result_text(result)
    choices = [str(choice).strip() for choice in result.get("choices", []) if str(choice).strip()]
    if choices:
        text += "\n\n" + "\n".join(f"{index}. {choice}" for index, choice in enumerate(choices, 1))
    await message.answer(text)


async def _run_task_with_context(
    message: Message,
    *,
    instruction: str,
    context_text: str,
    source: str,
    state: FSMContext,
) -> None:
    if await _llm_budget_or_reply(message):
        return
    context = _build_agent_context(message.chat.id)
    async with ux.typing_action(message):
        result = await handle_command(
            instruction,
            chat_id=message.chat.id,
            context_text=context_text,
            context=context,
        )
    _chat_ctx_note(message.chat.id, user_text=instruction)
    await message.answer(i18n.t("ingest_used", source=texts.esc(source)), parse_mode=ParseMode.HTML)
    await _dispatch_command_result(message, result, state, external_context=True)


async def _send_help(message: Message) -> None:
    await message.answer(i18n.t("help"), parse_mode=ParseMode.HTML)


from bot.handlers import register_all  # noqa: E402  (dp must exist first)

register_all()
