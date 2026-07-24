"""/campaigns: быстрые действия (пауза/возобновление), гео §3, аудитории, расширения

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.callback_query(bm.SlashMutCB.filter())
async def on_slash_mutate_pick(cq: bm.CallbackQuery, callback_data: bm.SlashMutCB) -> None:
    """D4: выбор кампании в пикере /pause · /resume (без аргумента) → минт proposal паузы/
    возобновления за confirm-гейтом (тот же хвост, что ввод имени командой)."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._SLASH_MUT_CACHE.get(chat_id)
    # N1.4-ревью: gen сверяет клавиатуру со СВОИМ снапшотом списка — кэш мог перезаписать второй
    # писатель (fuzzy-подсказка), и idx старой кнопки указывал бы в ДРУГОЙ список (подмена интента).
    if (
        not camps
        or not (0 <= callback_data.idx < len(camps))
        or int(getattr(callback_data, "gen", 0)) != bm._SLASH_MUT_GEN.get(chat_id, 0)
    ):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    await cq.answer()
    await bm._slash_mutate_present(
        msg, chat_id, callback_data.op, camps[callback_data.idx].get("name", "")
    )


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "geo"))
async def camp_geo(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    """Меню кампании → «📍 Гео-таргетинг»: ПОКАЗЫВАЕМ текущее гео/языки (§3 «чтение … ГЕО» —
    2E) и выбор способа изменения (локации/радиус). READ-ONLY: ничего не меняем — адрес/локации
    вводятся следующим шагом, черновик собирается после (confirm-гейт). Сбой чтения текущего гео
    НЕ блокирует изменение (fail-safe: меню показывается с пометкой)."""
    camps = bm._camp_rows(bm._cq_chat_id(cq), callback_data.gen)
    if not bm._valid_idx(camps, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    await cq.answer()
    name = camps[callback_data.idx]["name"]
    camp_id = camps[callback_data.idx].get("id")
    current = ""
    if camp_id:
        try:
            from ads.client import build_client_async
            from ads.read import read_campaign_targeting
            from core.resilience import run_ads_read_call

            # §8: читаем аккаунт-якорь меню (тот же, что и мутируем) — не хардкод Draft.
            acct = bm._camp_account(bm._cq_chat_id(cq))
            t = await run_ads_read_call(
                read_campaign_targeting,
                await build_client_async(acct),
                acct,
                camp_id,
                label="campaign_targeting",
            )
            current = bm.texts.fmt_campaign_targeting(t)
        except Exception:  # noqa: BLE001 — показ текущего гео best-effort, меню не блокируем
            current = bm.i18n.t("geo_read_failed")
    text = bm.i18n.t("geo_mode_pick", camp=bm.texts.esc(name))
    if current:
        text = current + "\n\n" + text
    await bm._safe_edit(
        cq,
        text,
        reply_markup=bm.geo_mode_kb(callback_data.idx, gen=callback_data.gen),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.GeoCB.filter(bm.F.action.in_(["loc", "prox"])))
async def on_geo_mode(cq: bm.CallbackQuery, callback_data: bm.GeoCB, state: bm.FSMContext) -> None:
    """Способ выбран → ждём текст (локации ИЛИ «город, радиус»). Кампанию резолвим СЕЙЧАС и кладём
    в state-data (имя стабильно, даже если _CAMP_CACHE сменится до ввода). Сбор ввода — до гейта."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._CAMP_CACHE.get(chat_id)
    if not bm._valid_idx(camps, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    name = camps[callback_data.idx]["name"]
    await state.clear()
    if callback_data.action == "loc":
        await state.set_state(bm.Geo.awaiting_locations)
        prompt = "geo_pick_locations"
    else:
        await state.set_state(bm.Geo.awaiting_proximity)
        prompt = "geo_pick_proximity"
    # geo_idx нужен retry-хендлерам, чтобы собрать Back-цель (меню кампании) даже после
    # невалидного ввода; имя кампании уже резолвлено в geo_campaign.
    await state.update_data(
        geo_campaign=name, geo_idx=callback_data.idx, geo_account=bm._camp_account(chat_id)
    )
    await cq.answer()
    # Back → меню кампании (та же цель, что у «‹ Назад» в geo_mode_kb): переиспользуем camp_menu.
    await msg.answer(
        bm.i18n.t(prompt),
        reply_markup=bm.nav_kb(
            bm.CampCB(action="menu", idx=callback_data.idx, gen=bm._camp_gen(chat_id))
        ),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.Geo.awaiting_locations)
async def geo_locations(m: bm.Message, state: bm.FSMContext) -> None:
    """Локации введены → черновик set_geo_location (заменяет гео кампании целиком, §3). Резолв
    названий → geoTargetConstant и remove-before-create — в SDK-слое после ✅; здесь только черновик."""
    locations = bm._parse_geo_locations(m.text or "")
    if not locations:
        await m.answer(
            bm.i18n.t("geo_empty_locations"),
            reply_markup=await bm._geo_nav_kb(state, m.chat.id),
            parse_mode=bm.ParseMode.HTML,
        )
        return  # остаёмся в состоянии — пользователь пришлёт локации ещё раз
    data = await state.get_data()
    campaign = (data.get("geo_campaign") or "").strip()
    if not campaign:
        await state.clear()
        await m.answer(bm.i18n.t("geo_stale"))
        return
    acct = (data.get("geo_account") or bm.DRAFT_ACCOUNT_ID).strip()
    await state.clear()
    try:
        cid, operation, params, summary = bm._build_proposal(
            "set_geo_location", campaign=campaign, locations=locations
        )
    except Exception as e:  # noqa: BLE001 — валидация схемы (>20 локаций / >80 симв.) → понятный ответ
        await m.answer(await bm._friendly_error(e, "camp:geo_location"))
        return
    await bm._present_proposal(
        m,
        chat_id=m.chat.id,
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=acct,
    )


@bm.dp.message(bm.Geo.awaiting_proximity)
async def geo_proximity(m: bm.Message, state: bm.FSMContext) -> None:
    """«город, радиус_км» введено → черновик set_geo_proximity (радиус-таргетинг, §3). Google геокодит
    адрес сам; границы радиуса (0, 2000] валидирует схема. Исполнение — только после ✅."""
    parsed = bm._parse_geo_proximity(m.text or "")
    if parsed is None:
        await m.answer(
            bm.i18n.t("geo_bad_proximity"),
            reply_markup=await bm._geo_nav_kb(state, m.chat.id),
            parse_mode=bm.ParseMode.HTML,
        )
        return  # остаёмся в состоянии — пользователь пришлёт «город, радиус» ещё раз
    city, radius = parsed
    data = await state.get_data()
    campaign = (data.get("geo_campaign") or "").strip()
    if not campaign:
        await state.clear()
        await m.answer(bm.i18n.t("geo_stale"))
        return
    acct = (data.get("geo_account") or bm.DRAFT_ACCOUNT_ID).strip()
    await state.clear()
    try:
        cid, operation, params, summary = bm._build_proposal(
            "set_geo_proximity", campaign=campaign, city_name=city, radius_km=radius
        )
    except Exception as e:  # noqa: BLE001 — валидация схемы (радиус вне (0,2000] / длина) → понятный ответ
        await m.answer(await bm._friendly_error(e, "camp:geo_proximity"))
        return
    await bm._present_proposal(
        m,
        chat_id=m.chat.id,
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=acct,
    )


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "ext"))
async def camp_ext(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    """Меню кампании → «🧩 Расширения»: показать выбор типа. READ-ONLY (ввод/черновик — дальше)."""
    camps = bm._camp_rows(bm._cq_chat_id(cq), callback_data.gen)
    if not bm._valid_idx(camps, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    await cq.answer()
    name = camps[callback_data.idx]["name"]
    await bm._safe_edit(
        cq,
        bm.i18n.t("ext_menu_pick", camp=bm.texts.esc(name)),
        reply_markup=bm.ext_menu_kb(callback_data.idx, gen=callback_data.gen),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.ExtCB.filter(bm.F.action.in_(["sitelink", "callout", "snippet", "image"])))
async def on_ext_type(cq: bm.CallbackQuery, callback_data: bm.ExtCB, state: bm.FSMContext) -> None:
    """Тип расширения выбран → ждём ввод (для snippet сначала выбор header кнопками; для image —
    фото, его перехватывает on_photo по состоянию ExtWizard.awaiting_image)."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._CAMP_CACHE.get(chat_id)
    if not bm._valid_idx(camps, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    name = camps[callback_data.idx]["name"]
    await cq.answer()
    if callback_data.action == "snippet":
        # header строго из канонического списка → выбираем кнопкой (иначе HEADER_NOT_FOUND)
        await bm._safe_edit(
            cq,
            bm.i18n.t("ext_ask_snippet_header", camp=bm.texts.esc(name)),
            reply_markup=bm.ext_snippet_header_kb(callback_data.idx),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    await state.clear()
    await state.update_data(
        ext_campaign=name, ext_idx=callback_data.idx, ext_account=bm._camp_account(chat_id)
    )
    if callback_data.action == "sitelink":
        await state.set_state(bm.ExtWizard.awaiting_sitelinks)
        prompt = "ext_ask_sitelinks"
    elif callback_data.action == "image":
        await state.set_state(bm.ExtWizard.awaiting_image)
        prompt = "ext_ask_image"
    else:
        await state.set_state(bm.ExtWizard.awaiting_callouts)
        prompt = "ext_ask_callouts"
    await msg.answer(
        bm.i18n.t(prompt),
        reply_markup=bm.nav_kb(
            bm.CampCB(action="ext", idx=callback_data.idx, gen=bm._camp_gen(chat_id))
        ),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.ExtCB.filter(bm.F.action == "snip_h"))
async def on_ext_snippet_header(
    cq: bm.CallbackQuery, callback_data: bm.ExtCB, state: bm.FSMContext
) -> None:
    """Header структурного описания выбран → ждём значения текстом."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._CAMP_CACHE.get(chat_id)
    if not bm._valid_idx(camps, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    name = camps[callback_data.idx]["name"]
    await state.clear()
    await state.set_state(bm.ExtWizard.awaiting_snippet_values)
    await state.update_data(
        ext_campaign=name,
        ext_idx=callback_data.idx,
        ext_header=callback_data.sub,
        ext_account=bm._camp_account(chat_id),
    )
    await cq.answer()
    await msg.answer(
        bm.i18n.t("ext_ask_snippet_values", header=bm.texts.esc(callback_data.sub)),
        reply_markup=bm.nav_kb(
            bm.CampCB(action="ext", idx=callback_data.idx, gen=bm._camp_gen(chat_id))
        ),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.ExtWizard.awaiting_sitelinks)
async def ext_sitelinks(m: bm.Message, state: bm.FSMContext) -> None:
    sitelinks = bm._parse_sitelinks(m.text or "")
    if not sitelinks:
        await m.answer(
            bm.i18n.t("ext_bad_sitelinks"),
            reply_markup=await bm._ext_nav_kb(state, m.chat.id),
            parse_mode=bm.ParseMode.HTML,
        )
        return  # остаёмся в состоянии (retry с навигацией)
    data = await state.get_data()
    campaign = (data.get("ext_campaign") or "").strip()
    if not campaign:
        await state.clear()
        await m.answer(bm.i18n.t("ext_stale"))
        return
    acct = (data.get("ext_account") or bm.DRAFT_ACCOUNT_ID).strip()
    await state.clear()
    try:
        cid, operation, params, summary = bm._build_proposal(
            "add_sitelinks", campaign=campaign, sitelinks=sitelinks
        )
    except Exception as e:  # noqa: BLE001 — валидация схемы (длина/URL/desc) → понятный ответ
        await m.answer(await bm._friendly_error(e, "camp:sitelinks"))
        return
    await bm._present_proposal(
        m,
        chat_id=m.chat.id,
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=acct,
    )


@bm.dp.message(bm.ExtWizard.awaiting_callouts)
async def ext_callouts(m: bm.Message, state: bm.FSMContext) -> None:
    callouts = bm._parse_csv_lines(m.text or "")
    if not callouts:
        await m.answer(
            bm.i18n.t("ext_bad_callouts"),
            reply_markup=await bm._ext_nav_kb(state, m.chat.id),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    data = await state.get_data()
    campaign = (data.get("ext_campaign") or "").strip()
    if not campaign:
        await state.clear()
        await m.answer(bm.i18n.t("ext_stale"))
        return
    acct = (data.get("ext_account") or bm.DRAFT_ACCOUNT_ID).strip()
    await state.clear()
    try:
        cid, operation, params, summary = bm._build_proposal(
            "add_callouts", campaign=campaign, callouts=callouts
        )
    except Exception as e:  # noqa: BLE001
        await m.answer(await bm._friendly_error(e, "camp:callouts"))
        return
    await bm._present_proposal(
        m,
        chat_id=m.chat.id,
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=acct,
    )


@bm.dp.message(bm.ExtWizard.awaiting_snippet_values)
async def ext_snippet_values(m: bm.Message, state: bm.FSMContext) -> None:
    values = bm._parse_csv_lines(m.text or "")
    data = await state.get_data()
    header = (data.get("ext_header") or "").strip()
    campaign = (data.get("ext_campaign") or "").strip()
    if len(values) < 3:
        await m.answer(
            bm.i18n.t("ext_bad_snippet_values"),
            reply_markup=await bm._ext_nav_kb(state, m.chat.id),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    if not campaign or not header:
        await state.clear()
        await m.answer(bm.i18n.t("ext_stale"))
        return
    acct = (data.get("ext_account") or bm.DRAFT_ACCOUNT_ID).strip()
    await state.clear()
    try:
        cid, operation, params, summary = bm._build_proposal(
            "add_structured_snippets", campaign=campaign, header=header, values=values
        )
    except Exception as e:  # noqa: BLE001
        await m.answer(await bm._friendly_error(e, "camp:snippets"))
        return
    await bm._present_proposal(
        m,
        chat_id=m.chat.id,
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=acct,
    )


@bm.dp.callback_query(bm.ExtCB.filter(bm.F.action == "show"))
async def on_ext_show(cq: bm.CallbackQuery, callback_data: bm.ExtCB) -> None:
    """Показать текущие расширения кампании с кнопками удаления (open-чтение, ничего не меняем)."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._CAMP_CACHE.get(chat_id)
    if not bm._valid_idx(camps, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    await cq.answer()
    campaign_id = camps[callback_data.idx].get("id")
    from ads.client import build_client_async
    from ads.read import list_campaign_assets

    acct = bm._camp_account(chat_id)  # §8: аккаунт-якорь меню, не хардкод Draft
    try:
        rows = await bm.run_ads_read_call(
            list_campaign_assets,
            await build_client_async(acct),
            acct,
            campaign_id,
            label="list_campaign_assets",
        )
    except Exception as e:  # сеть/доступ/SDK
        await bm._safe_edit(cq, bm.i18n.t("ext_show_error", err=bm.ux.err_text(e)))
        return
    if not rows:
        await bm._safe_edit(
            cq, bm.i18n.t("ext_empty"), reply_markup=bm.ext_menu_kb(callback_data.idx)
        )
        return
    bm._EXT_CACHE[chat_id] = rows
    await bm._safe_edit(
        cq,
        bm.i18n.t("ext_list_title", n=len(rows)),
        reply_markup=bm.ext_assets_list_kb(rows, callback_data.idx),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.ExtCB.filter(bm.F.action == "remove"))
async def on_ext_remove(cq: bm.CallbackQuery, callback_data: bm.ExtCB) -> None:
    """Кнопка 🗑 у расширения → черновик remove_asset_link (открепить связь, confirm-гейт)."""
    chat_id = bm._cq_chat_id(cq)
    rows = bm._EXT_CACHE.get(chat_id)
    if not bm._valid_idx(rows, callback_data.idx):
        await cq.answer(bm.i18n.t("ext_list_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    rn = rows[callback_data.idx].link_resource_name
    try:
        cid, operation, params, summary = bm._build_proposal(
            "remove_asset_link", link_resource_names=[rn]
        )
    except Exception as e:  # noqa: BLE001
        await cq.answer(
            await bm._friendly_error(e, "camp:remove_asset", short=True), show_alert=True
        )
        return
    await cq.answer()
    await bm._present_proposal(
        msg,
        chat_id=chat_id,
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=bm._camp_account(chat_id),
    )


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "menu"))
async def camp_menu(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    camps = bm._camp_rows(bm._cq_chat_id(cq), callback_data.gen)
    if not bm._valid_idx(camps, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    await cq.answer()
    c = camps[callback_data.idx]
    await bm._safe_edit(
        cq,
        bm.texts.fmt_campaign_header(c),
        reply_markup=bm.campaign_actions_kb(
            callback_data.idx, c.get("status", ""), gen=callback_data.gen
        ),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "back"))
async def camp_back(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    chat_id = bm._cq_chat_id(cq)
    camps = bm._camp_rows(chat_id, callback_data.gen)
    if not camps:
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    await cq.answer()
    await bm._safe_edit(
        cq,
        bm.texts.campaigns_title(bm._camp_account(chat_id)),
        reply_markup=bm.campaigns_kb(camps, gen=callback_data.gen),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "pause"))
async def camp_pause(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    await bm._camp_mutate(cq, callback_data.idx, "pause_campaign", gen=callback_data.gen)


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "resume"))
async def camp_resume(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    await bm._camp_mutate(cq, callback_data.idx, "resume_campaign", gen=callback_data.gen)


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "delete"))
async def camp_delete(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    """🗑 Удалить кампанию → черновик remove_campaign за confirm-гейтом. Необратимо, поэтому
    _present_proposal покажет ДВОЙНОЕ подтверждение (P1-6). Замок аккаунта держит ensure_allowed."""
    await bm._camp_mutate(cq, callback_data.idx, "remove_campaign", gen=callback_data.gen)


# ── §19.3 Сети: тумблер поисковых партнёров существующей кампании ────────────────
@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "network"))
async def camp_network(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    """Подменю сетей кампании. READ-ONLY: только выбор; изменение — черновиком
    set_campaign_network за confirm-гейтом («было→станет» подтянет read_before)."""
    camps = bm._camp_rows(bm._cq_chat_id(cq), callback_data.gen)
    if not bm._valid_idx(camps, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    await cq.answer()
    await bm._safe_edit(
        cq,
        bm.i18n.t("camp_network_title", camp=camps[callback_data.idx]["name"]),
        reply_markup=bm.campaign_network_kb(callback_data.idx, gen=callback_data.gen),
    )


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "net_off"))
async def camp_net_off(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    await bm._camp_mutate(
        cq, callback_data.idx, "set_campaign_network", gen=callback_data.gen, search_partners=False
    )


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "net_on"))
async def camp_net_on(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    await bm._camp_mutate(
        cq, callback_data.idx, "set_campaign_network", gen=callback_data.gen, search_partners=True
    )


# G12: КМС на кампании — «выключить» чинит поисковую кампанию, где баннеры ели поисковый бюджет.
@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "kms_off"))
async def camp_kms_off(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    await bm._camp_mutate(
        cq,
        callback_data.idx,
        "set_campaign_display_network",
        gen=callback_data.gen,
        display_network=False,
    )


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "kms_on"))
async def camp_kms_on(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    await bm._camp_mutate(
        cq,
        callback_data.idx,
        "set_campaign_display_network",
        gen=callback_data.gen,
        display_network=True,
    )


# G11: тип гео-таргетинга (кнопки живут в подменю «📍 Гео-таргетинг» — geo_mode_kb).
@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "gt_p"))
async def camp_geo_type_presence(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    await bm._camp_mutate(
        cq,
        callback_data.idx,
        "set_campaign_geo_target_type",
        gen=callback_data.gen,
        geo_target_type="PRESENCE",
    )


@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "gt_pi"))
async def camp_geo_type_presence_or_interest(
    cq: bm.CallbackQuery, callback_data: bm.CampCB
) -> None:
    await bm._camp_mutate(
        cq,
        callback_data.idx,
        "set_campaign_geo_target_type",
        gen=callback_data.gen,
        geo_target_type="PRESENCE_OR_INTEREST",
    )


