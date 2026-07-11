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
        recall = await bm._load_report_recall(m.chat.id)  # §UX-память: «↻ повторить прошлый»
        if recall:
            await m.answer(
                bm.i18n.t("report_repeat_last"), reply_markup=bm.report_recall_kb(recall)
            )
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
    # Быстрый путь: весь активный аккаунт; не выбран при нескольких живых → пикер (§8).
    acct = await bm._require_read_account(m, "report")
    if acct is None:
        return
    await bm._run_report(m, period, acct, None, None)
    await bm._save_report_recall(m.chat.id, acct, None, None, (command.args or "").strip())


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
    acct = await bm._require_read_account(m, "export")  # не выбран при неск. живых → пикер (§8)
    if acct is None:
        return
    await bm._run_export(m, period, acct)


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
    acct = await bm._require_read_account(m, "sheets")  # не выбран при неск. живых → пикер (§8)
    if acct is None:
        return
    await bm._run_sheets(m, period, acct)


@bm.dp.message(bm.Command("mcc"))
async def mcc_(m: bm.Message, command: bm.CommandObject) -> None:
    """ТЗ §8: сводка по всем дочерним аккаунтам MCC за период (7/30/90/MTD). Read-only."""
    await bm._send_mcc(m, command.args)


@bm.dp.message(bm.Command("searchterms"))
async def searchterms_(m: bm.Message, command: bm.CommandObject) -> None:
    """§7: топ «мусорных» поисковых запросов (клики без конверсий) → предложить в минус-слова за
    confirm-гейтом. Без аргумента — за 30 дн.; с аргументом-периодом (7/30/90/MTD/ISO) — за него.
    Read-only до «да»: клик по «🚫» минтит черновик add_negative_keywords (правило 1/2)."""
    try:
        period = bm._period_from_arg(command.args)
    except ValueError:
        await m.answer(bm.i18n.t("err_period"))
        return
    await bm._searchterms_run(m, period)


@bm.dp.callback_query(bm.SearchTermsCB.filter(bm.F.action == "cancel"))
async def on_searchterms_cancel(cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB) -> None:
    """§7: закрыть список /searchterms. Ничего не мутирует."""
    bm._SEARCH_TERMS_CACHE.pop(bm._cq_chat_id(cq), None)
    await cq.answer(bm.i18n.t("cb_cancelled"))
    await bm._safe_edit(cq, bm.i18n.t("rejected"))


