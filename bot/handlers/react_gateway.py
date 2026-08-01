"""Автономный текстовый вход: любой non-command текст сразу уходит в ReAct-агента.

Модуль регистрируется первым в ``HANDLER_MODULES``. Поэтому legacy FSM handlers не могут
перехватить свободный текст даже тогда, когда в storage остался старый state визарда.
Slash-команды намеренно исключены фильтром и продолжают обрабатываться command handlers.
"""

from __future__ import annotations

import bot.main as bm


async def forward_to_react(
    m: bm.Message,
    state: bm.FSMContext,
    *,
    text: str | None = None,
) -> None:
    """Сбросить legacy FSM и передать человеческий текст в единый agent entrypoint."""
    instruction = (text if text is not None else m.text or "").strip()
    if not instruction:
        return

    # Старый state больше не управляет маршрутизацией. Очищаем и state, и его data, чтобы
    # последующие callback'и визарда не продолжили устаревший сценарий.
    await state.clear()

    # Файл без caption по-прежнему связывается со следующей человеческой инструкцией, но без
    # IngestWizard: контекст достаётся напрямую и в тот же ход передаётся агенту.
    pending = bm._PENDING_CONTEXT.pop(m.chat.id, None)
    if pending is not None:
        await bm._run_task_with_context(
            m,
            instruction=instruction,
            context_text=pending["text"],
            source=pending.get("source", ""),
            state=state,
        )
        return

    if await bm._llm_budget_or_reply(m):
        return
    context = bm._build_agent_context(m.chat.id)
    async with bm.ux.typing_action(m):
        result = await bm.handle_command(instruction, chat_id=m.chat.id, context=context)
    bm._chat_ctx_note(m.chat.id, user_text=instruction)
    await bm._dispatch_command_result(m, result, state)


@bm.dp.message(bm.Command("newcampaign"))
async def newcampaign_react(m: bm.Message, state: bm.FSMContext) -> None:
    """Совместимый алиас: вместо CreateCampaignWizard начинает agent-first диалог."""
    await forward_to_react(
        m,
        state,
        text="Создай новую кампанию Google Ads. Собери недостающие параметры в диалоге.",
    )


@bm.dp.message(bm.F.text & ~bm.F.text.startswith("/"))
async def on_text(m: bm.Message, state: bm.FSMContext) -> None:
    await forward_to_react(m, state)