# ── §3 Аудитории: меню кампании → список аудиторий → черновик attach_audience ───────
@bm.dp.callback_query(bm.CampCB.filter(bm.F.action == "audience"))
async def camp_audience(cq: bm.CallbackQuery, callback_data: bm.CampCB) -> None:
    """Показать доступные аудитории (user_list) для прикрепления к выбранной кампании.
    READ-ONLY: только список; прикрепление — отдельным черновиком после выбора (confirm-гейт)."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._camp_rows(chat_id, callback_data.gen)
    if not bm._valid_idx(camps, callback_data.idx):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    try:
        from ads.client import build_client_async
        from ads.read import list_attached_audiences, list_audiences

        acct = bm._camp_account(chat_id)  # §8: аудитории аккаунта-якоря меню, не хардкод Draft
        client = await build_client_async(acct)
        async with bm.ux.typing_action(cq.message):
            auds = await bm.run_ads_read_call(list_audiences, client, acct, label="list_audiences")
            attached: list = []
            camp_id = camps[callback_data.idx].get("id")
            if camp_id:
                try:  # C7: прикреплённые — best-effort (сбой не блокирует прикрепление новых)
                    attached = await bm.run_ads_read_call(
                        list_attached_audiences, client, acct, camp_id, label="attached_audiences"
                    )
                except Exception:  # noqa: BLE001
                    attached = []
    except Exception as e:  # сеть/доступ/SDK
        await cq.answer(await bm._friendly_error(e, "camp:audiences", short=True), show_alert=True)
        return
    if not auds and not attached:
        await cq.answer(bm.i18n.t("no_audiences"), show_alert=True)
        return
    bm._AUD_CACHE[chat_id] = auds
    bm._AUD_DET_CACHE[chat_id] = attached  # C7: резолв idx→resource_name для 🗑
    await cq.answer()
    await bm._safe_edit(
        cq,
        bm.texts.audiences_title(camps[callback_data.idx]["name"]),
        reply_markup=bm.audiences_kb(
            auds, callback_data.idx, attached=attached, gen=callback_data.gen
        ),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.AudienceCB.filter(bm.F.action == "pick"))
async def on_audience_pick(cq: bm.CallbackQuery, callback_data: bm.AudienceCB) -> None:
    """Выбрана аудитория → СОЗДАЁТ черновик attach_audience (как кнопки пауза/возобновление):
    исполнение только после ✅ через confirm-гейт. Кампания+аудитория резолвятся из кэшей по idx."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._CAMP_CACHE.get(chat_id)
    auds = bm._AUD_CACHE.get(chat_id)
    if not bm._valid_idx(camps, callback_data.camp_idx) or not bm._valid_idx(
        auds, callback_data.idx
    ):
        await cq.answer(bm.i18n.t("aud_list_stale"), show_alert=True)
        return
    name = camps[callback_data.camp_idx]["name"]
    aud = auds[callback_data.idx]
    try:
        cid, op, params, summary = bm._build_proposal(
            "attach_audience", campaign=name, audience_resource_names=[aud.resource_name]
        )
    except Exception as e:  # валидация схемы
        await cq.answer(
            await bm._friendly_error(e, "camp:attach_audience", short=True), show_alert=True
        )
        return
    params["_audience_names"] = [aud.name]  # инертно для исполнения; для дружелюбной сводки
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    await bm._present_proposal(
        msg,
        chat_id=chat_id,
        operation=op,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=bm._camp_account(chat_id),
    )


