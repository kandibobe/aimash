"""Ключевые слова §7: /keywords, визард сидов, добавление в кампанию (за confirm-гейтом)

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.message(bm.Command("keywords"))
async def keywords_(m: bm.Message, state: bm.FSMContext, command: bm.CommandObject) -> None:
    """Подбор ключевых слов (read-only, advisory). С аргументами — сразу; иначе спросить."""
    await state.clear()
    seeds, url = bm._parse_kw_input(command.args or "")
    if seeds or url:
        await bm._kw_run(m, m.chat.id, seeds, url, "ru")
        return
    await state.set_state(bm.KwWizard.awaiting_seeds)
    await m.answer(bm.i18n.t("kw_ask"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML)


@bm.dp.message(bm.F.text.in_(bm.BTN_KEYWORDS_ALL))
async def btn_keywords(m: bm.Message, state: bm.FSMContext) -> None:
    """Кнопка «Ключевые слова» = /keywords без аргументов: запускаем визард подбора."""
    await state.clear()
    await state.set_state(bm.KwWizard.awaiting_seeds)
    await m.answer(bm.i18n.t("kw_ask"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML)


@bm.dp.message(bm.KwWizard.awaiting_seeds)
async def kw_seeds(m: bm.Message, state: bm.FSMContext) -> None:
    seeds, url = bm._parse_kw_input(m.text or "")
    if not seeds and not url:
        await m.answer(
            bm.i18n.t("kw_bad_input"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
        return  # остаёмся в состоянии — пользователь пришлёт сиды/URL ещё раз (или «✖ Отмена»)
    await state.clear()
    await bm._kw_run(m, m.chat.id, seeds, url, "ru")


# ── §7: добавить подобранные ключи в кампанию (research → кампания → тип соответствия → «да») ──
@bm.dp.callback_query(bm.KwAddCB.filter(bm.F.action == "start"))
async def on_kw_add_start(
    cq: bm.CallbackQuery, callback_data: bm.KwAddCB, state: bm.FSMContext
) -> None:
    """Старт флоу: ключи лежат в _KW_ADD по токену (не в callback_data) → спрашиваем кампанию.
    Ничего не меняем — это лишь сбор ввода до confirm-гейта."""
    if callback_data.token not in bm._KW_ADD:
        await cq.answer(bm.i18n.t("kw_add_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    await state.clear()
    await state.set_state(bm.KwAdd.awaiting_campaign)
    await state.update_data(kw_add_token=callback_data.token)
    await cq.answer()
    # nav_kb() без back_cb: предыдущий экран — сводка /keywords (нет дешёвого inline-родителя),
    # поэтому только «✖ Отмена» (выход в меню). Этого достаточно — пользователь больше не застрянет.
    await msg.answer(bm.i18n.t("kw_add_pick_campaign"), reply_markup=bm.nav_kb())


@bm.dp.message(bm.KwAdd.awaiting_campaign)
async def kw_add_campaign(m: bm.Message, state: bm.FSMContext) -> None:
    """Получили кампанию → §7 list-UX: показываем кандидаты-ключи СПИСКОМ для правки (менеджер
    копирует, редактирует, присылает обратно), затем выбор типа соответствия."""
    campaign = (m.text or "").strip()
    if not campaign:
        # retry-подсказка ОБЯЗАНА нести nav_kb — иначе после невалидного ввода кнопок снова нет
        # (это и есть исходный баг «застрял без Назад»).
        await m.answer(
            bm.i18n.t("kw_add_empty_campaign"),
            reply_markup=bm.nav_kb(),
            parse_mode=bm.ParseMode.HTML,
        )
        return  # остаёмся в состоянии — пользователь пришлёт название ещё раз
    data = await state.get_data()
    sess = bm._KW_ADD.get(data.get("kw_add_token", ""))
    if not sess:
        await state.clear()
        await m.answer(bm.i18n.t("kw_add_stale"))
        return
    sess["campaign"] = campaign
    await state.set_state(
        bm.KwAdd.awaiting_keywords
    )  # §7 list-UX: правка списка ключей вместо кликов
    await m.answer(
        bm.i18n.t("kw_add_edit_prompt", camp=bm.texts.esc(campaign)),
        reply_markup=bm.nav_kb(),
        parse_mode=bm.ParseMode.HTML,
    )
    await m.answer(
        bm.texts.fmt_kw_candidates(sess.get("keywords") or [])
    )  # плейн — копируется как есть


@bm.dp.message(bm.KwAdd.awaiting_keywords)
async def kw_add_keywords(m: bm.Message, state: bm.FSMContext) -> None:
    """§7 list-UX: менеджер прислал отредактированный список ключей (по одному в строке/через запятую).
    Дедуп (регистронезависимо, порядок), обрезка до 50 (схема AddKeywords) → выбор типа соответствия."""
    data = await state.get_data()
    token = data.get("kw_add_token", "")
    sess = bm._KW_ADD.get(token)
    if not sess:
        await state.clear()
        await m.answer(bm.i18n.t("kw_add_stale"))
        return
    items = [p.strip() for p in (m.text or "").replace("\n", ",").split(",") if p.strip()]
    seen: set[str] = set()
    kws: list[str] = []
    for it in items:  # дедуп регистронезависимо, порядок сохраняем
        key = it.lower()
        if key not in seen:
            seen.add(key)
            kws.append(it)
    if not kws:  # остаёмся в состоянии — менеджер пришлёт список снова
        await m.answer(bm.i18n.t("kw_add_list_empty"), reply_markup=bm.nav_kb())
        return
    en = bm.i18n.current_lang() == "en"
    if len(kws) > 50:  # схема AddKeywords.keywords max 50 — честно сообщаем об обрезке
        kws = kws[:50]
        await m.answer("Оставил первые 50 ключей." if not en else "Kept the first 50 keywords.")
    sess["keywords"] = kws
    await state.clear()
    await m.answer(
        bm.i18n.t("kw_add_pick_match", camp=bm.texts.esc(sess.get("campaign", "")), n=len(kws)),
        reply_markup=bm.match_type_kb(token),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.KwAddCB.filter(bm.F.action == "cancel"))
async def on_kw_add_cancel(cq: bm.CallbackQuery, callback_data: bm.KwAddCB) -> None:
    bm._KW_ADD.pop(callback_data.token, None)
    await cq.answer(bm.i18n.t("cb_cancelled"))
    await bm._safe_edit(cq, bm.i18n.t("rejected"))


@bm.dp.callback_query(bm.KwAddCB.filter(bm.F.action == "match"))
async def on_kw_add_match(cq: bm.CallbackQuery, callback_data: bm.KwAddCB) -> None:
    """Тип соответствия выбран → собрать черновик add_keywords (confirm-гейт + XLSX-для-списка,
    как любой keyword-черновик §5). Само добавление — только после ✅."""
    sess = bm._KW_ADD.pop(callback_data.token, None)
    msg = bm._cq_msg(cq)
    if not sess or not sess.get("campaign") or msg is None:
        await cq.answer(bm.i18n.t("kw_add_stale"), show_alert=True)
        return
    mt = callback_data.mt if callback_data.mt in ("broad", "phrase", "exact") else "broad"
    try:
        cid, operation, params, summary = bm._build_proposal(
            "add_keywords", campaign=sess["campaign"], keywords=sess["keywords"], match_type=mt
        )
    except Exception as e:  # noqa: BLE001 — валидация схемы (длина/пустой список) → понятный ответ
        await cq.answer(bm.i18n.t("cb_error", kind=type(e).__name__), show_alert=True)
        return
    await cq.answer()
    await bm._present_proposal(
        msg,
        chat_id=bm._cq_chat_id(cq),
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
    )
