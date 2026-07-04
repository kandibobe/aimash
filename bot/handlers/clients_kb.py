"""§20 «Информация про клиентов» (/clients, cli_*): профиль, краулинг, memory-домен

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.message(bm.Command("clients"))
async def clients_cmd(m: bm.Message, state: bm.FSMContext) -> None:
    await state.clear()
    await bm._cli_present_accounts(m)


@bm.dp.message(bm.F.text.in_(bm.BTN_CLIENTS_ALL))
async def btn_clients(m: bm.Message, state: bm.FSMContext) -> None:
    await state.clear()
    await bm._cli_present_accounts(m)


@bm.dp.message(bm.Command("client"))
async def client_cmd(m: bm.Message, command: bm.CommandObject, state: bm.FSMContext) -> None:
    """/client <customer_id> — карточка клиента по id (§20.8)."""
    await state.clear()
    arg = (command.args or "").strip()
    cid = bm.normalize_customer_id(arg)
    if not cid:
        await m.answer(bm.i18n.t("cli_usage_hint"), parse_mode=bm.ParseMode.HTML)
        return
    await state.update_data(cli_customer_id=cid)
    await bm._cli_show_card(m, m.chat.id, cid)


@bm.dp.callback_query(bm.ClientCB.filter(bm.F.action == "acct"))
async def cli_account_cb(
    cq: bm.CallbackQuery, callback_data: bm.ClientCB, state: bm.FSMContext
) -> None:
    """§20.2: аккаунт выбран → фиксируем в FSM и показываем карточку клиента."""
    chat_id = bm._cq_chat_id(cq)
    rows = bm._CLI_ACCT_CACHE.get(chat_id)
    if not bm._valid_idx(rows, callback_data.idx):
        await cq.answer(bm.i18n.t("cli_card_stale"), show_alert=True)
        return
    customer_id = getattr(rows[callback_data.idx], "id", bm.DRAFT_ACCOUNT_ID)
    # §20: смена аккаунта из списка сбрасывает режим накопления и буфер текста — иначе набранный для
    # прежнего клиента текст (со stale-кнопки старого списка) уехал бы в профиль другого аккаунта.
    bm._CLI_TEXT_BUF.pop(chat_id, None)
    bm._cli_cancel_idle(chat_id)  # B13: гасим таймер авто-сохранения прежнего аккаунта
    await state.clear()
    await state.update_data(cli_customer_id=customer_id)
    await cq.answer()
    await bm._cli_show_card(cq.message, chat_id, customer_id)


@bm.dp.callback_query(bm.ClientCB.filter(bm.F.action == "page"))
async def cli_account_page_cb(cq: bm.CallbackQuery, callback_data: bm.ClientCB) -> None:
    """B7: перелистывание постраничного списка аккаунтов раздела «Клиенты» — перерисовываем разметку."""
    chat_id = bm._cq_chat_id(cq)
    rows = bm._CLI_ACCT_CACHE.get(chat_id)
    if not rows:
        await cq.answer(bm.i18n.t("cli_card_stale"), show_alert=True)
        return
    try:
        page = int(callback_data.sub or "0")
    except ValueError:
        page = 0
    await cq.answer()
    with_profile = bm._CLI_WITH_PROFILE.get(chat_id, set())
    await bm._safe_edit_markup(cq, bm.clients_accounts_kb(rows, with_profile, page=page))


@bm.dp.callback_query(bm.ClientCB.filter(bm.F.action == "back"))
async def cli_back_cb(cq: bm.CallbackQuery, state: bm.FSMContext) -> None:
    await state.clear()
    await cq.answer()
    await bm._cli_present_accounts(cq.message)


@bm.dp.callback_query(bm.ClientCB.filter(bm.F.action == "card"))
async def cli_card_cb(
    cq: bm.CallbackQuery, callback_data: bm.ClientCB, state: bm.FSMContext
) -> None:
    """§20.2 «📋 Карточка клиента»: пере-показать карточку одним тапом (после add/update/краула).
    customer_id — из sub (stateless: работает после фонового краула/рестарта), фолбэк — FSM.
    Доступ re-check'ается fail-closed внутри _cli_show_card. Read-only."""
    chat_id = bm._cq_chat_id(cq)
    customer_id = bm.normalize_customer_id(callback_data.sub) or await bm._cli_selected_account(
        state
    )
    msg = bm._cq_msg(cq)
    if not customer_id or msg is None:
        await cq.answer(bm.i18n.t("cli_card_stale"), show_alert=True)
        return
    # контекст в FSM — чтобы «Обновить/Перекраулить» с показанной карточки работали сразу
    await state.update_data(cli_customer_id=customer_id)
    await cq.answer()
    await bm._cli_show_card(msg, chat_id, customer_id)