@bm.dp.callback_query(bm.AudienceCB.filter(bm.F.action == "det"))
async def on_audience_detach(cq: bm.CallbackQuery, callback_data: bm.AudienceCB) -> None:
    """C7: 🗑 у прикреплённой аудитории → черновик detach_audience (зеркало on_audience_pick;
    исполнение только после ✅ через confirm-гейт). Раньше detach был недостижим из UI вовсе."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._CAMP_CACHE.get(chat_id)
    attached = bm._AUD_DET_CACHE.get(chat_id)
    if not bm._valid_idx(camps, callback_data.camp_idx) or not bm._valid_idx(
        attached, callback_data.idx
    ):
        await cq.answer(bm.i18n.t("aud_list_stale"), show_alert=True)
        return
    name = camps[callback_data.camp_idx]["name"]
    aud = attached[callback_data.idx]
    try:
        cid, op, params, summary = bm._build_proposal(
            "detach_audience", campaign=name, audience_resource_names=[aud.resource_name]
        )
    except Exception as e:  # валидация схемы
        await cq.answer(
            await bm._friendly_error(e, "camp:detach_audience", short=True), show_alert=True
        )
        return
    params["_audience_names"] = [aud.name]  # инертно для исполнения; для дружелюбной сводки
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    await bm._present_proposal(
        msg,
        chat_id=chat_id,
        operation=op,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=bm._camp_account(chat_id),
    )