@bm.dp.callback_query(bm.SearchTermsCB.filter(bm.F.action == "neg"))
async def on_searchterms_neg(cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB) -> None:
    """§7: «🚫 в минус» по запросу → черновик add_negative_keywords (EXACT — режет только этот запрос,
    не шире) за confirm-гейтом. Само добавление — только после «да» (confirmation_id, правило 2).
    idx+gen анти-stale (клик по старой клавиатуре после повторного /searchterms → «список устарел»)."""
    chat_id = bm._cq_chat_id(cq)
    items = bm._SEARCH_TERMS_CACHE.get(chat_id)
    if (
        not items
        or not (0 <= callback_data.idx < len(items))
        or int(getattr(callback_data, "gen", 0)) != bm._SEARCH_TERMS_GEN.get(chat_id, 0)
    ):
        await cq.answer(bm.i18n.t("searchterms_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    it = items[callback_data.idx]
    try:
        cid, operation, params, summary = bm._build_proposal(
            "add_negative_keywords",
            campaign=it["campaign"],
            keywords=[it["term"]],
            match_type="exact",
        )
    except Exception as e:  # noqa: BLE001 — валидация схемы (пустой/длинный ключ) → понятный ответ
        await cq.answer(await bm._friendly_error(e, "searchterms:neg", short=True), show_alert=True)
        return
    await cq.answer()
    await bm._present_proposal_active(
        msg, chat_id=chat_id, operation=operation, params=params, summary=summary, cid=cid
    )


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


# ── 3E: перелистывание постраничных пикеров (rpta/rptc/camp) ───────────────────────
@bm.dp.callback_query(bm.PageCB.filter())
async def on_page_nav(cq: bm.CallbackQuery, callback_data: bm.PageCB) -> None:
    """Ряд «‹ · N/M · ›» пикеров: перерисовать разметку той же страницы кэша. Кэш потерян
    (рестарт) → stale-alert (паттерн cc_account_page_cb)."""
    chat_id = bm._cq_chat_id(cq)
    kind, page = callback_data.kind, max(0, int(callback_data.page))
    if kind == "rpta":
        rows = bm._REPORT_ACCT_CACHE.get(chat_id)
        if not rows:
            await cq.answer(bm.i18n.t("stale"), show_alert=True)
            return
        markup = bm.report_accounts_kb(
            rows,
            callback_data.target,
            last=await bm._last_account(chat_id),
            page=page,
            frequent=await bm._frequent_accounts(chat_id),
        )
    elif kind == "rptc":
        camps = bm._REPORT_CAMP_CACHE.get(chat_id)
        if camps is None:
            await cq.answer(bm.i18n.t("stale"), show_alert=True)
            return
        markup = bm.report_campaigns_kb(camps, callback_data.target, page=page)
    elif kind == "camp":
        camps = bm._CAMP_CACHE.get(chat_id)
        if not camps:
            await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
            return
        markup = bm.campaigns_kb(camps, page=page, gen=bm._camp_gen(chat_id))
    elif kind == "rsac":  # C8: пикер кампаний визарда /rsa
        camps = bm._RSA_CAMP_CACHE.get(chat_id)
        if not camps:
            await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
            return
        markup = bm.rsa_pick_campaigns_kb(camps, page=page)
    elif kind == "aud":  # C8: пикер аудиторий кампании (target несёт camp_idx)
        auds = bm._AUD_CACHE.get(chat_id)
        if auds is None:
            await cq.answer(bm.i18n.t("aud_list_stale"), show_alert=True)
            return
        try:
            camp_idx = int(callback_data.target or -1)
        except ValueError:
            camp_idx = -1
        if not bm._valid_idx(bm._CAMP_CACHE.get(chat_id), camp_idx):
            await cq.answer(bm.i18n.t("aud_list_stale"), show_alert=True)
            return
        markup = bm.audiences_kb(
            auds, camp_idx, attached=bm._AUD_DET_CACHE.get(chat_id) or [], page=page
        )
    else:  # неизвестный kind (старые кнопки после апгрейда) — тихий stale
        await cq.answer(bm.i18n.t("stale"), show_alert=True)
        return
    await cq.answer()
    await bm._safe_edit_markup(cq, markup)


# ── D1: «🔎 Найти» — поиск кампании по названию в пикерах (/campaigns, отчёт, /rsa) ──
@bm.dp.callback_query(bm.PickSearchCB.filter(bm.F.mode == "start"))
async def on_picker_search_start(
    cq: bm.CallbackQuery, callback_data: bm.PickSearchCB, state: bm.FSMContext
) -> None:
    """Клик «🔎 Найти» → ждём часть названия. kind/target кладём в state-data; экран показывает
    только «↩︎ Показать все / ✖ Отмена» (пустой список совпадений) — текст перехватит
    on_picker_search_query (зарегистрирован ДО catch-all)."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._picker_camps(callback_data.kind, chat_id)
    if not camps:  # кэш потерян (рестарт) — тихий stale, старый пикер бесполезен
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    await state.set_state(bm.PickerSearch.awaiting)
    await state.update_data(psrch_kind=callback_data.kind, psrch_target=callback_data.target)
    await cq.answer()
    await bm._safe_edit(
        cq,
        bm.i18n.t("picker_search_prompt"),
        reply_markup=bm.picker_search_kb(
            callback_data.kind, callback_data.target, camps, [], gen=bm._camp_gen(chat_id)
        ),
    )


@bm.dp.callback_query(bm.PickSearchCB.filter(bm.F.mode == "all"))
async def on_picker_search_all(
    cq: bm.CallbackQuery, callback_data: bm.PickSearchCB, state: bm.FSMContext
) -> None:
    """«↩︎ Показать все» → снять фильтр, вернуть полный (постраничный) пикер и состояние-покой."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._picker_camps(callback_data.kind, chat_id)
    if not camps:
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    await state.set_state(bm._picker_rest_state(callback_data.kind))
    await cq.answer()
    await bm._safe_edit(
        cq,
        bm.i18n.t("picker_pick_campaign"),
        reply_markup=bm._picker_full_kb(
            callback_data.kind, callback_data.target, camps, gen=bm._camp_gen(chat_id)
        ),
    )


@bm.dp.message(bm.PickerSearch.awaiting, bm.F.text)
async def on_picker_search_query(m: bm.Message, state: bm.FSMContext) -> None:
    """Текст в режиме поиска = фильтр по подстроке имени/id (ГЛОБАЛЬНЫЕ индексы → выбор без
    изменений). Одноразовый: показали результат → снимаем PickerSearch (повтор — снова «🔎»)."""
    data = await state.get_data()
    kind = data.get("psrch_kind", "campaigns")
    target = data.get("psrch_target", "")
    chat_id = m.chat.id
    camps = bm._picker_camps(kind, chat_id)
    await state.set_state(bm._picker_rest_state(kind))  # одноразовость поиска
    if not camps:
        await m.answer(bm.i18n.t("camp_list_stale"))
        return
    q = (m.text or "").strip()
    indices = bm._picker_match_indices(camps, q)
    if not indices:
        await m.answer(
            bm.i18n.t("picker_search_empty", q=bm.texts.esc(q[:40])),
            reply_markup=bm._picker_full_kb(kind, target, camps, gen=bm._camp_gen(chat_id)),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    await m.answer(
        bm.i18n.t("picker_search_results", n=len(indices), shown=min(len(indices), bm._CAMP_PAGE)),
        reply_markup=bm.picker_search_kb(kind, target, camps, indices, gen=bm._camp_gen(chat_id)),
        parse_mode=bm.ParseMode.HTML,
    )


# ── Inline: выбор АККАУНТА → КАМПАНИИ → периода для отчёта (§8/§9) ─────────────────
@bm.dp.callback_query(bm.ReportAcctCB.filter())
async def on_report_account(cq: bm.CallbackQuery, callback_data: bm.ReportAcctCB) -> None:
    """Выбран аккаунт в пикере /report /export /sheets → показать выбор кампании (или весь аккаунт)."""
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    if callback_data.idx == -3:  # 2.2: «Все аккаунты (MCC)» — кросс-аккаунтная ветка
        chat_id = bm._cq_chat_id(cq)
        if callback_data.target == "export":
            # deep-xlsx: ОДНОРАЗОВЫЙ сентинел в выбор → дальше обычный пикер периода
            # (period_export его pop-ает). Если пользователь бросил пикер периода, сентинел
            # висит до следующего флоу отчёта — безвреден: любой новый выбор его перезапишет.
            bm._REPORT_SEL[chat_id] = {
                "account": bm.MCC_ALL,
                "campaign_id": None,
                "campaign_name": None,
            }
            await bm._safe_edit(
                cq,
                bm.i18n.t("period_pick_export"),
                reply_markup=bm.period_kb("export", last=await bm._last_period(chat_id)),
            )
            return
        # report/sheets → готовая сводка /mcc (текст + лёгкая таблица по всем аккаунтам).
        # Для target=sheets это ОСОЗНАННЫЙ фолбэк: кросс-аккаунтного Google Sheets нет
        # (деньги в разных валютах не суммируются без FX — golden rule 4); полная детализация
        # по всем аккаунтам — «Все аккаунты (MCC)» в /export (deep-xlsx, лист на аккаунт).
        await bm._send_mcc(msg, None)
        return
    if callback_data.idx == -2:  # §UX-память: «↻ повторить прошлый отчёт» (аккаунт+кампания+период)
        recall = await bm._load_report_recall(bm._cq_chat_id(cq))
        if not recall:
            await msg.answer(bm.i18n.t("stale"))
            return
        acct = bm.normalize_customer_id(recall["account"])
        try:
            bm.ensure_read_allowed(acct)  # fail-closed: не строим по неразрешённому аккаунту
        except PermissionError:
            await msg.answer(
                bm.i18n.t("account_denied", cid=bm.texts.esc(acct)), parse_mode=bm.ParseMode.HTML
            )
            return
        try:
            period = bm._period_from_arg(recall["period"])
        except ValueError:
            await msg.answer(bm.i18n.t("err_period"))
            return
        await bm._run_report(
            msg, period, acct, recall.get("campaign_id"), recall.get("campaign_name")
        )
        return
    rows = bm._REPORT_ACCT_CACHE.get(bm._cq_chat_id(cq)) or []
    if not (0 <= callback_data.idx < len(rows)):
        await msg.answer(bm.i18n.t("stale"))
        return
    acct = bm.normalize_customer_id(getattr(rows[callback_data.idx], "id", ""))
    if (
        callback_data.target == "status"
    ):  # §6/§8: пикер → статистика (замок чтения — в _render_status)
        await bm._render_status(msg, acct)
        return
    if callback_data.target == "advise":  # advisor на ВЫБРАННОМ аккаунте (замок — в _advise_run)
        topic = bm._ADVISE_TOPIC_CACHE.pop(bm._cq_chat_id(cq), None)  # тема из пикера (или все)
        await bm._advise_run(msg, bm._cq_chat_id(cq), topic=topic, account=acct)
        return
    if callback_data.target == "audit":  # аудит на ВЫБРАННОМ аккаунте (замок — в _audit_run)
        p = bm._AUDIT_PERIOD_CACHE.pop(
            bm._cq_chat_id(cq), None
        )  # Period из пикера (или дефолт 30 в _audit_run)
        await bm._audit_run(msg, bm._cq_chat_id(cq), account=acct, period=p)
        return
    if callback_data.target == "setacct":  # E/§8 F: выбрать АКТИВНЫЙ аккаунт чтения (персист)
        chat_id = bm._cq_chat_id(cq)
        try:  # TOCTOU: rows уже read-allowed, но перепроверяем замок × пер-юзер грант (fail-closed)
            bm.ensure_read_allowed(acct)
            from core.access import ensure_account_allowed_for_user

            await ensure_account_allowed_for_user(chat_id, acct)
        except PermissionError:
            await msg.answer(
                bm.i18n.t("account_denied", cid=bm.texts.esc(acct)), parse_mode=bm.ParseMode.HTML
            )
            return
        await bm._save_selected_account(chat_id, acct)  # персист выбора (переживает рестарт)
        await msg.answer(bm.i18n.t("account_set", cid=acct), parse_mode=bm.ParseMode.HTML)
        return
    if callback_data.target == "campaigns":  # §8: /campaigns при невыбранном аккаунте → пикер
        # Разовый выбор (как в отчётах): глобальный активный НЕ трогаем, читаем выбранный.
        await bm._send_campaigns_for(msg, bm._cq_chat_id(cq), acct)
        return
    if callback_data.target == "mutate":  # AD.3: выбран аккаунт для ОТЛОЖЕННОЙ мутации → доиграть
        chat_id = bm._cq_chat_id(cq)
        pend = bm._pop_pending_mut(chat_id)
        if not pend:  # пикер бросили / протух → просим повторить команду
            await msg.answer(bm.i18n.t("stale"))
            return
        try:  # rows уже read-allowed, но перепроверяем замок × грант и ПИНИМ активным (fail-closed)
            bm.ensure_read_allowed(acct)
            from core.access import ensure_account_allowed_for_user

            await ensure_account_allowed_for_user(chat_id, acct)
        except PermissionError:
            await msg.answer(
                bm.i18n.t("account_denied", cid=bm.texts.esc(acct)), parse_mode=bm.ParseMode.HTML
            )
            return
        await bm._save_selected_account(chat_id, acct)  # пин: следующую мутацию не переспрашиваем
        await bm._present_proposal(
            msg,
            chat_id=chat_id,
            operation=pend["operation"],
            params=pend["params"],
            summary=pend["summary"],
            cid=pend["cid"],
            external_context=bool(pend.get("external_context", False)),
            customer_id=acct,  # мутационный замок ensure_allowed — внутри _present_proposal
        )
        return
    # P0-3: редактируем сообщение-пикер аккаунта в пикер кампании (без нового сообщения).
    await bm._present_report_campaigns(msg, callback_data.target, rows[callback_data.idx], cq=cq)


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
    # P0-3: редактируем сообщение-пикер кампании в выбор периода (без нового сообщения).
    await bm._safe_edit(
        cq,
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
    await bm._save_report_recall(  # §UX-память: «↻ повторить прошлый отчёт»
        bm._cq_chat_id(cq), acct, campaign_id, campaign_name, callback_data.code
    )


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
    chat_id = bm._cq_chat_id(cq)
    sel = bm._REPORT_SEL.get(chat_id) or {}
    if sel.get("account") == bm.MCC_ALL:  # 2.2: deep-xlsx по всем аккаунтам MCC
        bm._REPORT_SEL.pop(chat_id, None)  # одноразовый сентинел (не липнет к след. отчёту)
        await bm._run_mcc_deep_export(msg, period, callback_data.code)
        return
    acct, campaign_id, campaign_name = await bm._report_target(chat_id)
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