@bm.dp.callback_query(bm.ClientCB.filter(bm.F.action.in_({"add", "update"})))
async def cli_add_update_cb(
    cq: bm.CallbackQuery, callback_data: bm.ClientCB, state: bm.FSMContext
) -> None:
    """§20.3: перейти в режим приёма текста профиля (accumulate до «💾 Сохранить»)."""
    chat_id = bm._cq_chat_id(cq)
    # C4: аккаунт берём ИЗ callback (restart-safe), иначе из FSM. Раньше при пустом FSM (рестарт/
    # idle-автосейв/suspend меню) был ранний return БЕЗ set_state → следующий текст с URL уходил в
    # агент-задачу вместо накопления профиля (баг из живого теста). Теперь кнопка несёт customer_id.
    customer_id = bm.normalize_customer_id(callback_data.sub) or await bm._cli_selected_account(
        state
    )
    if not customer_id:
        await cq.answer(bm.i18n.t("cli_card_stale"), show_alert=True)
        return
    if not await bm._cli_check_access(chat_id, customer_id):
        await cq.answer(bm.i18n.t("cli_access_denied"), show_alert=True)
        return
    bm._CLI_TEXT_BUF[chat_id] = []
    bm._cli_cancel_idle(chat_id)  # B13: свежий приём текста — гасим прежний таймер авто-сохранения
    await state.update_data(cli_customer_id=customer_id, cli_mode=callback_data.action)
    await state.set_state(bm.ClientInfoWizard.awaiting_text)
    await cq.answer()
    await cq.message.answer(
        bm.i18n.t("cli_ask_text"),
        reply_markup=bm.client_input_kb(),
        parse_mode=bm.ParseMode.HTML,  # копируемый <code>-пример
    )


@bm.dp.message(bm.ClientInfoWizard.awaiting_text)
async def cli_accumulate_text(m: bm.Message, state: bm.FSMContext) -> None:
    """§20.3: копим присланный текст (несколько сообщений) до «💾 Сохранить»/таймаута."""
    chat_id = m.chat.id
    text = (m.text or m.caption or "").strip()
    if not text:
        await m.answer(bm.i18n.t("cli_empty_input"), reply_markup=bm.client_input_kb())
        return
    buf = bm._CLI_TEXT_BUF.setdefault(chat_id, [])
    buf.append(text)
    chars = sum(len(x) for x in buf)
    # §20.3/B13: каждое сообщение сбрасывает и заново взводит таймер авто-сохранения (по таймауту
    # покажем «было→станет» + confirm, как ручное «Сохранить»). customer_id — из FSM.
    customer_id = await bm._cli_selected_account(state)
    if customer_id:
        bm._cli_arm_idle(m.bot, chat_id, customer_id, state)  # 3G: автосейв закроет накопление
    await m.answer(
        bm.i18n.t("cli_accumulating", n=len(buf), chars=chars), reply_markup=bm.client_input_kb()
    )


