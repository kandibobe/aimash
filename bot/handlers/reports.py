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


@bm.dp.message(bm.Command("mysheets"))
async def mysheets_(m: bm.Message) -> None:
    """Последние Google-таблицы, созданные ботом для ЭТОГО чата (отчёты /sheets и таблицы ключей
    визарда). Реестр db.sheets_registry: ссылка переживает и рестарт, и TTL черновика визарда.
    Только свой chat_id — ссылка anyone-with-link фактически является ключом доступа. Read-only."""
    from db import sheets_registry

    rows = await sheets_registry.list_recent(m.chat.id, limit=10)
    await m.answer(
        bm.texts.fmt_my_sheets(rows),  # язык — из contextvar (i18n.current_lang), как у fmt_errors
        parse_mode=bm.ParseMode.HTML,
        disable_web_page_preview=True,
    )


@bm.dp.message(bm.Command("mcc"))
async def mcc_(m: bm.Message, command: bm.CommandObject) -> None:
    """ТЗ §8: сводка по всем дочерним аккаунтам MCC за период (7/14/30/90/MTD/LM/ISO). Read-only.
    Без аргумента — выбор периода кнопками (3.1); с аргументом — сразу за него."""
    if not (command.args or "").strip():
        await bm._ask_period(m, "mcc")
        return
    await bm._send_mcc(m, command.args)


@bm.dp.message(bm.Command("searchterms"))
async def searchterms_(m: bm.Message, command: bm.CommandObject) -> None:
    """§7: топ «мусорных» поисковых запросов (клики без конверсий) → предложить в минус-слова за
    confirm-гейтом. Без аргумента — выбор периода кнопками (3.1); с аргументом-периодом
    (7/14/30/90/MTD/LM/ISO/фраза) — сразу за него.
    Read-only до «да»: клик по «🚫» минтит черновик add_negative_keywords (правило 1/2)."""
    if not (command.args or "").strip():
        await bm._ask_period(m, "searchterms")
        return
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


# 3.2а: батч минус-слов чекбоксами. Тогглы/циклы только перерисовывают markup; черновики минтит
# ТОЛЬКО «Минусовать выбранные» (confirm-гейт, правило 1/2). Состояние — server-side по chat_id+gen.
_ST_MT_CYCLE = ("exact", "phrase", "broad")
_ST_LVL_CYCLE = ("campaign", "adgroup", "shared")


async def _st_ctx(
    cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB, *, need_idx: bool = False
) -> tuple[int, list[dict], set[int], dict] | None:
    """Общий анти-stale гейт батч-хендлеров /searchterms: (chat_id, items, sel, opts) или None —
    тогда уже показан alert «список устарел» (клик по старой клавиатуре / после рестарта)."""
    chat_id = bm._cq_chat_id(cq)
    items = bm._SEARCH_TERMS_CACHE.get(chat_id)
    if (
        not items
        or int(getattr(callback_data, "gen", 0)) != bm._SEARCH_TERMS_GEN.get(chat_id, 0)
        or (need_idx and not (0 <= callback_data.idx < len(items)))
    ):
        await cq.answer(bm.i18n.t("searchterms_stale"), show_alert=True)
        return None
    sel = bm._SEARCH_TERMS_SEL.setdefault(chat_id, set())
    opts = bm._SEARCH_TERMS_OPTS.setdefault(
        chat_id, {"mt": "exact", "lvl": "campaign", "ss": None, "ss_choices": [], "acct": ""}
    )
    return chat_id, items, sel, opts


async def _st_redraw(
    cq: bm.CallbackQuery, items: list[dict], gen: int, sel: set[int], opts: dict
) -> None:
    await bm._safe_edit_markup(cq, bm.searchterms_kb(items, gen, selected=sel, opts=opts))


@bm.dp.callback_query(bm.SearchTermsCB.filter(bm.F.action.in_({"toggle", "neg"})))
async def on_searchterms_toggle(cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB) -> None:
    """3.2а: чекбокс по запросу — только выбор и перерисовка markup, НИКАКОГО черновика/мутации.
    'neg' — легаси-кнопки старых сообщений: их gen всегда старый ⇒ гейт даст «список устарел»."""
    ctx = await _st_ctx(cq, callback_data, need_idx=True)
    if ctx is None:
        return
    _chat_id, items, sel, opts = ctx
    sel.symmetric_difference_update({callback_data.idx})
    await cq.answer()
    await _st_redraw(cq, items, callback_data.gen, sel, opts)


