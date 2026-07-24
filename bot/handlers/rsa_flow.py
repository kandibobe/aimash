"""RSA §10: /rsa визард + поэлементная курация + list-UX (paste-back до on_text!)

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.message(bm.F.text.in_(bm.BTN_RSA_ALL))
async def btn_rsa(m: bm.Message, state: bm.FSMContext) -> None:
    """Кнопка «Тексты (RSA)» = /rsa: визард выбора кампании → генерация → курация."""
    await bm.rsa_cmd(m, state)


@bm.dp.message(bm.Command("rsa"))
async def rsa_cmd(m: bm.Message, state: bm.FSMContext) -> None:
    """Визард генерации RSA: выбор кампании → группы → бриф → курация → создание."""
    await state.clear()
    try:
        from ads.client import build_client_async
        from ads.read import list_campaigns

        acct = await bm._active_read_account(
            m.chat.id
        )  # §8: RSA на активном аккаунте, не хардкод Draft
        client = await build_client_async(acct)
        # как остальной read-слой: таймаут+ретрай транзиентных под семафором Google Ads
        # (а не «голый» to_thread — иначе зависший SearchStream не капается и копит in-flight).
        # B2: RSA живёт только в Search-кампаниях → показываем ТОЛЬКО их (иначе объявление уходило
        # в не-Search группу и Google отвергал «operation not allowed for the given context»).
        camps = await bm.run_ads_read_call(
            list_campaigns,
            client,
            acct,
            label="list_campaigns",
            channel_type="SEARCH",
        )
    except Exception as e:  # сеть/доступ/SDK
        await m.answer(bm.i18n.t("err_campaigns", err=bm.ux.err_text(e)))
        return
    if not camps:
        await m.answer(bm.i18n.t("no_campaigns"))
        return
    bm._RSA_CAMP_CACHE[m.chat.id] = camps
    # N5: пикер выбора кампании — кнопочный экран; ставим state, чтобы текст/URL тут не утекал в агента
    # (гард on_text: активный state без своего текст-хендлера → подсказка «заверши шаг»).
    await state.set_state(bm.RsaWizard.picking)
    await m.answer(
        bm.i18n.t("rsa_pick_campaign"),
        reply_markup=bm.rsa_pick_campaigns_kb(camps),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.RsaPickCB.filter(bm.F.what == "camp"))
async def rsa_pick_camp(
    cq: bm.CallbackQuery, callback_data: bm.RsaPickCB, state: bm.FSMContext
) -> None:
    camps = bm._RSA_CAMP_CACHE.get(bm._cq_chat_id(cq))
    if not bm._valid_idx(camps, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    await bm._rsa_resolve_after_campaign(
        msg, bm._cq_chat_id(cq), camps[callback_data.idx]["name"], state
    )


@bm.dp.callback_query(bm.RsaPickCB.filter(bm.F.what == "ag"))
async def rsa_pick_ag(
    cq: bm.CallbackQuery, callback_data: bm.RsaPickCB, state: bm.FSMContext
) -> None:
    groups = bm._RSA_AG_CACHE.get(bm._cq_chat_id(cq))
    if not bm._valid_idx(groups, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    await cq.answer()
    g = groups[callback_data.idx]
    await state.update_data(ad_group_id=str(g["id"]), ad_group_name=g["name"])
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    await bm._rsa_after_adgroup(msg, bm._cq_chat_id(cq), state)


# FSM-хендлеры сообщений — ОБЯЗАТЕЛЬНО до общего on_text (иначе перехватит свободный текст).
@bm.dp.message(bm.RsaWizard.awaiting_brief)
async def rsa_brief(m: bm.Message, state: bm.FSMContext) -> None:
    topic, url = bm._parse_brief_text(m.text or "")
    if not url or not topic:
        await m.answer(bm.i18n.t("rsa_bad_url"), reply_markup=bm.nav_kb())
        return  # остаёмся в состоянии — retry-подсказка несёт «✖ Отмена» (не застрять)
    await state.update_data(topic=topic, final_url=url)
    await bm._rsa_generate_and_start(m, m.chat.id, state)


@bm.dp.message(bm.RsaRefine.awaiting_text)
async def rsa_refine_text(m: bm.Message, state: bm.FSMContext) -> None:
    data = await state.get_data()
    cid, kind, idx = data.get("cid"), data.get("kind"), data.get("idx")
    session = await bm.SESSIONS.get(cid, expected_chat_id=m.chat.id) if cid else None
    if session is None or not isinstance(cid, str) or kind is None or idx is None:
        await state.clear()
        await m.answer(bm.i18n.t("rsa_session_stale"))
        return
    try:
        new_text = await bm.refine_element(session.brief, kind, m.text or "")
    except Exception as e:  # LLM/сеть
        await m.answer(bm.i18n.t("err_refine", err=bm.ux.err_text(e)), reply_markup=bm.nav_kb())
        return  # остаёмся в состоянии — retry с «✖ Отмена» (выход из курации в меню)
    valid_kind = "headline" if kind == "h" else "description"
    ok, n = bm.rsa_validate(new_text, valid_kind)
    if not ok:
        limit = 30 if kind == "h" else 90
        await m.answer(bm.i18n.t("rsa_refine_too_long", n=n, limit=limit), reply_markup=bm.nav_kb())
        return  # остаёмся в состоянии — пользователь пришлёт другую правку (или «✖ Отмена»)
    await state.clear()
    session = await bm.SESSIONS.replace_element(
        cid, kind, idx, new_text, expected_chat_id=m.chat.id
    )
    if session is None:  # сессия исчезла между правкой и записью — не падаем
        await m.answer(bm.i18n.t("rsa_session_stale"))
        return
    items = session.items(kind)
    await m.answer(
        bm.texts.fmt_rsa_element(
            kind, idx, len(items), items[idx], session.campaign, session.ad_group_name
        ),
        reply_markup=bm.rsa_item_kb(cid, kind, idx, wizard=bm._rsa_is_wizard(session)),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "approve"))
async def rsa_approve(cq: bm.CallbackQuery, callback_data: bm.RsaCB) -> None:
    session = await bm.SESSIONS.set_state(
        callback_data.cid,
        callback_data.kind,
        callback_data.idx,
        "approved",
        expected_chat_id=bm._cq_chat_id(cq),  # гард владения: только своя сессия курации
    )
    if session is None:
        await cq.answer(bm.i18n.t("rsa_session_stale"), show_alert=True)
        return
    await cq.answer(bm.i18n.t("cb_approved"))
    await bm._rsa_edit(cq, session)


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "reject"))
async def rsa_reject(cq: bm.CallbackQuery, callback_data: bm.RsaCB) -> None:
    session = await bm.SESSIONS.set_state(
        callback_data.cid,
        callback_data.kind,
        callback_data.idx,
        "rejected",
        expected_chat_id=bm._cq_chat_id(cq),  # гард владения
    )
    if session is None:
        await cq.answer(bm.i18n.t("rsa_session_stale"), show_alert=True)
        return
    await cq.answer(bm.i18n.t("cb_rejected"))
    await bm._rsa_edit(cq, session)


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "approveall"))
async def rsa_approveall(cq: bm.CallbackQuery, callback_data: bm.RsaCB) -> None:
    session = await bm.SESSIONS.approve_all_valid(
        callback_data.cid, expected_chat_id=bm._cq_chat_id(cq)
    )
    if session is None:
        await cq.answer(bm.i18n.t("rsa_session_stale"), show_alert=True)
        return
    await cq.answer(bm.i18n.t("cb_approved_all"))
    await bm._rsa_edit_overview(cq, session)


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "review"))
async def rsa_review(cq: bm.CallbackQuery, callback_data: bm.RsaCB) -> None:
    session = await bm.SESSIONS.get(callback_data.cid, expected_chat_id=bm._cq_chat_id(cq))
    if session is None:
        await cq.answer(bm.i18n.t("rsa_session_stale"), show_alert=True)
        return
    await cq.answer()
    await bm._rsa_edit(cq, session)  # _rsa_render покажет следующий pending


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "overview"))
async def rsa_to_overview(cq: bm.CallbackQuery, callback_data: bm.RsaCB) -> None:
    session = await bm.SESSIONS.get(callback_data.cid, expected_chat_id=bm._cq_chat_id(cq))
    if session is None:
        await cq.answer(bm.i18n.t("rsa_session_stale"), show_alert=True)
        return
    await cq.answer()
    await bm._rsa_edit_overview(cq, session)


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "refine"))
async def rsa_refine(cq: bm.CallbackQuery, callback_data: bm.RsaCB, state: bm.FSMContext) -> None:
    session = await bm.SESSIONS.get(callback_data.cid, expected_chat_id=bm._cq_chat_id(cq))
    if session is None:
        await cq.answer(bm.i18n.t("rsa_session_stale"), show_alert=True)
        return
    await state.set_state(bm.RsaRefine.awaiting_text)
    await state.update_data(cid=callback_data.cid, kind=callback_data.kind, idx=callback_data.idx)
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is not None:
        await msg.answer(bm.i18n.t("rsa_refine_prompt"))


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "aslist"))
async def rsa_aslist(cq: bm.CallbackQuery, callback_data: bm.RsaCB, state: bm.FSMContext) -> None:
    """§10 list-UX «✅ Использовать как есть» / §19.5.2 «✅ Утвердить набор»: одобрить всё валидное
    (отклонённые вручную НЕ трогаем) → к завершению (confirm-гейт или черновик визарда)."""
    session = await bm.SESSIONS.approve_all_valid(
        callback_data.cid, expected_chat_id=bm._cq_chat_id(cq)
    )
    if session is None:
        await cq.answer(bm.i18n.t("rsa_session_stale"), show_alert=True)
        return
    if not session.can_finalize():
        h, d = session.counts()
        await cq.answer(bm.i18n.t("rsa_below_min", h=h, d=d), show_alert=True)
        return
    await cq.answer()
    await state.clear()
    msg = bm._cq_msg(cq)
    if msg is not None:
        await bm._rsa_present_final(msg, bm._cq_chat_id(cq), session, state)


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "editall"))
async def rsa_editall(cq: bm.CallbackQuery, callback_data: bm.RsaCB, state: bm.FSMContext) -> None:
    """§19.5.2 «✏️ Доработать всё»: переключить курацию в list-UX — весь набор редактируемым
    списком, менеджер правит текст и присылает обратно (rsa_list_edited). Мутаций нет."""
    session = await bm.SESSIONS.get(callback_data.cid, expected_chat_id=bm._cq_chat_id(cq))
    if session is None:
        await cq.answer(bm.i18n.t("rsa_session_stale"), show_alert=True)
        return
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is not None:
        await bm._rsa_present_list(msg, session, state)


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "regen"))
async def rsa_regen(cq: bm.CallbackQuery, callback_data: bm.RsaCB, state: bm.FSMContext) -> None:
    """§19.5.2 «🔁 Сгенерировать заново»: новый набор по ТОМУ ЖЕ брифу → replace_all(mark="pending")
    → курация заново. Только текст (LLM) — ни SDK, ни proposal; двойной клик гасит Throttle."""
    chat_id = bm._cq_chat_id(cq)
    session = await bm.SESSIONS.get(callback_data.cid, expected_chat_id=chat_id)
    if session is None:
        await cq.answer(bm.i18n.t("rsa_session_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    await cq.answer()
    await msg.answer(bm.i18n.t("rsa_generating"))
    try:
        # extra-ключи брифа (cc_session и пр.) CopyBrief игнорирует; сбой → остаёмся на текущем наборе
        brief = bm.CopyBrief(**{k: v for k, v in (session.brief or {}).items() if v is not None})
        async with bm.ux.typing_action(msg):
            gen = await bm._generate_rsa(brief)
    except Exception as e:  # LLM/сеть/битый бриф
        await msg.answer(bm.i18n.t("err_text_gen", err=bm.ux.err_text(e)), reply_markup=bm.nav_kb())
        return
    if len(gen.headlines) < bm.RSA_MIN_HEADLINES or len(gen.descriptions) < bm.RSA_MIN_DESCRIPTIONS:
        await msg.answer(bm.i18n.t("search_gen_empty"), reply_markup=bm.nav_kb())
        return
    session = await bm.SESSIONS.replace_all(
        callback_data.cid,
        gen.headlines,
        gen.descriptions,
        expected_chat_id=chat_id,
        mark="pending",  # новый набор снова проходит поэлементную курацию
    )
    if session is None:
        await msg.answer(bm.i18n.t("rsa_session_stale"))
        return
    await bm._cc_rsa_present_curation(msg, session)


@bm.dp.message(bm.RsaList.awaiting_edited)
async def rsa_list_edited(m: bm.Message, state: bm.FSMContext) -> None:
    """§10 list-UX: менеджер прислал отредактированный СПИСОК. Парсим → валидируем количество и длину
    (КОД, кириллица=1); ошибка → точное сообщение с номерами строк, остаёмся в состоянии; успех →
    replace_all (всё approved) → завершение (confirm-гейт для create_rsa или черновик визарда)."""
    from adcopy.validate import LIMITS, find_duplicates
    from adcopy.validate import validate as _validate

    data = await state.get_data()
    cid = data.get("cid")
    session = await bm.SESSIONS.get(cid, expected_chat_id=m.chat.id) if cid else None
    if session is None:
        await state.clear()
        await m.answer(bm.i18n.t("rsa_session_stale"))
        return
    headlines, descriptions = bm.texts.parse_rsa_paste(m.text or "")
    en = bm.i18n.current_lang() == "en"
    errs: list[str] = []
    if not (bm.RSA_MIN_HEADLINES <= len(headlines) <= bm.RSA_MAX_HEADLINES):
        errs.append(
            f"Headlines: {len(headlines)}, need {bm.RSA_MIN_HEADLINES}–{bm.RSA_MAX_HEADLINES}"
            if en
            else f"Заголовков: {len(headlines)}, нужно {bm.RSA_MIN_HEADLINES}–{bm.RSA_MAX_HEADLINES}"
        )
    if not (bm.RSA_MIN_DESCRIPTIONS <= len(descriptions) <= bm.RSA_MAX_DESCRIPTIONS):
        errs.append(
            f"Descriptions: {len(descriptions)}, need {bm.RSA_MIN_DESCRIPTIONS}–{bm.RSA_MAX_DESCRIPTIONS}"
            if en
            else f"Описаний: {len(descriptions)}, нужно {bm.RSA_MIN_DESCRIPTIONS}–{bm.RSA_MAX_DESCRIPTIONS}"
        )
    for i, t in enumerate(headlines, 1):
        ok, n = _validate(t, "headline")
        if not ok:
            label = "Headline" if en else "Заголовок"
            errs.append(f"{label} #{i}: {n}/{LIMITS['headline']} — «{t[:40]}»")
    for i, t in enumerate(descriptions, 1):
        ok, n = _validate(t, "description")
        if not ok:
            label = "Description" if en else "Описание"
            errs.append(f"{label} #{i}: {n}/{LIMITS['description']} — «{t[:40]}»")
    # D5: дубли заголовков/описаний (casefold) — Google Ads отклонит RSA. Показываем ДРУЖЕЛЮБНО,
    # чтобы менеджер убрал повтор до создания (а не получил серверный DUPLICATE после ✅).
    for i, t in find_duplicates(headlines):
        errs.append(
            (f"Duplicate headline #{i}" if en else f"Дубль заголовка #{i}") + f": «{t[:40]}»"
        )
    for i, t in find_duplicates(descriptions):
        errs.append(
            (f"Duplicate description #{i}" if en else f"Дубль описания #{i}") + f": «{t[:40]}»"
        )
    if errs:  # остаёмся в RsaList.awaiting_edited — менеджер правит и присылает снова
        head = "⚠️ Check the list and resend:" if en else "⚠️ Проверь список и пришли снова:"
        await m.answer(head + "\n" + "\n".join(f"• {e}" for e in errs), reply_markup=bm.nav_kb())
        return
    session = await bm.SESSIONS.replace_all(
        cid, headlines, descriptions, expected_chat_id=m.chat.id
    )
    if session is None:
        await state.clear()
        await m.answer(bm.i18n.t("rsa_session_stale"))
        return
    await state.clear()
    await bm._rsa_present_final(m, m.chat.id, session, state)


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "finalize"))
async def rsa_finalize(cq: bm.CallbackQuery, callback_data: bm.RsaCB, state: bm.FSMContext) -> None:
    session = await bm.SESSIONS.get(callback_data.cid, expected_chat_id=bm._cq_chat_id(cq))
    if session is None:
        await cq.answer(bm.i18n.t("rsa_session_stale"), show_alert=True)
        return
    if not session.can_finalize():
        h, d = session.counts()
        await cq.answer(bm.i18n.t("rsa_below_min", h=h, d=d), show_alert=True)
        return
    await cq.answer()
    await state.clear()
    msg = bm._cq_msg(cq)
    if msg is not None:
        await bm._rsa_present_final(msg, bm._cq_chat_id(cq), session, state)


@bm.dp.callback_query(bm.RsaCB.filter(bm.F.action == "cancel"))
async def rsa_cancel(cq: bm.CallbackQuery, callback_data: bm.RsaCB, state: bm.FSMContext) -> None:
    await (
        state.clear()
    )  # §10 list-UX: снять RsaList.awaiting_edited, чтобы след. текст не парсился списком
    await cq.answer(bm.i18n.t("cb_cancelled"))
    await bm._safe_edit(cq, bm.i18n.t("rejected"))
