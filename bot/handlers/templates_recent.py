"""§2B шаблоны кампаний (/templates /savetemplate) и повтор недавних (/recent)

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.message(bm.Command("templates"))
async def templates_cmd(m: bm.Message) -> None:
    await bm._send_templates(m, m.chat.id)


@bm.dp.message(bm.Command("savetemplate"))
async def savetemplate_cmd(m: bm.Message, command: bm.CommandObject) -> None:
    """/savetemplate <имя> [from <кампания>]. Без from — из последнего черновика
    create_search_campaign (клон/новая кампания). С from — читаем живую кампанию (как клон,
    с ре-валидацией RSA). Шаблон хранит ВАЛИДИРОВАННЫЕ params; применение — через confirm-гейт."""
    name, source = bm._parse_savetemplate_arg((command.args or "").strip())
    if not name:
        await m.answer(bm.i18n.t("tpl_save_hint"))
        return
    from db.templates import save_template

    if source:
        from ads.client import build_client_async
        from ads.read import read_campaign_config

        try:
            acct = await bm._active_read_account(m.chat.id)  # §8: источник с активного аккаунта
            client = await build_client_async(acct)
            cfg = await bm.run_ads_read_call(
                read_campaign_config,
                client,
                acct,
                source,
                label="read_campaign_config",
            )
        except Exception as e:  # сеть/доступ/SDK
            await m.answer(bm.i18n.t("clone_read_error", err=bm.ux.err_text(e)))
            return
        if cfg is None:
            await m.answer(bm.i18n.t("clone_source_not_found", name=bm.texts.esc(source)))
            return
        try:
            params, _bu, _dr, _rg = await bm._search_params_from_cfg(cfg, campaign_name=source)
        except bm._SearchBuildError as e:
            await m.answer(bm.i18n.t(e.key, **e.kw))
            return
        await save_template(chat_id=m.chat.id, name=name, params=params, source_campaign=source)
        await m.answer(
            bm.i18n.t("tpl_saved", name=bm.texts.esc(name)), parse_mode=bm.ParseMode.HTML
        )
        return
    last = bm._LAST_SEARCH_PARAMS.get(m.chat.id)
    if not last:
        await m.answer(bm.i18n.t("tpl_save_none"))
        return
    await save_template(chat_id=m.chat.id, name=name, params=dict(last), source_campaign=None)
    await m.answer(bm.i18n.t("tpl_saved", name=bm.texts.esc(name)), parse_mode=bm.ParseMode.HTML)


@bm.dp.callback_query(bm.TemplateCB.filter(bm.F.action == "del"))
async def on_template_del(cq: bm.CallbackQuery, callback_data: bm.TemplateCB) -> None:
    chat_id = bm._cq_chat_id(cq)
    rows = bm._TPL_CACHE.get(chat_id)
    if not bm._valid_idx(rows, callback_data.idx):
        await cq.answer(bm.i18n.t("tpl_list_stale"), show_alert=True)
        return
    from db.templates import delete_template, list_templates

    await delete_template(chat_id, rows[callback_data.idx].name)
    await cq.answer(bm.i18n.t("tpl_deleted"))
    new_rows = await list_templates(chat_id)
    bm._TPL_CACHE[chat_id] = new_rows
    if new_rows:
        await bm._safe_edit(
            cq, bm.i18n.t("tpl_list_title", n=len(new_rows)), reply_markup=bm.templates_kb(new_rows)
        )
    else:
        await bm._safe_edit(cq, bm.i18n.t("tpl_list_empty"))


@bm.dp.callback_query(bm.TemplateCB.filter(bm.F.action == "use"))
async def on_template_use(
    cq: bm.CallbackQuery, callback_data: bm.TemplateCB, state: bm.FSMContext
) -> None:
    """Создание из шаблона: спрашиваем НОВОЕ имя кампании (защита от дубля), затем черновик."""
    chat_id = bm._cq_chat_id(cq)
    rows = bm._TPL_CACHE.get(chat_id)
    if not bm._valid_idx(rows, callback_data.idx):
        await cq.answer(bm.i18n.t("tpl_list_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    name = rows[callback_data.idx].name
    await state.clear()
    await state.set_state(bm.TplWizard.awaiting_name)
    await state.update_data(tpl_name=name)
    await cq.answer()
    await msg.answer(
        bm.i18n.t("tpl_ask_name", tpl=bm.texts.esc(name)),
        reply_markup=bm.nav_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.TplWizard.awaiting_name)
async def tpl_name(m: bm.Message, state: bm.FSMContext) -> None:
    """Имя новой кампании получено → подставляем в params шаблона, ре-валидируем, черновик
    create_search_campaign (тот же confirm-гейт). Дубль имени → стоп. Шаблон гейт НЕ обходит."""
    new_name = (m.text or "").strip()
    if not new_name:
        await m.answer(
            bm.i18n.t("tpl_name_empty"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
        return  # остаёмся в состоянии (retry с навигацией)
    data = await state.get_data()
    tpl_key = (data.get("tpl_name") or "").strip()
    from db.templates import get_template

    tpl = await get_template(m.chat.id, tpl_key) if tpl_key else None
    if tpl is None:
        await state.clear()
        await m.answer(bm.i18n.t("tpl_not_found"))
        return
    await state.clear()
    from ads.client import build_client_async
    from ads.resolve import find_campaign_by_name

    acct = await bm._active_read_account(
        m.chat.id
    )  # §8: создаём на активном аккаунте (и дубль-чек на нём)
    try:
        existing = await bm.run_ads_read_call(
            find_campaign_by_name,
            await build_client_async(acct),
            acct,
            new_name,
            label="find_campaign_by_name",
        )
    except Exception:  # noqa: BLE001 — дубль-проверка best-effort
        existing = None
    if existing is not None:
        await m.answer(bm.i18n.t("clone_name_taken", name=bm.texts.esc(new_name)))
        return
    params = dict(tpl.params)
    params["campaign_name"] = new_name
    try:  # ре-валидация (схема могла измениться с момента сохранения)
        validated: dict = bm.SCHEMAS["create_search_campaign"](**params).model_dump()
    except Exception as e:
        await m.answer(bm.i18n.t("err_validate", err=bm.ux.humanize_validation(e)))
        return
    budget_units = validated["budget_daily_micros"] / 1_000_000
    summary = bm.texts.fmt_search_proposal_summary(
        new_name,
        validated["final_url"],
        budget_units,
        validated["headlines"],
        validated["descriptions"],
        validated["keywords"],
        validated["match_type"],
    )
    p = bm.Proposal(
        operation="create_search_campaign", summary=summary, params=validated, chat_id=m.chat.id
    )
    await bm._present_proposal(
        m,
        chat_id=m.chat.id,
        operation="create_search_campaign",
        params=validated,
        summary=summary,
        cid=p.confirmation_id,
        customer_id=acct,  # §8: создаём кампанию из шаблона на активном аккаунте (замок — в _present_proposal)
    )


@bm.dp.message(bm.Command("recent"))
async def recent_cmd(m: bm.Message) -> None:
    await bm._send_recent(m, m.chat.id)


@bm.dp.callback_query(bm.RecentCB.filter(bm.F.action == "repeat"))
async def on_recent_repeat(cq: bm.CallbackQuery, callback_data: bm.RecentCB) -> None:
    """Повтор недавнего действия: пере-собираем ТЕ ЖЕ params → ре-валидация схемой → confirm-гейт
    (пользователь снова видит «было→станет»). Гейт НЕ обходится. Неподдержанную/невалидную
    операцию честно отклоняем."""
    chat_id = bm._cq_chat_id(cq)
    rows = bm._RECENT_CACHE.get(chat_id)
    if not bm._valid_idx(rows, callback_data.idx):
        await cq.answer(bm.i18n.t("recent_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    action = rows[callback_data.idx]
    op = action.operation
    if op not in bm.SCHEMAS:
        await cq.answer(bm.i18n.t("recent_unsupported"), show_alert=True)
        return
    try:  # ре-валидация (схема могла измениться) + нормализация
        validated: dict = bm.SCHEMAS[op](**action.params).model_dump()
    except Exception:  # noqa: BLE001 — параметры устарели/не валидны → понятный отказ
        await cq.answer(bm.i18n.t("recent_unsupported"), show_alert=True)
        return
    # summary-фолбэк = сохранённый текст (для create_*, у которых fmt_mutation_summary == "").
    fallback = action.summary or op
    p = bm.Proposal(operation=op, summary=fallback, params=validated, chat_id=chat_id)
    await cq.answer()
    await bm._present_proposal(
        msg,
        chat_id=chat_id,
        operation=op,
        params=validated,
        summary=fallback,
        cid=p.confirmation_id,
        customer_id=await bm._active_read_account(chat_id),  # §8: повтор на активном аккаунте
    )