@bm.dp.callback_query(bm.SearchTermsCB.filter(bm.F.action == "mt"))
async def on_searchterms_mt(cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB) -> None:
    """3.2а: цикл типа соответствия пакета exact → phrase → broad (только opts + перерисовка)."""
    ctx = await _st_ctx(cq, callback_data)
    if ctx is None:
        return
    _chat_id, items, sel, opts = ctx
    cur = str(opts.get("mt") or "exact")
    i = _ST_MT_CYCLE.index(cur) if cur in _ST_MT_CYCLE else 0
    opts["mt"] = _ST_MT_CYCLE[(i + 1) % len(_ST_MT_CYCLE)]
    await cq.answer(bm.texts.match_type_human(opts["mt"]))
    await _st_redraw(cq, items, callback_data.gen, sel, opts)


@bm.dp.callback_query(bm.SearchTermsCB.filter(bm.F.action == "lvl"))
async def on_searchterms_lvl(cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB) -> None:
    """3.2а: цикл уровня пакета кампания → группа объявлений → общий список. Вход в «общий список»
    без выбранного имени подставляет дефолтное (создаст apply ПОСЛЕ подтверждения, не здесь)."""
    ctx = await _st_ctx(cq, callback_data)
    if ctx is None:
        return
    _chat_id, items, sel, opts = ctx
    cur = str(opts.get("lvl") or "campaign")
    i = _ST_LVL_CYCLE.index(cur) if cur in _ST_LVL_CYCLE else 0
    opts["lvl"] = _ST_LVL_CYCLE[(i + 1) % len(_ST_LVL_CYCLE)]
    if opts["lvl"] == "shared" and not opts.get("ss"):
        opts["ss"] = bm.i18n.t("searchterms_ss_default_name")
    await cq.answer(bm.i18n.t(f"searchterms_lvl_{opts['lvl']}"))
    await _st_redraw(cq, items, callback_data.gen, sel, opts)


@bm.dp.callback_query(bm.SearchTermsCB.filter(bm.F.action == "ss_open"))
async def on_searchterms_ss_open(cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB) -> None:
    """3.2а: пикер общего списка — читаем существующие NEGATIVE_KEYWORDS shared sets аккаунта
    (read-only) и показываем выбор. Сбой чтения → alert, основная клавиатура не трогается."""
    ctx = await _st_ctx(cq, callback_data)
    if ctx is None:
        return
    _chat_id, _items, _sel, opts = ctx
    from ads.client import build_client_async
    from ads.read import list_negative_shared_sets

    acct = str(opts.get("acct") or "")
    try:
        client = await build_client_async(acct)
        choices = await bm.run_ads_read_call(
            list_negative_shared_sets, client, acct, label="list_negative_shared_sets"
        )
    except Exception as e:  # noqa: BLE001 — сеть/доступ/SDK: короткий редактированный alert
        await cq.answer(await bm._friendly_error(e, "searchterms:ss", short=True), show_alert=True)
        return
    opts["ss_choices"] = choices
    await cq.answer(bm.i18n.t("searchterms_ss_pick_hint"))
    await bm._safe_edit_markup(cq, bm.searchterms_sharedset_kb(choices, callback_data.gen))


@bm.dp.callback_query(bm.SearchTermsCB.filter(bm.F.action == "ss_pick"))
async def on_searchterms_ss_pick(cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB) -> None:
    """3.2а: выбор общего списка из пикера. idx=-1 — «новый» с дефолтным именем: здесь только ИМЯ
    в opts, сам список создаст apply_add_negatives_to_shared_set ПОСЛЕ подтверждения (правило 1)."""
    ctx = await _st_ctx(cq, callback_data)
    if ctx is None:
        return
    _chat_id, items, sel, opts = ctx
    choices = list(opts.get("ss_choices") or [])
    if callback_data.idx == -1:
        opts["ss"] = bm.i18n.t("searchterms_ss_default_name")
    elif 0 <= callback_data.idx < len(choices):
        opts["ss"] = str(choices[callback_data.idx].get("name") or "")
    else:
        await cq.answer(bm.i18n.t("searchterms_stale"), show_alert=True)
        return
    opts["lvl"] = "shared"
    await cq.answer(opts["ss"])
    await _st_redraw(cq, items, callback_data.gen, sel, opts)