@bm.dp.callback_query(bm.ClientCB.filter(bm.F.action == "save"))
async def cli_save_cb(cq: bm.CallbackQuery, state: bm.FSMContext) -> None:
    """§20.3/20.5: извлечь профиль из накопленного текста → показать «было→станет» + confirm."""
    chat_id = bm._cq_chat_id(cq)
    customer_id = await bm._cli_selected_account(state)
    buf = bm._CLI_TEXT_BUF.get(chat_id) or []
    if not customer_id:
        await cq.answer(bm.i18n.t("cli_card_stale"), show_alert=True)
        return
    if not buf:
        await cq.answer(bm.i18n.t("cli_nothing_to_save"), show_alert=True)
        return
    bm._cli_cancel_idle(chat_id)  # ручное сохранение — гасим таймер авто-сохранения (B13)
    await cq.answer()
    # 3G: тот же замок, что у idle-автосейва — буфер потребляется ровно один раз (при гонке
    # «таймер уже стрельнул» вторая сторона увидит пустой буфер и выйдет тихо).
    async with bm._cli_lock(chat_id):
        buf = bm._CLI_TEXT_BUF.pop(chat_id, None) or buf
        if not buf:
            await cq.message.answer(bm.i18n.t("cli_nothing_to_save"))
            return
        await cq.message.answer(bm.i18n.t("cli_extracting"))
        # выходим из режима накопления (черновик показан; исполнение — по ✅). §20.3: сайт в тексте →
        # «🕷 Сохранить и краулить» (внутри _cli_extract_and_propose).
        await state.clear()
        if not await bm._cli_extract_and_propose(cq.message.bot, chat_id, customer_id, buf):
            await cq.message.answer(
                bm.i18n.t("cli_extract_empty"), reply_markup=bm.client_input_kb()
            )


@bm.dp.callback_query(bm.ClientCB.filter(bm.F.action == "save_crawl"))
async def cli_save_crawl_cb(
    cq: bm.CallbackQuery, callback_data: bm.ClientCB, state: bm.FSMContext
) -> None:
    """§20.3: «🕷 Сохранить и краулить» — сначала сохранить текстовый профиль (тот же confirm-гейт,
    что и «✅»), ЗАТЕМ запустить краул сайта. Текст не теряется: краул мёржит поверх уже сохранённого
    профиля (существующий → profile_update-черновик по готовности), без гонки auto-save."""
    cid = callback_data.sub
    snap = await bm.STORE.get_confirmed(cid)
    if snap is None or snap.operation not in bm.MEMORY_OPERATIONS:
        await cq.answer(bm.i18n.t("stale"), show_alert=True)
        return
    customer_id = snap.customer_id
    website = (snap.params.get("patch") or {}).get("website")
    # B4: краул запускаем ТОЛЬКО если текстовый профиль реально сохранён ЭТИМ confirm. Иначе (повтор
    # stale-кнопки / сбой execute) краул нашёл бы before=None и авто-сохранил результат В ОБХОД гейта.
    if not await bm._do_confirm(cq, cid):
        return
    if not website:
        return
    url = (
        website
        if str(website).startswith(("http://", "https://"))
        else "https://" + str(website).lstrip("/")
    )
    from urllib.parse import urlparse

    domain = bm.texts.esc(urlparse(url).netloc or url)
    if not bm._spawn_crawl(cq.message.bot, bm._cq_chat_id(cq), customer_id, url):
        await cq.message.answer(bm.i18n.t("cli_crawl_already", domain=domain))
        return
    await cq.message.answer(bm.i18n.t("cli_crawl_started", domain=domain))


@bm.dp.callback_query(bm.ClientCB.filter(bm.F.action == "clear"))
async def cli_clear_cb(cq: bm.CallbackQuery, state: bm.FSMContext) -> None:
    """§20 «Очистить профиль»: изменяющее действие над памятью → confirm-гейт обязателен."""
    chat_id = bm._cq_chat_id(cq)
    customer_id = await bm._cli_selected_account(state)
    if not customer_id:
        await cq.answer(bm.i18n.t("cli_card_stale"), show_alert=True)
        return
    if not await bm._cli_check_access(chat_id, customer_id):
        await cq.answer(bm.i18n.t("cli_access_denied"), show_alert=True)
        return
    if await bm.CLIENTS.get_by_account(customer_id) is None:
        await cq.answer(bm.i18n.t("cli_no_profile_to_clear"), show_alert=True)
        return
    await cq.answer()
    summary = bm.texts.fmt_client_diff(None, {}, customer_id, operation="profile_clear")
    await bm._present_memory_proposal(
        cq.message.bot,
        chat_id=chat_id,
        operation="profile_clear",
        customer_id=customer_id,
        params={"customer_id": customer_id},
        summary=summary,
    )


