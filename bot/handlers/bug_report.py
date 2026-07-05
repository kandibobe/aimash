"""§6 «сообщить об ошибке»: /reportbug (пользователь) + /bugs (админ-триаж).

/reportbug → FSM awaiting_text → приём текста → core.bugs.add_bug_report (текст РЕДАКТИРУЕТСЯ перед
записью, golden rule #5) → форвард админам (ADMIN_CHAT_IDS, тоже редактированно) → тикет-код автору.
/bugs → список последних репортов + инлайн-триаж (new→triaged→closed), ТОЛЬКО админ.

Ничего не мутирует в Google Ads и не создаёт proposal — только локальная память bug_reports.
Позднее связывание bm.<name>; порядок регистрации — HANDLER_MODULES (до fallback/on_text).
"""

from __future__ import annotations

import bot.main as bm


def _is_admin(chat_id: int) -> bool:
    """Админ бота (env ADMIN_CHAT_IDS). Пусто ⇒ никто (fail-closed)."""
    return chat_id in bm.settings.admin_ids


async def reportbug_start(msg: bm.Message, state: bm.FSMContext) -> None:
    """Старт приёма баг-репорта: включаем FSM + промпт. Общий вход для команды и кнопки «➕ Ещё»."""
    await state.set_state(bm.BugReportWizard.awaiting_text)
    await msg.answer(bm.i18n.t("bug_ask"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML)


@bm.dp.message(bm.Command("reportbug"))
async def reportbug_cmd(m: bm.Message, command: bm.CommandObject, state: bm.FSMContext) -> None:
    """/reportbug [текст] — сообщить об ошибке. С аргументом — принимаем сразу; без — просим текст."""
    arg = (command.args or "").strip()
    if arg:
        await _submit_bug(m, state, arg)
        return
    await reportbug_start(m, state)


@bm.dp.message(bm.BugReportWizard.awaiting_text)
async def reportbug_text(m: bm.Message, state: bm.FSMContext) -> None:
    """Текст описания бага → сохранить + форвард админам + тикет-код автору."""
    text = (m.text or "").strip()
    if not text:
        await m.answer(
            bm.i18n.t("bug_empty"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
        return  # остаёмся в состоянии — пользователь пришлёт текст ещё раз (или «✖ Отмена»)
    await _submit_bug(m, state, text)


async def _submit_bug(m: bm.Message, state: bm.FSMContext, text: str) -> None:
    """Сохранить репорт (редакция внутри store), ответить тикет-кодом, форварднуть админам."""
    await state.clear()
    from core import bugs
    from core.context import get_context

    ticket = get_context().request_id  # тикет-код (не секрет), как «код инцидента» err_unexpected
    username = getattr(m.from_user, "username", None) if m.from_user else None
    try:
        bug_id = await bugs.add_bug_report(
            m.chat.id, text, username=username, context_request_id=ticket
        )
    except Exception as e:  # noqa: BLE001 — БД недоступна: не роняем наружу, честно сообщаем
        bm.log.warning("bug report не сохранён: %s", type(e).__name__)
        await m.answer(bm.i18n.t("bug_save_failed"))
        return
    await m.answer(
        bm.i18n.t("bug_thanks", ticket=bm.texts.esc(ticket)), parse_mode=bm.ParseMode.HTML
    )
    await _forward_to_admins(m, bug_id, text, ticket, username)


async def _forward_to_admins(m, bug_id: int, text: str, ticket: str, username) -> None:
    """Немедленный форвард репорта админам (редактированно). Один недоступный админ не роняет остальных."""
    from core.logging import redact_text

    admins = set(bm.settings.admin_ids)
    if not admins:
        return
    safe = redact_text((text or "").strip())
    note = bm.texts.fmt_bug_forward(
        bug_id=bug_id, chat_id=m.chat.id, username=username, ticket=ticket, text=safe
    )
    for admin in admins:
        if admin == m.chat.id:
            continue  # автор уже получил подтверждение
        try:
            await m.bot.send_message(admin, note, parse_mode=bm.ParseMode.HTML)
        except Exception as e:  # noqa: BLE001 — недоступный админ не критичен
            bm.log.warning("bug forward не доставлен админу %s: %s", admin, type(e).__name__)


@bm.dp.message(bm.Command("bugs"))
async def bugs_cmd(m: bm.Message) -> None:
    """/bugs — список последних баг-репортов + триаж (ТОЛЬКО админ). Read-only до нажатия статуса."""
    if not _is_admin(m.chat.id):
        await m.answer(bm.i18n.t("admin_only"))
        return
    from core import bugs

    rows = await bugs.list_bug_reports(limit=15)
    await m.answer(
        bm.texts.fmt_bug_list(rows),
        reply_markup=bm.bugs_kb(rows),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.BugCB.filter())
async def on_bug_cb(cq: bm.CallbackQuery, callback_data: bm.BugCB) -> None:
    """Триаж баг-репорта (админ): статус new→triaged→closed либо обновить список. Локальная БД —
    Google Ads не трогаем, proposal не создаём."""
    if not _is_admin(cq.from_user.id):
        await cq.answer(bm.i18n.t("admin_only"), show_alert=True)
        return
    from core import bugs

    if callback_data.action in ("triaged", "closed"):
        await bugs.set_bug_status(
            callback_data.bid, callback_data.action, triaged_by=cq.from_user.id
        )
    await cq.answer(bm.i18n.t("cb_done"))
    rows = await bugs.list_bug_reports(limit=15)
    await bm._safe_edit(
        cq,
        bm.texts.fmt_bug_list(rows),
        reply_markup=bm.bugs_kb(rows),
        parse_mode=bm.ParseMode.HTML,
    )
