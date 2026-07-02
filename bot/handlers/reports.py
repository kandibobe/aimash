"""Отчёты §8/§9: /report /export /sheets /mcc + пикеры аккаунт→кампания→период

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.message(bm.Command("report"))
async def report_(m: bm.Message, command: bm.CommandObject) -> None:
    """Read-only сводка по аккаунту за период. Без аргумента — пикер (аккаунт → кампания → период);
    с аргументом-периодом — быстрый путь на активном аккаунте ЧТЕНИЯ (§6 /account)."""
    if not (command.args or "").strip():
        await bm._start_report_picker(m, "report")  # §8: выбор аккаунта/кампании
        return
    try:
        period = bm._period_from_arg(command.args)
    except ValueError:
        await m.answer(bm.i18n.t("err_period"))
        return
    await bm._remember_period(
        m.chat.id, (command.args or "").strip()
    )  # §UX-память (только пресеты)
    acct = await bm._active_read_account(m.chat.id)  # быстрый путь: весь активный аккаунт
    await bm._run_report(m, period, acct, None, None)


@bm.dp.message(bm.Command("export"))
async def export_(m: bm.Message, command: bm.CommandObject) -> None:
    """Глубокий отчёт .xlsx (разбивки ТЗ §9) вложением. Без аргумента — пикер; с периодом — быстрый путь."""
    if not (command.args or "").strip():
        await bm._start_report_picker(m, "export")
        return
    try:
        period = bm._period_from_arg(command.args)
    except ValueError:
        await m.answer(bm.i18n.t("err_period"))
        return
    await bm._remember_period(
        m.chat.id, (command.args or "").strip()
    )  # §UX-память (только пресеты)
    await bm._run_export(m, period, await bm._active_read_account(m.chat.id))


@bm.dp.message(bm.Command("sheets"))
async def sheets_(m: bm.Message, command: bm.CommandObject) -> None:
    """ТЗ §9: глубокий отчёт в Google Sheets. Без аргумента — пикер; с периодом — быстрый путь. Read-only."""
    if not (command.args or "").strip():
        await bm._start_report_picker(m, "sheets")
        return
    try:
        period = bm._period_from_arg(command.args)
    except ValueError:
        await m.answer(bm.i18n.t("err_period"))
        return
    await bm._remember_period(
        m.chat.id, (command.args or "").strip()
    )  # §UX-память (только пресеты)
    await bm._run_sheets(m, period, await bm._active_read_account(m.chat.id))


@bm.dp.message(bm.Command("mcc"))
async def mcc_(m: bm.Message, command: bm.CommandObject) -> None:
    """ТЗ §8: сводка по всем дочерним аккаунтам MCC за период (7/30/90/MTD). Read-only."""
    await bm._send_mcc(m, command.args)


@bm.dp.message(bm.F.text.in_(bm.BTN_REPORT_ALL))
async def btn_report(m: bm.Message) -> None:
    await bm._start_report_picker(m, "report")  # §8: аккаунт → кампания → период


@bm.dp.message(bm.F.text.in_(bm.BTN_EXPORT_ALL))
async def btn_export(m: bm.Message) -> None:
    await bm._start_report_picker(m, "export")


@bm.dp.message(bm.F.text.in_(bm.BTN_SHEETS_ALL))
async def btn_sheets(m: bm.Message) -> None:
    await bm._start_report_picker(m, "sheets")


@bm.dp.message(bm.F.text.in_(bm.BTN_MCC_ALL))
async def btn_mcc(m: bm.Message) -> None:
    """§8: кнопка «MCC (все аккаунты)» = /mcc c дефолтным периодом (30 дн.)."""
    await bm._send_mcc(m, None)


# ── Inline: выбор АККАУНТА → КАМПАНИИ → периода для отчёта (§8/§9) ─────────────────
@bm.dp.callback_query(bm.ReportAcctCB.filter())
async def on_report_account(cq: bm.CallbackQuery, callback_data: bm.ReportAcctCB) -> None:
    """Выбран аккаунт в пикере /report /export /sheets → показать выбор кампании (или весь аккаунт)."""
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    rows = bm._REPORT_ACCT_CACHE.get(bm._cq_chat_id(cq)) or []
    if not (0 <= callback_data.idx < len(rows)):
        await msg.answer(bm.i18n.t("stale"))
        return
    await bm._present_report_campaigns(msg, callback_data.target, rows[callback_data.idx])


@bm.dp.callback_query(bm.ReportCampCB.filter())
async def on_report_campaign(cq: bm.CallbackQuery, callback_data: bm.ReportCampCB) -> None:
    """Выбрана кампания (или «Весь аккаунт», idx=-1) → показать выбор периода."""
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    chat_id = bm._cq_chat_id(cq)
    sel = bm._REPORT_SEL.get(chat_id) or {"account": bm.DRAFT_ACCOUNT_ID}
    if callback_data.idx == -1:
        sel["campaign_id"], sel["campaign_name"] = None, None
    else:
        camps = bm._REPORT_CAMP_CACHE.get(chat_id) or []
        if not (0 <= callback_data.idx < len(camps)):
            await msg.answer(bm.i18n.t("stale"))
            return
        c = camps[callback_data.idx]
        sel["campaign_id"], sel["campaign_name"] = str(c.get("id")), c.get("name")
    bm._REPORT_SEL[chat_id] = sel
    await msg.answer(
        bm.i18n.t(f"period_pick_{callback_data.target}"),
        # §UX-память: последний пресет — первой кнопкой «↻ как в прошлый раз»
        reply_markup=bm.period_kb(callback_data.target, last=await bm._last_period(chat_id)),
    )


# ── Inline: выбор периода → построение отчёта на ВЫБРАННОМ аккаунте/кампании ───────
@bm.dp.callback_query(bm.PeriodCB.filter(bm.F.target == "report"))
async def period_report(cq: bm.CallbackQuery, callback_data: bm.PeriodCB) -> None:
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    try:
        period = bm._period_from_arg(callback_data.code)
    except ValueError:
        await msg.answer(bm.i18n.t("err_period"))
        return
    await bm._remember_period(bm._cq_chat_id(cq), callback_data.code)  # §UX-память: «в прошлый раз»
    # ФИКС B2: строим на ВЫБРАННОМ аккаунте/кампании (_report_target), а не на хардкоде Draft.
    acct, campaign_id, campaign_name = await bm._report_target(bm._cq_chat_id(cq))
    await bm._run_report(msg, period, acct, campaign_id, campaign_name)


@bm.dp.callback_query(bm.PeriodCB.filter(bm.F.target == "export"))
async def period_export(cq: bm.CallbackQuery, callback_data: bm.PeriodCB) -> None:
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    try:
        period = bm._period_from_arg(callback_data.code)
    except ValueError:
        await msg.answer(bm.i18n.t("err_period"))
        return
    await bm._remember_period(bm._cq_chat_id(cq), callback_data.code)  # §UX-память
    acct, campaign_id, campaign_name = await bm._report_target(bm._cq_chat_id(cq))
    await bm._run_export(msg, period, acct, campaign_id, campaign_name)


@bm.dp.callback_query(bm.PeriodCB.filter(bm.F.target == "sheets"))
async def period_sheets(cq: bm.CallbackQuery, callback_data: bm.PeriodCB) -> None:
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    try:
        period = bm._period_from_arg(callback_data.code)
    except ValueError:
        await msg.answer(bm.i18n.t("err_period"))
        return
    await bm._remember_period(bm._cq_chat_id(cq), callback_data.code)  # §UX-память
    acct, campaign_id, campaign_name = await bm._report_target(bm._cq_chat_id(cq))
    await bm._run_sheets(msg, period, acct, campaign_id, campaign_name)