@bm.dp.callback_query(bm.ClientCB.filter(bm.F.action == "autofill"))
async def cli_autofill_cb(
    cq: bm.CallbackQuery, callback_data: bm.ClientCB, state: bm.FSMContext
) -> None:
    """§20 (P1-11): «🔎 Подтянуть из аккаунта» — собрать ФАКТЫ из Google Ads (валюта/таймзона/гео/
    языки/домен, БЕЗ LLM, никаких выдумок) → показать «было→станет» через ТОТ ЖЕ confirm-гейт памяти
    (profile_save/update). Ничего не сохраняется без ✅. Замок мутаций Google Ads не затрагивается."""
    chat_id = bm._cq_chat_id(cq)
    customer_id = bm.normalize_customer_id(callback_data.sub) or await bm._cli_selected_account(
        state
    )
    if not customer_id:
        await cq.answer(bm.i18n.t("cli_card_stale"), show_alert=True)
        return
    if not await bm._cli_check_access(chat_id, customer_id):
        await cq.answer(bm.i18n.t("cli_access_denied"), show_alert=True)
        return
    await cq.answer()
    await cq.message.answer(bm.i18n.t("cli_autofill_reading"))
    # brand-подсказка (имя аккаунта) из кэша пикера — на случай, если meta обхода MCC пуста.
    brand_hint = ""
    for r in bm._CLI_ACCT_CACHE.get(chat_id) or []:
        if bm.normalize_customer_id(getattr(r, "id", "")) == customer_id:
            brand_hint = getattr(r, "name", "") or ""
            break
    from clients.account_facts import fetch_account_facts

    try:
        async with bm.ux.typing_action(cq.message):
            patch = await fetch_account_facts(customer_id, brand_hint=brand_hint)
    except Exception as e:  # noqa: BLE001 — сеть/доступ/SDK
        await cq.message.answer(bm.i18n.t("cli_autofill_failed", err=bm.ux.err_text(e)))
        return
    if not patch:
        await cq.message.answer(bm.i18n.t("cli_autofill_empty"))
        return
    before = await bm.CLIENTS.get_by_account(customer_id)
    operation = "profile_update" if before is not None else "profile_save"
    # preview_merge = ТА ЖЕ merge-семантика, что исполнит apply_upsert (§20.5) — превью правдиво.
    after = bm.preview_merge(before, patch)
    summary = bm.texts.fmt_client_diff(before, after, customer_id, operation=operation)
    await bm._present_memory_proposal(
        cq.message.bot,
        chat_id=chat_id,
        operation=operation,
        customer_id=customer_id,
        params={"customer_id": customer_id, "patch": patch, "source": "account"},
        summary=summary,
    )


@bm.dp.callback_query(bm.ClientCB.filter(bm.F.action == "recrawl"))
async def cli_recrawl_cb(
    cq: bm.CallbackQuery, callback_data: bm.ClientCB, state: bm.FSMContext
) -> None:
    """§20.4/20.5: перекраулить сайт клиента (из профиля). sub='incr' → инкрементальный («только
    новое», diff по content_hash); иначе полный. Запуск — фоново, сводка по готовности."""
    chat_id = bm._cq_chat_id(cq)
    customer_id = await bm._cli_selected_account(state)
    if not customer_id:
        await cq.answer(bm.i18n.t("cli_card_stale"), show_alert=True)
        return
    if not await bm._cli_check_access(chat_id, customer_id):
        await cq.answer(bm.i18n.t("cli_access_denied"), show_alert=True)
        return
    profile = await bm.CLIENTS.get_by_account(customer_id)
    url = (profile or {}).get("website")
    if not url:
        await cq.answer(bm.i18n.t("cli_no_website"), show_alert=True)
        return
    if not str(url).startswith(("http://", "https://")):
        url = "https://" + str(url).lstrip("/")
    from urllib.parse import urlparse

    mode = "incremental" if callback_data.sub == "incr" else "full"
    await cq.answer()
    domain = bm.texts.esc(urlparse(url).netloc or url)
    if not bm._spawn_crawl(cq.message.bot, chat_id, customer_id, url, mode=mode):
        await cq.message.answer(
            bm.i18n.t("cli_crawl_already", domain=domain)
        )  # B15: обход уже идёт
        return
    started_key = "cli_recrawl_incr_started" if mode == "incremental" else "cli_crawl_started"
    await cq.message.answer(bm.i18n.t(started_key, domain=domain))
