"""§19 визард «Создание кампании» (/newcampaign, cc_*): 8 этапов, один composite proposal

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.message(bm.Command("newcampaign"))
async def newcampaign_cmd(m: bm.Message, state: bm.FSMContext) -> None:
    await bm._cc_entry(m, state)


@bm.dp.message(bm.F.text.in_(bm.BTN_NEWCAMPAIGN_ALL))
async def btn_newcampaign(m: bm.Message, state: bm.FSMContext) -> None:
    await bm._cc_entry(m, state)


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "resume"))
async def cc_resume(cq: bm.CallbackQuery, state: bm.FSMContext) -> None:
    chat_id = bm._cq_chat_id(cq)
    draft = await bm.CDRAFTS.get_active(chat_id)
    msg = bm._cq_msg(cq)
    if draft is None or msg is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        return
    await cq.answer()
    await bm._safe_edit(cq, bm.i18n.t("cb_done"))
    await bm._cc_render_stage(msg, chat_id, draft, state)


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "new"))
async def cc_new(cq: bm.CallbackQuery, state: bm.FSMContext) -> None:
    chat_id = bm._cq_chat_id(cq)
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    await cq.answer()
    await bm._safe_edit(cq, bm.i18n.t("cb_done"))
    await bm._cc_begin(msg, chat_id, state)


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "acct"))
async def cc_account_cb(cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext) -> None:
    """Этап 0: аккаунт выбран (read-only превью) → фиксируем preview_customer_id, переходим к
    Этапу 1 (описание). Аккаунт МУТАЦИИ остаётся Draft (замок не трогаем)."""
    chat_id = bm._cq_chat_id(cq)
    rows = bm._CC_ACCT_CACHE.get(chat_id)
    if rows is None:  # кэш пуст (рестарт процесса) — само-хил: перерисовать пикер, не тупик-alert
        msg = bm._cq_msg(cq)
        if msg is not None and await bm.CDRAFTS.get_active(chat_id):  # черновик в БД жив
            await cq.answer()
            await bm._cc_present_stage0(msg, chat_id)
            return
    if not bm._valid_idx(rows, callback_data.idx):
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        return
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=chat_id) if session_id else None
    if draft is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        await state.clear()
        return
    chosen = rows[callback_data.idx]
    preview_id = getattr(chosen, "id", bm.DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_preview(session_id, preview_id, expected_chat_id=chat_id)
    await bm.CDRAFTS.set_step(session_id, 1, expected_chat_id=chat_id)
    await state.set_state(bm.CreateCampaignWizard.settings_desc)
    await cq.answer()
    await bm._safe_edit(
        cq,
        bm.texts.cc_account_header(getattr(chosen, "name", ""), preview_id),
        # P1-L: «‹ Назад» → Этап 0 (выбор аккаунта). cc_back декрементит шаг и перерисовывает
        # пикер — раньше сменить аккаунт можно было только полной отменой визарда (dead-spot).
        reply_markup=bm.nav_kb(bm.CcCB(action="back")),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "back"))
async def cc_back(cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext) -> None:
    """3D: «‹ Назад» — на предыдущий этап визарда. НАВИГАЦИЯ, не откат: данные черновика
    (ключи/RSA/настройки) НЕ стираются — _cc_render_stage показывает сохранённое подсостояние
    (напр. подтверждённый список ключей на Этапе 2). Раньше единственным «назад» была полная
    отмена с потерей маршрута."""
    chat_id = bm._cq_chat_id(cq)
    msg = bm._cq_msg(cq)
    draft = await bm.CDRAFTS.get_active(chat_id)
    if draft is None or msg is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        return
    prev = max(0, int(draft.current_step) - 1)
    snap = await bm.CDRAFTS.set_step(draft.session_id, prev, expected_chat_id=chat_id)
    await cq.answer()
    if snap is not None:
        # P0-3: убираем текущую карточку этапа перед показом предыдущей — «туда-обратно» не
        # плодит одинаковые сообщения (этапы могут слать >1 сообщения → удаление надёжнее правки).
        try:
            await msg.delete()
        except Exception:  # noqa: BLE001 — старое/уже удалённое сообщение
            pass
        await bm._cc_render_stage(msg, chat_id, snap, state)


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "page"))
async def cc_account_page_cb(cq: bm.CallbackQuery, callback_data: bm.CcCB) -> None:
    """B7: перелистывание постраничного пикера аккаунтов Этапа 0 — перерисовываем ту же разметку."""
    chat_id = bm._cq_chat_id(cq)
    rows = bm._CC_ACCT_CACHE.get(chat_id)
    if not rows:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        return
    try:
        page = int(callback_data.sub or "0")
    except ValueError:
        page = 0
    await cq.answer()
    await bm._safe_edit_markup(cq, bm.cc_accounts_kb(rows, page=page))


@bm.dp.message(bm.CreateCampaignWizard.account_select)
async def cc_account_search(m: bm.Message, state: bm.FSMContext) -> None:
    """Этап 0 (§19.2 «постраничный список / поиск по названию»): свободный текст в состоянии выбора
    аккаунта = поисковый запрос — фильтруем кэш списка по подстроке имени/id и показываем совпадения
    (ГЛОБАЛЬНЫЕ индексы → cc_account_cb без изменений). Пока визард на Этапе 0, текст НЕ уходит в
    NL-агента (как и на других этапах, B8); выход — «✖ Отмена». Read-only."""
    chat_id = m.chat.id
    rows = bm._CC_ACCT_CACHE.get(chat_id)
    if rows is None:  # кэш потерян (рестарт) — перерисовать пикер заново (само-хил)
        await bm._cc_present_stage0(m, chat_id)
        return
    q = (m.text or "").strip().lower()
    if not q:
        await m.answer(
            bm.i18n.t("cc_pick_account"),
            reply_markup=bm.cc_accounts_kb(rows),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    matches = [
        i
        for i, r in enumerate(rows)
        if q in (getattr(r, "name", "") or "").lower() or q in str(getattr(r, "id", ""))
    ]
    if not matches:
        await m.answer(
            bm.i18n.t("cc_acct_search_empty", q=bm.texts.esc(q[:40])),
            reply_markup=bm.cc_accounts_kb(rows),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    from bot.keyboards import _ACCT_PAGE, cc_accounts_search_kb

    await m.answer(
        bm.i18n.t("cc_acct_search_results", n=len(matches), shown=min(len(matches), _ACCT_PAGE)),
        reply_markup=cc_accounts_search_kb(rows, matches),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.CreateCampaignWizard.settings_desc)
async def cc_settings_desc(m: bm.Message, state: bm.FSMContext) -> None:
    """Этап 1: свободное описание → извлечение настроек + медианы «по аналогии» → сводка."""
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=m.chat.id) if session_id else None
    if draft is None:
        await state.clear()
        await m.answer(bm.i18n.t("cc_draft_stale"))
        return
    text = (m.text or "").strip()
    if not text:
        await m.answer(
            bm.i18n.t("cc_empty_description"),
            reply_markup=bm.nav_kb(),
            parse_mode=bm.ParseMode.HTML,  # копируемый <code>-пример
        )
        return
    await m.answer(bm.i18n.t("cc_extracting"))
    try:
        extracted = await bm.extract_campaign_settings(text, language=bm.i18n.current_lang())
    except Exception as e:  # LLM/сеть — остаёмся в состоянии, пользователь повторит
        await m.answer(bm.i18n.t("err_text_gen", err=bm.ux.err_text(e)), reply_markup=bm.nav_kb())
        return
    medians = await bm._cc_read_medians(draft.preview_customer_id or bm.DRAFT_ACCOUNT_ID)
    settings_dict = bm.assemble_settings(
        extracted,
        median_budget_micros=medians.median_daily_budget_micros,
        avg_cpc_micros=medians.avg_cpc_micros,
        common_match_type=medians.common_match_type,
        topic=text,  # полное описание (не text[:60] — прежний срез отрезал сам товар)
        ui_language=bm.i18n.current_lang(),
    )
    await bm.CDRAFTS.patch(
        session_id, lambda s: s.__setitem__("settings", settings_dict), expected_chat_id=m.chat.id
    )
    await state.set_state(bm.CreateCampaignWizard.settings_confirm)
    await m.answer(
        bm._cc_crumb(1) + bm.texts.fmt_cc_settings_summary(settings_dict),
        reply_markup=bm.cc_settings_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.CreateCampaignWizard.settings_confirm)
async def cc_settings_edit(m: bm.Message, state: bm.FSMContext) -> None:
    """Этап 1: пред-confirm правка текстом («поставь бюджет 60») → merge → пере-сводка. Это правка
    черновика, НЕ мутация (confirmation_id не тратится)."""
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=m.chat.id) if session_id else None
    if draft is None:
        await state.clear()
        await m.answer(bm.i18n.t("cc_draft_stale"))
        return
    text = (m.text or "").strip()
    if not text:
        return  # пустое сообщение — игнорируем (кнопки остаются)
    await m.answer(bm.i18n.t("cc_extracting"))
    try:
        patch = await bm.extract_campaign_settings(text, language=bm.i18n.current_lang())
    except Exception as e:
        await m.answer(
            bm.i18n.t("err_text_gen", err=bm.ux.err_text(e)), reply_markup=bm.cc_settings_kb()
        )
        return
    merged = bm._cc_apply_settings_patch(draft.wizard_state.get("settings") or {}, patch)
    await bm.CDRAFTS.patch(
        session_id, lambda s: s.__setitem__("settings", merged), expected_chat_id=m.chat.id
    )
    await m.answer(
        bm._cc_crumb(1) + bm.texts.fmt_cc_settings_summary(merged),
        reply_markup=bm.cc_settings_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "edit"))
async def cc_edit_hint(cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext) -> None:
    """§19.3: «✏️ Изменить» — подсказка формата правки (сама правка — свободным текстом в том же
    состоянии; cc_settings_edit применит patch и перерисует сводку). Ничего не меняет."""
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is not None:
        await msg.answer(bm.i18n.t("cc_edit_hint"), parse_mode=bm.ParseMode.HTML)


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "accept"))
async def cc_accept(cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext) -> None:
    """Этап 1 принят (advisory, НЕ мутация): курсор → этап 2 (ключевые слова), выходим из FSM
    (черновик в БД остаётся active и переживает рестарт)."""
    chat_id = bm._cq_chat_id(cq)
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=chat_id) if session_id else None
    if draft is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        await state.clear()
        return
    await bm.CDRAFTS.set_step(session_id, 2, expected_chat_id=chat_id)
    await state.clear()
    await cq.answer(bm.i18n.t("cc_settings_saved"))
    await bm._safe_edit(cq, bm.i18n.t("cc_settings_saved"))
    msg = bm._cq_msg(cq)
    if msg is not None:
        await bm._cc_after_settings(msg, chat_id, session_id, state)


@bm.dp.message(bm.CreateCampaignWizard.ad_url)
async def cc_ad_url(m: bm.Message, state: bm.FSMContext) -> None:
    """Final URL получен → display path (КОД) + парс страницы → генерация 15/4 → сессия курации
    (с cc_session в brief) → итог курации. Объявление кладётся в черновик на finalize (НЕ proposal)."""
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=m.chat.id) if session_id else None
    if draft is None:
        await state.clear()
        await m.answer(bm.i18n.t("cc_draft_stale"))
        return
    url = (m.text or "").strip()
    if not url.startswith(("http://", "https://")) or len(url) > 2048:
        await m.answer(bm.i18n.t("cc_bad_url"), reply_markup=bm.nav_kb())
        return
    s = draft.wizard_state.get("settings") or {}
    kw_list = list((draft.wizard_state.get("keywords") or {}).get("list") or [])
    path1, path2 = bm.build_display_path(url, kw_list, s)
    await m.answer(bm.i18n.t("cc_generating_ad"))
    # Парс посадочной (best-effort) → УТП для генерации; сбой не критичен.
    page_text = ""
    try:
        page_text = await bm.ingest.fetch_url_text(url)
    except Exception:  # noqa: BLE001 — парс страницы не критичен
        page_text = ""
    prof = await bm._cc_profile_ctx(
        draft
    )  # §20: профиль клиента → релевантность заголовков/описаний
    brief = bm.CopyBrief(
        # §19.5.2: тематика = ЧТО рекламируем (product), язык — аудитории целевой страны (Кения→en),
        # а не имя кампании / язык интерфейса — иначе тексты не по теме и на чужом языке.
        topic=(s.get("product") or s.get("campaign_name") or url),
        keywords=kw_list[:20],
        usp=(page_text[:1500] or None),  # УТП с посадочной страницы
        profile=(prof or None),  # §20: профиль клиента — отдельным первоклассным контекстом
        geo=((s.get("geo_locations") or [""])[0] or None),
        language=(s.get("target_language") or bm.i18n.current_lang()),
        tone=(s.get("tone") or None),  # P1-E: §19.5 тон-контекст (если задан в настройках)
        n_headlines=bm.RSA_MAX_HEADLINES,
        n_descriptions=bm.RSA_MAX_DESCRIPTIONS,
    )
    try:
        async with bm.ux.typing_action(m):
            gen = await bm._generate_rsa(brief)
    except Exception as e:  # LLM/сеть
        await m.answer(bm.i18n.t("err_text_gen", err=bm.ux.err_text(e)), reply_markup=bm.nav_kb())
        return
    # P1-E: та же диагностика, что в /rsa (сколько набрано/отброшено КОДОМ за длину + модерация
    # CAPS/спам) — раньше в визарде §19 не показывалась, менеджер не видел предупреждений.
    await m.answer(bm.ux.fmt_rsa_diagnostics(gen, bm.RSA_MAX_HEADLINES, bm.RSA_MAX_DESCRIPTIONS))
    if len(gen.headlines) < bm.RSA_MIN_HEADLINES or len(gen.descriptions) < bm.RSA_MIN_DESCRIPTIONS:
        await m.answer(bm.i18n.t("search_gen_empty"), reply_markup=bm.nav_kb())
        return
    rsa_session_id = await bm.SESSIONS.create(
        chat_id=m.chat.id,
        customer_id=bm.DRAFT_ACCOUNT_ID,
        campaign=(s.get("campaign_name") or ""),
        ad_group_id="",
        ad_group_name=(s.get("campaign_name") or ""),
        final_url=url,
        headlines=gen.headlines,
        descriptions=gen.descriptions,
        brief={**brief.model_dump(), "cc_session": session_id},  # ← связь с черновиком визарда
    )

    def _save_ad(st: dict) -> None:
        st["ad"].update(
            {
                "final_url": url,
                "path1": path1,
                "path2": path2,
                "rsa_session_id": rsa_session_id,
            }
        )

    await bm.CDRAFTS.patch(session_id, _save_ad, expected_chat_id=m.chat.id)
    # §19.5.2/§10: поэлементная курация (✅/✏️/❌ на каждый элемент) + батч-ряд («Доработать всё» =
    # list-UX правка, «Сгенерировать заново», «Утвердить набор»). cc_session в session.brief →
    # финал курации вернётся в визард (_cc_finalize_ad) даже после рестарта.
    await state.clear()
    session = await bm.SESSIONS.get(rsa_session_id)
    await bm._cc_rsa_present_curation(m, session)


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "skip"))
async def cc_skip(cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext) -> None:
    """⏭ Пропустить текущий этап (изображения/URL-опции). Двигаем курсор дальше."""
    chat_id = bm._cq_chat_id(cq)
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=chat_id) if session_id else None
    if draft is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        await state.clear()
        return
    await cq.answer(bm.i18n.t("cc_stage_skipped"))
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    step = draft.current_step
    if step == 2:  # пропуск ключевых слов → объявление (кампания без ключей допустима)
        await bm._cc_present_stage3(msg, chat_id, session_id, state)
        return
    if (
        step == 4
    ):  # «Готово/Пропустить» изображений → ассеты (skipped только если фото не добавляли)
        await bm.CDRAFTS.patch(
            session_id,
            lambda st: st["images"].__setitem__(
                "skipped", not (st["images"].get("media_ids") or [])
            ),
            expected_chat_id=chat_id,
        )
        await bm._cc_after_images(msg, chat_id, session_id, state)
        return
    if step == 5:  # «Готово» с ассетами → URL-опции
        await bm._cc_present_stage6(msg, chat_id, session_id, state)
        return
    if step == 6:  # пропуск URL-опций → финал
        await bm._cc_present_stage7(msg, chat_id, session_id, state)
        return
    await bm._cc_present_stage7(msg, chat_id, session_id, state)  # safety: ведём к финалу


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "kw_confirm"))
async def cc_kw_confirm(cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext) -> None:
    """§19.4: «✅ Подтвердить ключевые слова» → Этап 3 (объявление). Кнопка живёт под обзором
    списка; без сохранённых верифицированных ключей — стейл (черновик устарел/заменён)."""
    chat_id = bm._cq_chat_id(cq)
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=chat_id) if session_id else None
    msg = bm._cq_msg(cq)
    if draft is None or msg is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        return
    kw = draft.wizard_state.get("keywords") or {}
    if not (kw.get("list") and kw.get("verified")):
        await cq.answer(bm.i18n.t("cc_kw_empty"), show_alert=True)
        return
    await cq.answer()
    mt_label = (
        bm.i18n.t("cc_kw_mixed_mt")
        if kw.get("match_types")
        else bm.texts.match_type_human(kw.get("match_type") or "phrase")
    )
    await msg.answer(bm.i18n.t("cc_kw_accepted", n=len(kw["list"]), mt=mt_label))
    await bm._cc_present_stage3(msg, chat_id, session_id, state)


@bm.dp.message(bm.CreateCampaignWizard.keywords)
async def cc_keywords_text(m: bm.Message, state: bm.FSMContext) -> None:
    """Этап 2 (ввод A): свои ключи текстом ИЛИ ссылкой на Google-таблицу. Тип соответствия — по
    маркерам/инструкции (default phrase); смешанные типы сохраняются per-keyword (§19.4.1).
    Файлы XLSX/CSV/TXT принимает on_document → _cc_keywords_from_document."""
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=m.chat.id) if session_id else None
    if draft is None:
        await state.clear()
        await m.answer(bm.i18n.t("cc_draft_stale"))
        return
    text = (m.text or "").strip()
    if not text:
        await m.answer(bm.i18n.t("cc_kw_empty"), reply_markup=bm.cc_kw_kb())
        return
    from keywords.ingest import parse_keywords_text
    from reports.sheets import parse_spreadsheet_id

    default_mt = bm._cc_default_match_type(
        draft
    )  # B6: подтверждённый на Этапе 1, не хардкод phrase
    # ссылка на Google-таблицу → читаем колонку ключей (§19.4.1: со scope spreadsheets.readonly —
    # любую доступную таблицу менеджера, не только созданную ботом)
    if "spreadsheets" in text.lower() and parse_spreadsheet_id(text):
        from reports.sheets import read_keyword_column

        sid = parse_spreadsheet_id(text)
        try:
            kws_raw = await bm.asyncio.to_thread(read_keyword_column, sid)
        except Exception as e:  # noqa: BLE001 — нет scope/доступа/не наша таблица
            await m.answer(
                bm.i18n.t("cc_kw_read_failed", err=bm.ux.err_text(e)), reply_markup=bm.cc_kw_kb()
            )
            return
        kws, dropped = bm._cc_sanitize_keywords(
            kws_raw
        )  # B5: отбросить мусорные ключи из колонки A
        if dropped:
            await m.answer(bm.i18n.t("cc_kw_dropped", n=dropped))
        await bm._cc_save_keywords(m, m.chat.id, session_id, state, kws, default_mt, "sheet")
        return
    parsed = parse_keywords_text(text, default_match_type=default_mt)
    kw_list = [k.text for k in parsed]
    mt = parsed[0].match_type if parsed else default_mt
    per_kw = [k.match_type for k in parsed]  # §19.4.1: смешанные типы не схлопываем в первый
    await bm._cc_save_keywords(
        m, m.chat.id, session_id, state, kw_list, mt, "text", per_kw_mts=per_kw
    )


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "kw_generate"))
async def cc_kw_generate(
    cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext
) -> None:
    """Этап 2 (ввод B): seed-ключи → Discover → фильтр релевантности → Google Sheets → верификация.
    Если Sheets недоступен (нет scope) — fallback: берём релевантные идеи напрямую, без round-trip."""
    chat_id = bm._cq_chat_id(cq)
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=chat_id) if session_id else None
    msg = bm._cq_msg(cq)
    if draft is None or msg is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        return
    await cq.answer()
    await msg.answer(bm.i18n.t("cc_kw_generating"))
    s = draft.wizard_state.get("settings") or {}
    try:
        from ads import geo as adsgeo
        from ads.client import build_client_async
        from ads.keyword_plan import generate_keyword_ideas
        from keywords.filter import filter_relevance
        from keywords.seeds import generate_seed_keywords

        # §19.4.2: тема = ЧТО рекламируем (product), а НЕ имя кампании; язык и ГЕО — целевой
        # страны (Кения→en, geo Кения), а не интерфейса — иначе ключи нерелевантны/чужеязычны.
        topic = s.get("product") or s.get("campaign_name") or ""
        gen_lang = s.get("target_language") or bm.i18n.current_lang()
        geo_ids = adsgeo.geo_ids_for_settings(s)
        # K: РФ/РБ не обслуживаются Keyword Planner — сообщаем и подбираем без гео (иначе падало
        # «The input has an invalid value»); generate_keyword_ideas сам выкинет необслуживаемые.
        if geo_ids and adsgeo.has_non_serviceable_geo(geo_ids):
            await msg.answer(bm.i18n.t("kw_geo_dropped"))
        prof = await bm._cc_profile_ctx(draft)  # §20: профиль клиента → релевантность подбора
        site = await bm._cc_profile_site(draft)  # §19.4.2: seed + URL — сайт из §20-профиля
        seeds = await generate_seed_keywords(topic=topic, url=site, profile=prof, language=gen_lang)
        # P1-7: метрики Keyword Planner берём с ЖИВОГО аккаунта (read-only) — на Draft/тесте объёмы
        # пустые → мало идей и мёртвая сортировка. Идеи ключей рыночные (не данные аккаунта), поэтому
        # любой живой аккаунт корректен; замок мутаций не трогается (кампания всё равно строится на Draft).
        metrics_acct, kw_is_live = await bm._keyword_metrics_account(chat_id)
        if not kw_is_live:
            await msg.answer(bm.i18n.t("kw_metrics_test_note"))
        client = await build_client_async(metrics_acct)  # холодная сборка — вне loop
        ideas = await bm.run_ads_read_call(
            generate_keyword_ideas,
            client,
            metrics_acct,
            seeds=seeds,
            url=site,  # §19.4.2: keyword_and_url_seed (seed 10 + URL), None ⇒ только seeds
            language=adsgeo.keyword_ideas_lang(gen_lang),
            geo_ids=geo_ids,  # страна кампании; () (неизвестна) → глобально, а не дефолт-Украина
            label="generate_keyword_ideas",
        )
        relevance = await filter_relevance(
            texts=[i.text for i in ideas], topic=topic, profile=prof, language=gen_lang
        )
    except Exception as e:  # noqa: BLE001 — сеть/квота/LLM
        await msg.answer(bm.i18n.t("err_kw", err=bm.ux.err_text(e)), reply_markup=bm.cc_kw_kb())
        return
    if not ideas:
        await msg.answer(bm.i18n.t("kw_empty"), reply_markup=bm.cc_kw_kb())
        return
    # §19.4: минус-слова — ADVISORY (сам НЕ добавляю; добавление — отдельной командой за гейтом).
    try:
        from keywords.cluster import suggest_negative_keywords

        negs = await suggest_negative_keywords(
            topic, [i.text for i in ideas], language=gen_lang, limit=10
        )
        if negs:
            await msg.answer(
                bm.i18n.t("cc_kw_negatives_hint", negs=bm.texts.esc(", ".join(negs))),
                parse_mode=bm.ParseMode.HTML,
            )
    except Exception:  # noqa: BLE001 — совет не критичен
        pass
    # Пытаемся выгрузить в Google Sheets для ручной верификации; при сбое — fallback без round-trip.
    try:
        from reports.sheets import publish_keywords_to_sheets

        title = f"kw-{(topic or 'campaign')[:40]}"
        url, sid = await bm.asyncio.to_thread(
            publish_keywords_to_sheets, ideas, relevance, title=title
        )
    except Exception as e:  # noqa: BLE001 — нет drive.file scope/сети → fallback
        await msg.answer(bm.i18n.t("cc_kw_sheet_failed", err=bm.ux.err_text(e)))
        relevant = [i.text for i in ideas if relevance.get(i.text, True)]
        # B6: тип соответствия — подтверждённый на Этапе 1, не хардкод phrase
        await bm._cc_save_keywords(
            msg, chat_id, session_id, state, relevant, bm._cc_default_match_type(draft), "generated"
        )
        return
    # P0-2: сохраняем сгенерированный (отфильтрованный по релевантности) список СРАЗУ, а не только
    # после возврата ссылки на таблицу. Раньше пропуск round-trip'а → кампания создавалась без
    # ключей («ключи не добавились»). verified=False: ручная правка таблицы остаётся ОПЦИОНАЛЬНЫМ
    # уточнением (вернёт ссылку → перезапишем выверенным списком в cc_kw_verify).
    # B3-resume: sheet_url тоже в черновик — resume после рестарта переоткроет верификацию с той
    # же ссылкой (см. _cc_present_stage2), а не молча вернёт на пустой Этап 2.
    relevant = [i.text for i in ideas if relevance.get(i.text, True)]
    default_mt = bm._cc_default_match_type(draft)
    await bm.CDRAFTS.patch(
        session_id,
        lambda st: st["keywords"].update(
            {
                "sheet_id": sid,
                "sheet_url": url,
                "list": relevant,
                "match_type": default_mt,
                "match_types": None,
                "source": "generated",
                "verified": False,
            }
        ),
        expected_chat_id=chat_id,
    )
    await state.set_state(bm.CreateCampaignWizard.kw_verify)
    await state.update_data(cc_session=session_id)
    await msg.answer(bm.i18n.t("cc_kw_sheet_ready", url=url))
    await msg.answer(
        bm.i18n.t("cc_kw_verify_prompt_v2", n=len(relevant)),
        reply_markup=bm.cc_kw_verify_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.CreateCampaignWizard.kw_verify)
async def cc_kw_verify(m: bm.Message, state: bm.FSMContext) -> None:
    """Этап 2: менеджер прислал ссылку на отредактированную таблицу → читаем верифицированный список."""
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=m.chat.id) if session_id else None
    if draft is None:
        await state.clear()
        await m.answer(bm.i18n.t("cc_draft_stale"))
        return
    from reports.sheets import parse_spreadsheet_id, read_keyword_column

    sid = parse_spreadsheet_id(m.text or "")
    if not sid:
        await m.answer(bm.i18n.t("cc_kw_not_a_link"), reply_markup=bm.nav_kb())
        return
    # §19.4.2 (round-trip): менеджер обязан вернуть ТУ ЖЕ таблицу, что бот создал для верификации.
    # Чужая ссылка — не «верифицированный список» (можно перепутать таблицу) → просим правильную.
    expected_sid = (draft.wizard_state.get("keywords") or {}).get("sheet_id")
    if expected_sid and sid != expected_sid:
        await m.answer(
            bm.i18n.t("cc_kw_wrong_sheet", sid=bm.texts.esc(str(expected_sid)[-6:])),
            reply_markup=bm.nav_kb(),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    try:
        kws_raw = await bm.asyncio.to_thread(read_keyword_column, sid)
    except Exception as e:  # noqa: BLE001
        await m.answer(
            bm.i18n.t("cc_kw_read_failed", err=bm.ux.err_text(e)), reply_markup=bm.nav_kb()
        )
        return
    kws, dropped = bm._cc_sanitize_keywords(
        kws_raw
    )  # B5: отбросить мусорные ключи из отредактированной таблицы
    if dropped:
        await m.answer(bm.i18n.t("cc_kw_dropped", n=dropped))
    # B6: тип соответствия — подтверждённый на Этапе 1, не хардкод phrase
    await bm._cc_save_keywords(
        m, m.chat.id, session_id, state, kws, bm._cc_default_match_type(draft), "sheet"
    )


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "kw_use_generated"))
async def cc_kw_use_generated(
    cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext
) -> None:
    """P0-2: «✅ Использовать эти ключи» — взять уже сохранённый сгенерированный список без ручной
    правки Google-таблицы и перейти к обзору (Этап 2 → подтверждение ключей)."""
    chat_id = bm._cq_chat_id(cq)
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=chat_id) if session_id else None
    msg = bm._cq_msg(cq)
    if draft is None or msg is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        return
    await cq.answer()
    kw = draft.wizard_state.get("keywords") or {}
    kw_list = kw.get("list") or []
    if not kw_list:
        await msg.answer(bm.i18n.t("cc_kw_empty"), reply_markup=bm.cc_kw_kb())
        return
    await bm._cc_save_keywords(
        msg,
        chat_id,
        session_id,
        state,
        kw_list,
        kw.get("match_type") or bm._cc_default_match_type(draft),
        "generated",
    )


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "use_assets"))
async def cc_use_assets(cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext) -> None:
    """Этап 5: читаем существующие ассеты аккаунта и кладём в черновик ссылки для переиспользования."""
    chat_id = bm._cq_chat_id(cq)
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=chat_id) if session_id else None
    msg = bm._cq_msg(cq)
    if draft is None or msg is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        return
    await cq.answer()
    # Переиспользуем ТОЛЬКО ассеты Draft: кампания создаётся на Draft, а CampaignAsset не может
    # ссылаться на ассет другого аккаунта (превью-аккаунт дал бы чужие resource_name → молчаливый
    # no-op). Поэтому читаем строго с DRAFT_ACCOUNT_ID, а не с preview_customer_id.
    read_cid = bm.DRAFT_ACCOUNT_ID
    rows: list = []
    try:
        from ads.client import build_client_async
        from ads.read import list_account_assets

        client = await build_client_async(read_cid)
        rows = await bm.run_ads_read_call(
            list_account_assets, client, read_cid, label="list_account_assets"
        )
    except Exception:  # noqa: BLE001 — нет доступа/сбой → просто без переиспользования
        rows = []
    if not rows:
        await msg.answer(bm.i18n.t("cc_assets_none"))
        await bm._cc_present_stage6(msg, chat_id, session_id, state)
        return
    reuse = [
        {"asset_resource_name": r.asset_resource_name, "field_type": r.field_type} for r in rows
    ]
    await bm.CDRAFTS.patch(
        session_id,
        lambda st: st["assets"].__setitem__("reuse_links", reuse),
        expected_chat_id=chat_id,
    )
    await msg.answer(bm.i18n.t("cc_assets_reused", n=len(reuse)))
    await msg.answer(
        bm.i18n.t("cc_assets_prompt"), reply_markup=bm.cc_assets_kb(), parse_mode=bm.ParseMode.HTML
    )


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "add_assets"))
async def cc_add_assets(cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext) -> None:
    """Этап 5: «➕ Добавить новый ассет» → пикер типа (§19.7.2 автогенерация)."""
    await cq.answer()
    await bm._safe_edit(
        cq,
        bm.i18n.t("cc_assets_pick_type"),
        reply_markup=bm.cc_asset_types_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "assets_back"))
async def cc_assets_back(
    cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext
) -> None:
    await cq.answer()
    await bm._safe_edit(
        cq,
        bm.i18n.t("cc_assets_prompt"),
        reply_markup=bm.cc_assets_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "asset_type"))
async def cc_asset_type(cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext) -> None:
    """Этап 5: тип выбран → автогенерация наполнения → накапливаем в черновик (assets.new). Реальная
    привязка — на финальном composite-создании (confirm-гейт). Возврат в меню ассетов (можно ещё)."""
    chat_id = bm._cq_chat_id(cq)
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=chat_id) if session_id else None
    msg = bm._cq_msg(cq)
    if draft is None or msg is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        return
    family = callback_data.sub
    await cq.answer()
    if family == "business_logo":  # §19.7.1: логотип — это ФОТО, не текстовая автогенерация
        await state.set_state(bm.CreateCampaignWizard.asset_logo)
        await state.update_data(cc_session=session_id)
        await msg.answer(bm.i18n.t("cc_asset_logo_prompt"), reply_markup=bm.nav_kb())
        return
    await msg.answer(bm.i18n.t("cc_asset_generating"))
    s = draft.wizard_state.get("settings") or {}
    ad = draft.wizard_state.get("ad") or {}
    site_pages: list[dict] = []  # §20.6: карта страниц краула (только для family=sitelinks)
    try:
        from clients.profile_assets import PROFILE_ASSET_FAMILIES, build_profile_asset

        if family in PROFILE_ASSET_FAMILIES:
            # §19.7.2: call/price/promotion несут ФАКТЫ (телефон/цены/скидка) — строим из РЕАЛЬНОГО
            # профиля клиента (§20), НЕ через LLM. Нет данных → ValueError → пропуск (не выдумываем).
            cid = draft.preview_customer_id or bm.DRAFT_ACCOUNT_ID
            profile = await bm.CLIENTS.get_by_account(cid) or {}
            spec = build_profile_asset(
                family,
                profile,
                final_url=ad.get("final_url"),
                country_code=(s.get("geo_country_code") or "UA"),
                language_code=(s.get("target_language") or "uk"),
                currency_hint=s.get("currency"),
            )
        else:
            from adcopy.assets_gen import generate_asset

            # §20.6: для sitelinks подаём карту РЕАЛЬНЫХ страниц последнего краула (client_site_pages)
            # — ссылки ведут на существующие url, а не выдуманные LLM. Сбой чтения → [] (fallback).
            if family == "sitelinks":
                try:
                    site_pages = await bm.CLIENTS.top_site_pages(
                        draft.preview_customer_id or bm.DRAFT_ACCOUNT_ID
                    )
                except Exception:  # noqa: BLE001 — карта страниц опциональна, генерацию не роняем
                    site_pages = []
            spec = await generate_asset(
                family,
                # §19.7.2: как заголовки/описания — наполнение по товару (product) на языке аудитории
                # целевой страны, а не по имени кампании / языку интерфейса (иначе ассеты не по теме).
                topic=(s.get("product") or s.get("campaign_name") or ""),
                url=ad.get("final_url"),
                profile=await bm._cc_profile_ctx(
                    draft
                ),  # §20: наполнение ассетов из профиля клиента
                language=(s.get("target_language") or bm.i18n.current_lang()),
                site_pages=site_pages or None,
            )
    except Exception as e:  # noqa: BLE001 — LLM/валидация наполнения/нет данных профиля
        await msg.answer(bm.i18n.t("cc_asset_gen_failed", err=bm.ux.err_text(e)))
        await msg.answer(
            bm.i18n.t("cc_assets_pick_type"),
            reply_markup=bm.cc_asset_types_kb(),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    snap = await bm.CDRAFTS.patch(
        session_id, lambda st: st["assets"]["new"].append(spec), expected_chat_id=chat_id
    )
    n = len(((snap.wizard_state if snap else {}).get("assets") or {}).get("new") or [])
    await msg.answer(bm.i18n.t("cc_asset_added", label=bm.texts.fmt_asset_spec_label(spec), n=n))
    if family == "sitelinks" and site_pages:
        # §20.6: честно сообщаем, что ссылки — из реальной карты сайта (краул), не выдуманы
        await msg.answer(bm.i18n.t("cc_asset_sitelinks_from_crawl", n=len(site_pages)))
    await msg.answer(
        bm.i18n.t("cc_assets_prompt"), reply_markup=bm.cc_assets_kb(), parse_mode=bm.ParseMode.HTML
    )


@bm.dp.message(bm.CreateCampaignWizard.url_options)
async def cc_url_text(m: bm.Message, state: bm.FSMContext) -> None:
    """Этап 6: «tracking | suffix» → url_options. Валидирует КОД (ads.mutations._validate_url_options)."""
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=m.chat.id) if session_id else None
    if draft is None:
        await state.clear()
        await m.answer(bm.i18n.t("cc_draft_stale"))
        return
    parts = [p.strip() for p in (m.text or "").split("|")]
    url_opts: dict = {}
    if parts and parts[0]:
        url_opts["tracking_url_template"] = parts[0]
    if len(parts) > 1 and parts[1]:
        url_opts["final_url_suffix"] = parts[1]
    if len(parts) > 2 and parts[2]:
        # §19.8: custom parameters — 3-е поле «key=value, key2=value2» (схема/мутация давно
        # поддерживают; раньше UI их не собирал — аудит M3.10). Валидность ключей проверит
        # _validate_url_options (латиница/цифры/_, ≤8 штук, значение ≤250).
        custom: dict[str, str] = {}
        for pair in parts[2].split(","):
            k, sep, v = pair.partition("=")
            if sep and k.strip():
                custom[k.strip().lstrip("{_").rstrip("}")] = v.strip()
        if custom:
            url_opts["custom_parameters"] = custom
    try:
        from ads.mutations import _validate_url_options

        _validate_url_options(url_opts)
    except Exception as e:  # noqa: BLE001 — невалидные опции → retry
        await m.answer(bm.i18n.t("cc_url_bad", err=bm.ux.err_text(e)), reply_markup=bm.cc_skip_kb())
        return
    await bm.CDRAFTS.patch(
        session_id, lambda st: st.__setitem__("url_options", url_opts), expected_chat_id=m.chat.id
    )
    await m.answer(bm.i18n.t("cc_url_saved"))
    await bm._cc_present_stage7(m, m.chat.id, session_id, state)


@bm.dp.message(bm.CreateCampaignWizard.final)
async def cc_final_edit(m: bm.Message, state: bm.FSMContext) -> None:
    """Этап 7: правка командой в свободной форме (§19.8). Порядок (3B, footgun-фикс):
    1) команда с МАРКЕРОМ настроек (бюджет/ставка/гео/язык/…) или чисто-числовой парой «с 40 на
       60» → СНАЧАЛА LLM-извлечение настроек (раньше literal replace «40»→«60» тихо портил
       заголовок «Скидка 40%», а бюджет не менялся);
    2) чистый текст «смени X на Y» → буквальная замена по текстам объявления/ассетов/URL
       (КОД, ре-валидация длин);
    3) settings-извлечение дало пусто, а пара была → фолбэк на literal (путь «смени цену
       „от 40$“ на „от 60$“» в price-ассете сохранён);
    4) оба мимо → честное «не понял» (НЕ пере-сводка, будто что-то поменялось).
    Это правка черновика, НЕ мутация (confirmation_id не тратится)."""
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=m.chat.id) if session_id else None
    if draft is None:
        await state.clear()
        await m.answer(bm.i18n.t("cc_draft_stale"))
        return
    text = (m.text or "").strip()
    if not text:
        return
    from agent.campaign_edit import (
        apply_text_replace,
        is_numeric_pair,
        is_settings_edit,
        parse_replace_edit,
    )

    rep = parse_replace_edit(text)
    settings_intent = is_settings_edit(text) or (rep is not None and is_numeric_pair(*rep))

    async def _try_replace() -> bool:
        if rep is None:
            return False
        old, new = rep
        applied = {"n": 0}

        def _do(st: dict) -> None:
            applied["n"] = apply_text_replace(st, old, new)

        await bm.CDRAFTS.patch(session_id, _do, expected_chat_id=m.chat.id)
        return bool(applied["n"])

    # 1/2) чистый текст без settings-намерения → literal replace сразу
    if rep is not None and not settings_intent:
        if await _try_replace():
            await bm._cc_resummarize(m, session_id, m.chat.id)
            return
        # ничего не заменилось → пробуем как правку настроек ниже

    # settings-извлечение (LLM)
    await m.answer(bm.i18n.t("cc_extracting"))
    try:
        patch = await bm.extract_campaign_settings(text, language=bm.i18n.current_lang())
    except Exception as e:  # noqa: BLE001
        await m.answer(
            bm.i18n.t("err_text_gen", err=bm.ux.err_text(e)), reply_markup=bm.cc_final_kb()
        )
        return
    if patch.is_empty():
        # 3) извлечение пустое: если была пара «X на Y» с settings-маркером — фолбэк на literal
        if settings_intent and rep is not None and await _try_replace():
            await bm._cc_resummarize(m, session_id, m.chat.id)
            return
        # 4) оба мимо → честно признаёмся (пере-сводка создала бы иллюзию применённой правки)
        await m.answer(bm.i18n.t("cc_edit_not_understood"), reply_markup=bm.cc_final_kb())
        return
    merged = bm._cc_apply_settings_patch(draft.wizard_state.get("settings") or {}, patch)
    await bm.CDRAFTS.patch(
        session_id, lambda st: st.__setitem__("settings", merged), expected_chat_id=m.chat.id
    )
    await bm._cc_resummarize(m, session_id, m.chat.id)


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "create"))
async def cc_create(cq: bm.CallbackQuery, callback_data: bm.CcCB, state: bm.FSMContext) -> None:
    """Этап 7: «✅ Создать черновик» → ОДИН composite proposal create_search_campaign (всё PAUSED,
    confirmation_id + audit). Единственная реальная мутация визарда; запуск — отдельной командой."""
    chat_id = bm._cq_chat_id(cq)
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await bm.CDRAFTS.get(session_id, expected_chat_id=chat_id) if session_id else None
    msg = bm._cq_msg(cq)
    if draft is None or msg is None:
        await cq.answer(bm.i18n.t("cc_draft_stale"), show_alert=True)
        await state.clear()
        return
    ad = draft.wizard_state.get("ad") or {}
    if (
        not ad.get("final_url")
        or len(ad.get("headlines") or []) < bm.RSA_MIN_HEADLINES
        or len(ad.get("descriptions") or []) < bm.RSA_MIN_DESCRIPTIONS
    ):
        await cq.answer(
            bm.i18n.t(
                "rsa_below_min",
                h=len(ad.get("headlines") or []),
                d=len(ad.get("descriptions") or []),
            ),
            show_alert=True,
        )
        return
    params = bm._cc_build_create_params(draft)
    try:
        validated = bm.SCHEMAS["create_search_campaign"](**params).model_dump()
    except Exception as e:  # noqa: BLE001 — валидация схемы (длины/URL/бюджет)
        await cq.answer(bm.i18n.t("cb_error", kind=type(e).__name__), show_alert=True)
        return
    # P1-A: на карточке ✅-гейта показываем и таргетинг (гео/язык/сети/расписание/даты) — менеджер
    # видит полное «было→станет» перед созданием (§5/§19.8). ad_schedule — человекочит. строка из
    # настроек черновика (в validated хранится структурой ad_schedule_blocks).
    _cc_settings = draft.wizard_state.get("settings") or {}
    summary = bm.texts.fmt_search_proposal_summary(
        validated["campaign_name"],
        validated["final_url"],
        validated["budget_daily_micros"] / 1_000_000,
        validated["headlines"],
        validated["descriptions"],
        validated["keywords"],
        validated["match_type"],
        geo_locations=validated.get("geo_locations"),
        languages=validated.get("languages"),
        networks=validated.get("networks"),
        ad_schedule=_cc_settings.get("ad_schedule"),
        start_date=validated.get("start_date"),
        end_date=validated.get("end_date"),
    )
    await cq.answer()
    # B10: список ключей мог быть обрезан до потолка схемы — не молчим, сообщаем менеджеру в чат
    # (раньше был только log.warning), чтобы «пропажа» части ключей не осталась незамеченной.
    kw_total = len((draft.wizard_state.get("keywords") or {}).get("list") or [])
    if kw_total > bm.MAX_CAMPAIGN_KEYWORDS:
        await msg.answer(
            bm.i18n.t("cc_keywords_truncated", total=kw_total, kept=bm.MAX_CAMPAIGN_KEYWORDS)
        )
    cid = bm.uuid.uuid4().hex
    # B9: НЕ гасим черновик здесь — лишь связываем с proposal. finish произойдёт при УСПЕШНОМ
    # подтверждении (_do_confirm); при reject черновик остаётся active и возобновляем «▶️ Продолжить».
    # Связка cid→draft живёт в params proposal (БД, прецедент '_before') — переживает рестарт;
    # execute_confirmed читает params по явным ключам, лишний '_cc_draft' инертен.
    validated["_cc_draft"] = session_id
    await state.clear()
    await bm._present_proposal(
        msg,
        chat_id=chat_id,
        operation="create_search_campaign",
        params=validated,
        summary=summary,
        cid=cid,
    )


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "launch"))
async def cc_launch(cq: bm.CallbackQuery, callback_data: bm.CcCB) -> None:
    """§19.8: «🚀 Запустить» после создания черновика → resume_campaign proposal (confirm-гейт).
    Запуск = отдельная прямая команда: кнопка лишь СОЗДАЁТ черновик «PAUSED→ENABLED», исполняется
    только после ✅ (та же ветка, что /campaigns → «Возобновить»). Замок аккаунта не трогаем.

    Имя кампании резолвим по sub=confirmation_id ПРИМЕНЁННОГО create-proposal из БД (переживает
    рестарт процесса); legacy-кнопки без sub — из _CC_LAUNCH_CACHE (один релиз). Гарды по sub:
    владение (chat_id), op ∈ _CREATE_CAMPAIGN_OPS, status='applied', одноразовость в процессе."""
    chat_id = bm._cq_chat_id(cq)
    name: str | None = None
    create_cid = (callback_data.sub or "").strip()
    if create_cid:
        if (
            create_cid in bm._CC_LAUNCH_DONE
        ):  # одноразовость: не минтим второй запуск той же кнопкой
            await cq.answer(bm.i18n.t("cc_launch_stale"), show_alert=True)
            return
        snap = await bm.STORE.get_confirmed(create_cid)
        if (
            snap is None
            or snap.chat_id != chat_id  # гард владения: чужая кнопка не запускает чужую кампанию
            or snap.operation not in bm._CREATE_CAMPAIGN_OPS
            or snap.status != "applied"  # запуск только РЕАЛЬНО созданного (не pending/failed)
        ):
            await cq.answer(bm.i18n.t("cc_launch_stale"), show_alert=True)
            return
        name = (snap.params or {}).get("campaign_name") or None
    else:  # legacy-кнопка (до деплоя restart-durability) — процессный кэш
        name = bm._CC_LAUNCH_CACHE.get(chat_id)
    if not name:
        await cq.answer(bm.i18n.t("cc_launch_stale"), show_alert=True)
        return
    try:
        cid, op, params, summary = bm._build_proposal("resume_campaign", campaign=name)
    except Exception as e:  # noqa: BLE001 — валидация схемы
        await cq.answer(bm.i18n.t("cb_error", kind=type(e).__name__), show_alert=True)
        return
    await cq.answer()
    if create_cid:
        bm._CC_LAUNCH_DONE.add(create_cid)  # одноразово в процессе (после рестарта — гейт защитит)
    bm._CC_LAUNCH_CACHE.pop(chat_id, None)  # одноразово: не запускаем дважды по старой кнопке
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    await bm._present_proposal(
        msg, chat_id=chat_id, operation=op, params=params, summary=summary, cid=cid
    )


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "view_camps"))
async def cc_view_camps(cq: bm.CallbackQuery, callback_data: bm.CcCB) -> None:
    """§UX «что дальше»: показать список кампаний (чистое чтение, как /campaigns)."""
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is not None:
        await bm._send_campaigns(msg, bm._cq_chat_id(cq))


@bm.dp.callback_query(bm.CcCB.filter(bm.F.action == "hint_neg"))
async def cc_hint_neg(cq: bm.CallbackQuery, callback_data: bm.CcCB) -> None:
    """§UX «что дальше»: подсказка, как добавить минус-слова (ТЕКСТ, proposal НЕ минтится —
    агент предлагает, добавляет только по отдельной команде за confirm-гейтом, ТЗ §7/§19.4).
    Имя кампании — из применённого create-proposal по sub (как cc_launch), фолбэк — многоточие."""
    chat_id = bm._cq_chat_id(cq)
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    name = ""
    sub = (callback_data.sub or "").strip()
    if sub:
        snap = await bm.STORE.get_confirmed(sub)
        if snap is not None and snap.chat_id == chat_id:
            name = (snap.params or {}).get("campaign_name") or ""
    name = name or bm._CC_LAUNCH_CACHE.get(chat_id) or "…"
    await msg.answer(
        bm.i18n.t("cc_neg_kw_hint", name=bm.texts.esc(name)),
        parse_mode=bm.ParseMode.HTML,
    )