@bm.dp.callback_query(bm.SearchTermsCB.filter(bm.F.action == "ss_back"))
async def on_searchterms_ss_back(cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB) -> None:
    """3.2а: выход из пикера общего списка без изменений."""
    ctx = await _st_ctx(cq, callback_data)
    if ctx is None:
        return
    _chat_id, items, sel, opts = ctx
    await cq.answer()
    await _st_redraw(cq, items, callback_data.gen, sel, opts)


@bm.dp.callback_query(bm.SearchTermsCB.filter(bm.F.action == "apply"))
async def on_searchterms_apply(cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB) -> None:
    """3.2а: «Минусовать выбранные» → черновик(и) на весь пакет за confirm-гейтом; сам SDK — только
    после «да» (confirmation_id, правило 1/2). Уровень «общий список» — ОДИН черновик на пакет;
    кампания/группа при запросах из РАЗНЫХ кампаний — по черновику на кампанию (схема привязана к
    одной). Целимся в аккаунт, с которого термины ПРОЧИТАНЫ (opts['acct'], «мутируем то, что
    видим») — не в активный на момент клика; замок ensure_allowed внутри _present_proposal."""
    ctx = await _st_ctx(cq, callback_data)
    if ctx is None:
        return
    chat_id, items, sel, opts = ctx
    picked = [items[i] for i in sorted(sel) if 0 <= i < len(items)]
    if not picked:
        await cq.answer(bm.i18n.t("searchterms_none_selected"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    mt = str(opts.get("mt") or "exact")
    lvl = str(opts.get("lvl") or "campaign")
    batches: list[tuple[str, dict]] = []
    if lvl == "shared":
        batches.append(
            (
                "add_negatives_to_shared_set",
                {
                    "shared_set": str(opts.get("ss") or bm.i18n.t("searchterms_ss_default_name")),
                    "keywords": [it["term"] for it in picked],
                    "match_type": mt,
                },
            )
        )
    else:
        groups: dict[tuple[str, str | None], list[str]] = {}
        for it in picked:
            ag = (it.get("ad_group") or None) if lvl == "adgroup" else None
            groups.setdefault((it["campaign"], ag), []).append(it["term"])
        for (camp, ag), terms in groups.items():
            kw: dict = {"campaign": camp, "keywords": terms, "match_type": mt}
            if ag:
                kw["ad_group"] = ag
            batches.append(("add_negative_keywords", kw))
    try:
        proposals = [bm._build_proposal(op, **kw) for op, kw in batches]
    except Exception as e:  # noqa: BLE001 — валидация схемы (пустой/длинный ключ) → понятный ответ
        await cq.answer(
            await bm._friendly_error(e, "searchterms:apply", short=True), show_alert=True
        )
        return
    await cq.answer()
    bm._SEARCH_TERMS_SEL[chat_id] = set()  # выбор израсходован; сами термины остаются
    await bm._safe_edit_markup(
        cq, bm.searchterms_kb(items, callback_data.gen, selected=set(), opts=opts)
    )
    acct = str(opts.get("acct") or "")
    for cid, operation, params, summary in proposals:
        if acct:
            await bm._present_proposal(
                msg,
                chat_id=chat_id,
                operation=operation,
                params=params,
                summary=summary,
                cid=cid,
                customer_id=acct,
            )
        else:  # старый кэш без аккаунта чтения — прежняя семантика активного аккаунта
            await bm._present_proposal_active(
                msg, chat_id=chat_id, operation=operation, params=params, summary=summary, cid=cid
            )


@bm.dp.callback_query(bm.SearchTermsCB.filter(bm.F.action == "add"))
async def on_searchterms_add(cq: bm.CallbackQuery, callback_data: bm.SearchTermsCB) -> None:
    """Ф4 «сбор урожая»: «➕ в ключи» по конвертящему запросу → черновик add_keywords (EXACT, в ТУ
    группу, где запрос уже крутился) за confirm-гейтом. Сам SDK — только после «да» (правило 1/2).

    Группу передаём ЯВНО: без неё add_keywords положил бы ключ во ВСЕ группы кампании — это ровно та
    каннибализация, которую флажит аудит. idx указывает в _SEARCH_TERMS_HARVEST (не в «мусорный»
    кэш!), gen общий: клик по старой клавиатуре после повторного /searchterms → «список устарел»."""
    chat_id = bm._cq_chat_id(cq)
    items = bm._SEARCH_TERMS_HARVEST.get(chat_id)
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
            "add_keywords",
            campaign=it["campaign"],
            ad_group=it["ad_group"],
            keywords=[it["term"]],
            match_type="exact",
        )
    except Exception as e:  # noqa: BLE001 — валидация схемы (пустой/длинный ключ) → понятный ответ
        await cq.answer(await bm._friendly_error(e, "searchterms:add", short=True), show_alert=True)
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
    """§8: кнопка «MCC (все аккаунты)» = /mcc без аргумента → выбор периода кнопками (3.1)."""
    await bm._ask_period(m, "mcc")


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
    elif kind == "kwadd":  # #2: пикер кампаний /addkeys (target несёт token сессии)
        camps = bm._KW_ADD_CAMP_CACHE.get(chat_id)
        if not camps:
            await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
            return
        markup = bm.kw_add_campaigns_kb(
            camps, callback_data.target, gen=bm._KW_ADD_CAMP_GEN.get(chat_id, 0), page=page
        )
    elif kind == "smut":  # #2: пикер кампаний /pause,/resume (target несёт op)
        camps = bm._SLASH_MUT_CACHE.get(chat_id)
        if not camps:
            await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
            return
        markup = bm.slash_mutate_campaigns_kb(
            camps, callback_data.target, gen=bm._SLASH_MUT_GEN.get(chat_id, 0), page=page
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
async def on_report_account(
    cq: bm.CallbackQuery, callback_data: bm.ReportAcctCB, state: bm.FSMContext
) -> None:
    """Выбран аккаунт в пикере /report /export /sheets → показать выбор кампании (или весь аккаунт).

    state нужен только ветке target=="audit": после карточки включаем режим доп-вопросов (Q&A, #6).
    aiogram подставляет FSMContext по cq.from_user (правильный ключ хранилища) — сами его не строим."""
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
    ):  # §6/§8: пикер → выбор периода → статистика (3.1; замок чтения — в _report_target)
        await bm._start_status_period(msg, bm._cq_chat_id(cq), acct)
        return
    if callback_data.target == "advise":  # advisor на ВЫБРАННОМ аккаунте (замок — в _advise_run)
        topic = bm._ADVISE_TOPIC_CACHE.pop(bm._cq_chat_id(cq), None)  # тема из пикера (или все)
        await bm._advise_run(msg, bm._cq_chat_id(cq), topic=topic, account=acct)
        return
    if callback_data.target == "audit":  # аудит на ВЫБРАННОМ аккаунте (замок — в _audit_run)
        p = bm._AUDIT_PERIOD_CACHE.pop(
            bm._cq_chat_id(cq), None
        )  # Period из пикера (или дефолт 30 в _audit_run)
        await bm._audit_run(msg, bm._cq_chat_id(cq), account=acct, period=p, state=state)
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
# 3.1: «📅 Свой период…» — РАНЬШЕ пресетных хендлеров (первый совпавший фильтр выигрывает;
# ниже period_report матчит target=='report' с ЛЮБЫМ code, включая CUST).
@bm.dp.callback_query(bm.PeriodCB.filter(bm.F.code == "CUST"))
async def period_custom(
    cq: bm.CallbackQuery, callback_data: bm.PeriodCB, state: bm.FSMContext
) -> None:
    """3.1: «📅 Свой период…» → одноразовый FSM (PeriodCustom.awaiting, паттерн PickerSearch):
    ждём диапазон текстом («вчера», «с 1 по 15 июня», ISO — parse_period_text). target — в
    state-data; сам запуск отчёта — в on_period_custom_input."""
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    await state.set_state(bm.PeriodCustom.awaiting)
    await state.update_data(period_target=callback_data.target)
    await msg.answer(bm.i18n.t("period_custom_prompt"))


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
    # ФИКС B2: строим на ВЫБРАННОМ аккаунте/кампании (_report_target), а не на хардкоде Draft.
    await bm._dispatch_period_target(msg, bm._cq_chat_id(cq), "report", period, callback_data.code)


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
    await bm._dispatch_period_target(msg, bm._cq_chat_id(cq), "export", period, callback_data.code)


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
    await bm._dispatch_period_target(msg, bm._cq_chat_id(cq), "sheets", period, callback_data.code)


@bm.dp.callback_query(
    bm.PeriodCB.filter(bm.F.target.in_({"audit", "status", "bids", "searchterms", "mcc"}))
)
async def period_more_targets(
    cq: bm.CallbackQuery, callback_data: bm.PeriodCB, state: bm.FSMContext
) -> None:
    """3.1: пресет периода для остальных отчётных команд (audit/status/bids/searchterms/mcc) →
    единый диспатч. state нужен только audit-ветке (Q&A после карточки). READ-ONLY."""
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    try:
        period = bm._period_from_arg(callback_data.code)
    except ValueError:
        await msg.answer(bm.i18n.t("err_period"))
        return
    await bm._dispatch_period_target(
        msg, bm._cq_chat_id(cq), callback_data.target, period, callback_data.code, state=state
    )


@bm.dp.message(bm.PeriodCustom.awaiting, bm.F.text)
async def on_period_custom_input(m: bm.Message, state: bm.FSMContext) -> None:
    """3.1: произвольный период текстом после «📅 Свой период…». Одноразовый (паттерн PickerSearch):
    state снят ДО запуска — и разобранный, и нераспознанный ввод выходят из режима (повтор — снова
    кнопкой). Нераспознано → подсказка + та же клавиатура периода. Пассивный выход /командой или
    кнопкой меню дают middleware'ы (SlashCommandExitsWizardMiddleware, btn_guard_menu)."""
    data = await state.get_data()
    target = data.get("period_target") or "report"
    await state.clear()
    text = (m.text or "").strip()
    period = None
    if text:  # пустой/пробельный ввод → как нераспознанный (не молчаливый дефолт 30 дн.)
        try:
            period = bm._period_from_arg(text)
        except ValueError:
            period = None
    if period is None:
        await m.answer(bm.i18n.t("err_period"))
        await m.answer(
            bm.i18n.t(f"period_pick_{target}"),
            reply_markup=bm.period_kb(target, last=await bm._last_period(m.chat.id)),
        )
        return
    # code=None: произвольный диапазон — разовый, в §UX-память не пишем (внутри диспатчер
    # передаст ISO-пару дат тем веткам, которым нужен строковый код: mcc/deep-export/recall).
    await bm._dispatch_period_target(m, m.chat.id, target, period, None, state=state)


@bm.dp.callback_query(bm.AuditExportCB.filter())
async def on_audit_export(cq: bm.CallbackQuery, callback_data: bm.AuditExportCB) -> None:
    """Кнопки «📄 В Google Sheets» / «📊 Скачать .xlsx» под карточкой /audit — выгрузка УЖЕ
    посчитанного результата (кэш bot.main). Read-only, GR3: бумага. Аудит по клику не пересобираем."""
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    await bm._run_audit_export(msg, callback_data.fmt, bm._cq_chat_id(cq))


# ── #6: режим доп-вопросов (Q&A) по последнему /audit ──────────────────────────────
# Свободный текст в состоянии AuditQA.active = вопрос к READ-ONLY аналитику (run_analysis_agent,
# тот же fact-guard, БЕЗ мутаций). Пассивный выход обеспечивают middleware'ы: /команда снимается
# SlashCommandExitsWizardMiddleware, кнопка меню — btn_guard_menu (оба зарегистрированы РАНЬШЕ),
# поэтому сюда доходит только настоящий свободный текст. Хендлер стоит после menu_guard и ДО catch-all
# on_text (порядок HANDLER_MODULES) — инвариант «on_text строго последний» сохранён.
@bm.dp.message(bm.AuditQA.active, bm.F.text)
async def on_audit_qa_question(m: bm.Message, state: bm.FSMContext) -> None:
    """Вопрос по показанному аудиту → ответ аналитика по КЭШИРОВАННЫМ фактам (числа = КОД движка).
    READ-ONLY: аккаунт залочен (drill проходит ensure_read_allowed внутри ридеров), мутаций нет —
    исполнение любого совета остаётся отдельной командой через confirm-гейт. Холодный кэш (рестарт) →
    снимаем состояние + подсказка. Ответ не прошёл fact-guard/сбой → мягкий фолбэк (сырой str(e) не шлём,
    GR5). Остаёмся в режиме — можно задать следующий вопрос; выход командой/кнопкой меню/«✖ Выйти»."""
    chat_id = m.chat.id
    lang = bm.i18n.get_lang(chat_id)
    ctx = bm._AUDIT_QA_CACHE.get(chat_id)
    if not ctx:  # рестарт/новый /audit затёр — режим уже неактуален
        await state.clear()
        await m.answer(bm.i18n.t("audit_qa_stale", lang))
        return
    facts, acct, period = ctx
    question = (m.text or "").strip()
    if not question:
        return
    answer: str | None = None
    async with bm.ux.typing_action(m):  # «печатает…» пока идёт чтение/LLM
        try:
            from ads.client import build_client_async
            from agent.loop import run_analysis_agent

            client = await build_client_async(acct)
            answer = await run_analysis_agent(
                facts,
                chat_id=chat_id,
                lang=lang,
                question=question,
                drill=bm._make_audit_drill(client, acct, period),
            )
        except Exception as e:  # noqa: BLE001 — сеть/доступ/SDK: мягкий фолбэк, карточка уже была
            bm.log.warning("audit Q&A не удался: %s", type(e).__name__)
            answer = None
    if answer:
        await m.answer("🧠 " + answer, parse_mode=None, reply_markup=bm.audit_qa_exit_kb(lang))
    else:  # None = сбой/timeout/бюджет-стоп/выдуманное число (fact-guard) — не выдумываем ответ
        await m.answer(bm.i18n.t("audit_qa_failed", lang), reply_markup=bm.audit_qa_exit_kb(lang))


@bm.dp.callback_query(bm.AuditQaCB.filter())
async def on_audit_qa_exit(cq: bm.CallbackQuery, state: bm.FSMContext) -> None:
    """«✖ Выйти» — явный выход из режима доп-вопросов: снимаем FSM и контекст-кэш. Пассивно из режима
    выводят и /команда, и кнопка меню — эта кнопка лишь очевидный ручной выход."""
    await cq.answer()
    chat_id = bm._cq_chat_id(cq)
    bm._AUDIT_QA_CACHE.pop(chat_id, None)
    await state.clear()
    msg = bm._cq_msg(cq)
    if msg is not None:
        await msg.answer(bm.i18n.t("audit_qa_exited", bm.i18n.get_lang(chat_id)))


# ── 3.5: действия под сводкой /mcc ─────────────────────────────────────────────────
@bm.dp.callback_query(bm.MccAcctCB.filter())
async def on_mcc_account(cq: bm.CallbackQuery, callback_data: bm.MccAcctCB) -> None:
    """Тап по аккаунту под /mcc → закрепить его АКТИВНЫМ аккаунтом чтения (персист). Зеркало ветки
    target='setacct' в on_report_account, но id — из самой кнопки (не из кэша-индекса): кнопка
    переживает рестарт и перезапись _REPORT_ACCT_CACHE другим пикером. Замок перепроверяется
    fail-closed: read-замок × пер-юзер грант. Мутационный замок не затрагивается (GR9)."""
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    chat_id = bm._cq_chat_id(cq)
    acct = bm.normalize_customer_id(callback_data.cid)
    try:
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


@bm.dp.callback_query(bm.MccAuditCB.filter())
async def on_mcc_audit_all(cq: bm.CallbackQuery, callback_data: bm.MccAuditCB) -> None:
    """«▶️ Аудит по всем» под /mcc → фоновый score-прогон дочерних (_mcc_audit_all). READ-ONLY:
    чтение + снапшот в локальную БД, мутаций/proposal нет — confirm-гейт не нужен. Один прогон
    на чат (двойной тап → алерт); кэш списка потерян (рестарт) → stale-алерт."""
    chat_id = bm._cq_chat_id(cq)
    msg = bm._cq_msg(cq)
    cids = bm._MCC_AUDIT_CACHE.get(chat_id)
    if msg is None or not cids:
        await cq.answer(bm.i18n.t("stale"), show_alert=True)
        return
    if chat_id in bm._MCC_AUDIT_RUNNING:
        await cq.answer(bm.i18n.t("mcc_audit_running"), show_alert=True)
        return
    await cq.answer()
    bm._MCC_AUDIT_RUNNING.add(chat_id)
    try:
        await bm._mcc_audit_all(msg, chat_id, list(cids))
    finally:
        bm._MCC_AUDIT_RUNNING.discard(chat_id)
