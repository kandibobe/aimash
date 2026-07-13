"""Ключевые слова §7: /keywords, визард сидов, добавление в кампанию (за confirm-гейтом)

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm
from ads.keyword_plan import MAX_SEEDS


async def _kw_cap_seeds(m: bm.Message, seeds: list[str]) -> list[str]:
    """C1: Google берёт не больше MAX_SEEDS=20 сид-ключей за запрос (лишний сид ⇒ InvalidArgument
    на ВЕСЬ подбор). Режем на входе и говорим об этом вслух — молчаливое усечение читалось бы как
    «подобрал по всему списку»."""
    if len(seeds) <= MAX_SEEDS:
        return seeds
    await m.answer(bm.i18n.t("kw_seeds_capped", n=MAX_SEEDS, total=len(seeds)))
    return seeds[:MAX_SEEDS]


async def _kw_take_seeds(m: bm.Message, raw: str) -> tuple[list[str], str | None]:
    """Разбор ввода пользователя (сиды/URL) + честный кап сидов до лимита Google."""
    seeds, url = bm._parse_kw_input(raw)
    return await _kw_cap_seeds(m, seeds), url


async def _kw_present_params(m: bm.Message, state: bm.FSMContext) -> None:
    """3F (§7): экран параметров research (ГЕО/язык/сеть/период) с разумными дефолтами.
    Раньше из 4 параметров ТЗ §7 пользователю не был доступен НИ ОДИН (гео=Украина,
    язык='ru' хардкодом, сеть/период зашиты)."""
    from ads.geo import keyword_ideas_lang

    data = await state.get_data()
    if not data.get("kw_lang"):  # дефолт языка = язык интерфейса (не хардкод 'ru')
        await state.update_data(kw_lang=keyword_ideas_lang(bm.i18n.current_lang()))
    await state.set_state(bm.KwWizard.params)
    cfg = await state.get_data()
    await m.answer(
        bm.i18n.t("kw_params_title"),
        reply_markup=bm.kw_params_kb(cfg),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.Command("keywords"))
async def keywords_(m: bm.Message, state: bm.FSMContext, command: bm.CommandObject) -> None:
    """Подбор ключевых слов (read-only, advisory). С аргументами — сразу к параметрам
    (сиды предзаполнены); иначе спросить сиды."""
    await state.clear()
    seeds, url = await _kw_take_seeds(m, command.args or "")
    if seeds or url:
        await state.update_data(kw_seeds=seeds, kw_url=url)
        await _kw_present_params(m, state)
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
    seeds, url = await _kw_take_seeds(m, m.text or "")
    if not seeds and not url:
        await m.answer(
            bm.i18n.t("kw_bad_input"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
        return  # остаёмся в состоянии — пользователь пришлёт сиды/URL ещё раз (или «✖ Отмена»)
    await state.update_data(kw_seeds=seeds, kw_url=url)
    await _kw_present_params(m, state)  # 3F: параметры §7 перед подбором


async def _kw_run_from_state(m: bm.Message, state: bm.FSMContext) -> None:
    """Запуск подбора с параметрами из state-data (экран 3F)."""
    from ads.geo import geo_id_for_country

    cfg = await state.get_data()
    seeds = list(cfg.get("kw_seeds") or [])
    url = cfg.get("kw_url")
    # N3b: без сид-слов/URL НЕ запускаем (иначе сырой ValueError «нужен хотя бы один сид-ключ или URL»
    # из generate_keyword_ideas). Остаёмся на экране параметров (state НЕ чистим) — пользователь
    # пришлёт сиды текстом (их поймает kw_params_text) и снова нажмёт «🚀 Подобрать».
    if not seeds and not url:
        await m.answer(bm.i18n.t("kw_params_need_seeds"))
        return
    lang = str(cfg.get("kw_lang") or "ru")
    # A1: дефолт гео — из settings.geo_default_country (пусто ⇒ «any» = глобально), без хардкода «UA».
    from core.config import settings

    geo_iso = str(cfg.get("kw_geo_iso") or (settings.geo_default_country or "any"))
    if geo_iso == "any":
        geo_ids: tuple[int, ...] | None = ()  # явно «все страны» → глобальный подбор (не Украина)
    else:
        gid = geo_id_for_country(geo_iso)
        # неизвестный ISO → None: _kw_run подставит «домашний» дефолт из settings (не Украину)
        geo_ids = (gid,) if gid else None
    months_raw = int(cfg.get("kw_months") or 0)
    # state НЕ чистим до/после запуска: пользователь остаётся на экране параметров (может подправить
    # и пере-запустить), а любой текст тут ловит kw_params_text (не утекает в агента). Выход — /cancel
    # или кнопка меню. Раньше ранний state.clear() оставлял state=None → следующий URL уходил в ingest.
    await bm._kw_run(
        m,
        m.chat.id,
        seeds,
        url,
        lang,
        geo_ids=geo_ids,
        network=str(cfg.get("kw_net") or "GOOGLE_SEARCH"),
        months=months_raw or None,
    )


@bm.dp.message(bm.KwWizard.params)
async def kw_params_text(m: bm.Message, state: bm.FSMContext) -> None:
    """N5/N3b: текст, присланный на экране параметров /keywords, — это доп. сид-слова/URL (а не
    задача агенту). Раньше хендлера тут не было → текст/URL падал в on_text→ingest («Прочитал…
    контекст»). Мержим в kw_seeds/kw_url и пере-показываем параметры."""
    seeds, url = bm._parse_kw_input(m.text or "")
    if not seeds and not url:
        await m.answer(bm.i18n.t("kw_params_need_seeds"))
        return
    data = await state.get_data()
    merged = list(data.get("kw_seeds") or [])
    seen = {s.casefold() for s in merged}
    for s in seeds:  # дедуп регистронезависимо, порядок сохраняем
        if s.casefold() not in seen:
            seen.add(s.casefold())
            merged.append(s)
    merged = await _kw_cap_seeds(m, merged)  # C1: суммарно тоже не больше лимита Google
    await state.update_data(kw_seeds=merged, kw_url=url or data.get("kw_url"))
    url_note = "" if not (url or data.get("kw_url")) else " + URL"
    await m.answer(bm.i18n.t("kw_params_seeds_added", n=len(merged), url=url_note))
    await _kw_present_params(m, state)


@bm.dp.callback_query(bm.KwCfgCB.filter())
async def kw_params_cb(
    cq: bm.CallbackQuery, callback_data: bm.KwCfgCB, state: bm.FSMContext
) -> None:
    """3F: тапы экрана параметров — цикл значений/саб-пикер ГЕО/запуск. Read-only (state-data)."""
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    field, value = callback_data.field, callback_data.value
    if field == "run":
        await cq.answer()
        await _kw_run_from_state(msg, state)
        return
    if field == "geo":
        await cq.answer()
        await msg.answer(bm.i18n.t("kw_params_pick_geo"), reply_markup=bm.kw_geo_kb())
        return
    if field == "lang":  # §7: открыть саб-пикер языка (был 3-тактный цикл ru/uk/en)
        await cq.answer()
        await msg.answer(bm.i18n.t("kw_params_pick_lang"), reply_markup=bm.kw_lang_kb())
        return
    if field == "geo_pick":
        if value == "custom":
            await state.set_state(bm.KwWizard.awaiting_geo)
            await cq.answer()
            await msg.answer(bm.i18n.t("kw_params_ask_geo"), reply_markup=bm.nav_kb())
            return
        await state.update_data(kw_geo_iso=value)
        await state.set_state(bm.KwWizard.params)
    elif field == "lang_pick":  # §7: пресет/«любой»/ручной ввод языка
        if value == "custom":
            await state.set_state(bm.KwWizard.awaiting_lang)
            await cq.answer()
            await msg.answer(bm.i18n.t("kw_params_ask_lang"), reply_markup=bm.nav_kb())
            return
        await state.update_data(kw_lang=value)  # ISO из пресета или 'any' (язык не фильтруем)
        await state.set_state(bm.KwWizard.params)
    elif field == "net":
        cur = str((await state.get_data()).get("kw_net") or "GOOGLE_SEARCH")
        await state.update_data(
            kw_net="GOOGLE_SEARCH_AND_PARTNERS" if cur == "GOOGLE_SEARCH" else "GOOGLE_SEARCH"
        )
    elif field == "period":  # цикл авто → 12 → 24
        cur = int((await state.get_data()).get("kw_months") or 0)
        await state.update_data(kw_months={0: 12, 12: 24}.get(cur, 0))
    await cq.answer()
    cfg = await state.get_data()
    await bm._safe_edit(
        cq,
        bm.i18n.t("kw_params_title"),
        reply_markup=bm.kw_params_kb(cfg),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.KwWizard.awaiting_geo)
async def kw_geo_text(m: bm.Message, state: bm.FSMContext) -> None:
    """3F: ручной ввод страны («Кения»/«KE») → ISO через ads.geo; не распозналось → retry."""
    from ads.geo import country_iso, geo_id_for_country

    iso = country_iso(m.text or "")
    if not iso or not geo_id_for_country(iso):
        await m.answer(bm.i18n.t("kw_params_geo_unknown"), reply_markup=bm.nav_kb())
        return  # остаёмся в состоянии — пользователь уточнит (или «✖ Отмена»)
    await state.update_data(kw_geo_iso=iso)
    await _kw_present_params(m, state)


@bm.dp.message(bm.KwWizard.awaiting_lang)
async def kw_lang_text(m: bm.Message, state: bm.FSMContext) -> None:
    """§7: ручной ввод ЯЗЫКА подбора («немецкий»/«de»/«japanese») → ISO через ads.geo.lang_iso.
    Не распозналось Google — retry. Зеркало kw_geo_text; любой язык из таблицы Google достижим."""
    from ads.geo import lang_iso

    iso = lang_iso(m.text or "")
    if not iso:
        await m.answer(bm.i18n.t("kw_params_lang_unknown"), reply_markup=bm.nav_kb())
        return  # остаёмся в состоянии — пользователь уточнит (или «✖ Отмена»)
    await state.update_data(kw_lang=iso)
    await _kw_present_params(m, state)


# ── §7: добавить ключи в кампанию (свой файл/ссылка/текст → тип соответствия → «да») ──
async def _kw_add_present_campaign(msg: bm.Message, chat_id: int, token: str) -> None:
    """D3: показать шаг выбора кампании /addkeys — пикером кампаний активного аккаунта, если он
    доступен (текст-ввод названия остаётся: kw_add_campaign ловит любое имя). Крупный аккаунт/
    сбой чтения ⇒ только текст-подсказка (пикер опционален)."""
    camps = await bm._kw_add_load_campaigns(chat_id)
    if camps:
        gen = bm._kw_add_store(chat_id, camps)
        await msg.answer(
            bm.i18n.t("kw_add_pick_campaign_list"),
            reply_markup=bm.kw_add_campaigns_kb(camps, token, gen=gen),
            parse_mode=bm.ParseMode.HTML,
        )
    else:
        await msg.answer(bm.i18n.t("kw_add_pick_campaign"), reply_markup=bm.nav_kb())


async def _kw_add_set_campaign(
    msg: bm.Message, state: bm.FSMContext, token: str, campaign: str
) -> None:
    """Общий хвост «кампания выбрана» (из пикера или из текста): фиксируем кампанию в сессии и
    переходим к §7 list-UX (правка кандидатов ИЛИ приём своего списка). Гейт не тратим."""
    sess = bm._KW_ADD.get(token)
    if not sess:
        await state.clear()
        await msg.answer(bm.i18n.t("kw_add_stale"))
        return
    sess["campaign"] = campaign
    await state.set_state(bm.KwAdd.awaiting_keywords)  # §7 list-UX: правка списка ключей
    if sess.get("keywords"):  # старт из research: кандидаты уже есть — показать для правки
        await msg.answer(
            bm.i18n.t("kw_add_edit_prompt", camp=bm.texts.esc(campaign)),
            reply_markup=bm.nav_kb(),
            parse_mode=bm.ParseMode.HTML,
        )
        await msg.answer(
            bm.texts.fmt_kw_candidates(sess.get("keywords") or [])
        )  # плейн — копируется
        return
    await msg.answer(  # P3: свой список — файл xlsx/csv/txt, ссылка на Google Sheets или текст
        bm.i18n.t("kw_add_send_list", camp=bm.texts.esc(campaign)),
        reply_markup=bm.nav_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


async def addkeys_start(m: bm.Message, state: bm.FSMContext) -> None:
    """P3 (фидбэк заказчика 2026-07-06): отдельный вход «добавить ключи в кампанию» из меню/
    команды — вместо кнопки под отчётом research. Принимает СВОЙ список: файл xlsx/csv/txt,
    ссылку на Google Sheets или текст. Ничего не меняет — сбор ввода до confirm-гейта.
    D3: шаг кампании — пикером (текст-ввод названия остаётся фолбэком)."""
    await state.clear()
    token = bm._kw_add_put([], "manual")
    await state.set_state(bm.KwAdd.awaiting_campaign)
    await state.update_data(kw_add_token=token)
    await _kw_add_present_campaign(m, m.chat.id, token)


@bm.dp.message(bm.Command("addkeys"))
async def addkeys_cmd(m: bm.Message, state: bm.FSMContext) -> None:
    await addkeys_start(m, state)


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
    # D3: пикер кампаний активного аккаунта (текст-ввод названия остаётся). nav-выход — «✖ Отмена».
    await _kw_add_present_campaign(msg, bm._cq_chat_id(cq), callback_data.token)


@bm.dp.callback_query(bm.KwAddCB.filter(bm.F.action == "camp"))
async def on_kw_add_pick_campaign(
    cq: bm.CallbackQuery, callback_data: bm.KwAddCB, state: bm.FSMContext
) -> None:
    """D3: выбор кампании в пикере /addkeys → тот же хвост, что и текст-ввод (сбор до confirm-гейта)."""
    chat_id = bm._cq_chat_id(cq)
    camps = bm._KW_ADD_CAMP_CACHE.get(chat_id)
    # N1.4-ревью: gen сверяет клавиатуру со СВОИМ снапшотом списка (кэш мог перезаписать
    # fuzzy-подсказка опечатки — idx старой кнопки указывал бы в другой список).
    if (
        not camps
        or not (0 <= callback_data.idx < len(camps))
        or int(getattr(callback_data, "gen", 0)) != bm._KW_ADD_CAMP_GEN.get(chat_id, 0)
    ):
        await cq.answer(bm.i18n.t("camp_list_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    await cq.answer()
    await _kw_add_set_campaign(
        msg, state, callback_data.token, camps[callback_data.idx].get("name", "")
    )


@bm.dp.message(bm.KwAdd.awaiting_campaign)
async def kw_add_campaign(m: bm.Message, state: bm.FSMContext) -> None:
    """Получили кампанию ТЕКСТОМ (фолбэк к пикеру D3) → §7 list-UX: кандидаты списком для правки
    (менеджер копирует, редактирует, присылает обратно), затем выбор типа соответствия."""
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
    # N1.4: опечатка в имени → подсказка ТОЧНЫХ имён кнопками (fail-closed: не подставляем
    # угаданное имя сами — клик идёт штатным пикером on_kw_add_pick_campaign). Сбой/таймаут
    # загрузки/пустой список → старое поведение (имя как есть; «не найдена» скажет исполнение).
    camps = await bm._load_campaigns_briefly(m.chat.id)
    if camps and not any((c.get("name") or "") == campaign for c in camps):
        cands = bm._fuzzy_campaign_candidates(camps, campaign)
        if cands:
            gen = bm._kw_add_store(m.chat.id, cands)
            await m.answer(
                bm.i18n.t("campaign_typo_suggest", name=bm.texts.esc(campaign)),
                reply_markup=bm.kw_add_campaigns_kb(cands, data.get("kw_add_token", ""), gen=gen),
                parse_mode=bm.ParseMode.HTML,
            )
            return  # остаёмся в состоянии — клик по кнопке или новое имя текстом
    await _kw_add_set_campaign(m, state, data.get("kw_add_token", ""), campaign)


async def kw_add_accept(m: bm.Message, state: bm.FSMContext, kws: list[str]) -> None:
    """P3: ЕДИНАЯ точка приёма списка ключей (текст / ссылка на Sheets / файл — все адаптеры
    сходятся сюда): дедуп (регистронезависимо, порядок), обрезка до лимита схемы AddKeywords,
    затем выбор типа соответствия. Гейт не тратится — только сбор ввода."""
    data = await state.get_data()
    token = data.get("kw_add_token", "")
    sess = bm._KW_ADD.get(token)
    if not sess:
        await state.clear()
        await m.answer(bm.i18n.t("kw_add_stale"))
        return
    seen: set[str] = set()
    uniq: list[str] = []
    for it in kws:  # дедуп регистронезависимо, порядок сохраняем
        key = it.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(it)
    if not uniq:  # остаёмся в состоянии — менеджер пришлёт список снова
        await m.answer(bm.i18n.t("kw_add_list_empty"), reply_markup=bm.nav_kb())
        return
    from agent.tools.schemas import ADD_KEYWORDS_MAX  # 2.6: единый источник лимита батча

    if len(uniq) > ADD_KEYWORDS_MAX:  # лимит схемы AddKeywords — честно сообщаем об обрезке
        uniq = uniq[:ADD_KEYWORDS_MAX]
        await m.answer(bm.i18n.t("kw_add_truncated", n=ADD_KEYWORDS_MAX))
    sess["keywords"] = uniq
    await state.clear()
    await m.answer(
        bm.i18n.t("kw_add_pick_match", camp=bm.texts.esc(sess.get("campaign", "")), n=len(uniq)),
        reply_markup=bm.match_type_kb(token),
        parse_mode=bm.ParseMode.HTML,
    )


async def kw_add_from_document(m: bm.Message, state: bm.FSMContext, text: str, name: str) -> None:
    """P3: файл xlsx/csv/txt в состоянии KwAdd.awaiting_keywords — это СПИСОК КЛЮЧЕЙ (как на
    Этапе 2 визарда §19.4.1), не задача агенту. Сбой парса оставляет в состоянии с подсказкой."""
    from keywords.ingest import parse_keywords_text

    parsed = parse_keywords_text(text)
    if not parsed:
        await m.answer(bm.i18n.t("kw_add_list_empty"), reply_markup=bm.nav_kb())
        return
    await m.answer(
        bm.i18n.t("cc_kw_file_accepted", name=bm.texts.esc(name)), parse_mode=bm.ParseMode.HTML
    )
    await kw_add_accept(m, state, [k.text for k in parsed])


# F.text ОБЯЗАТЕЛЕН (ревью 2026-07-07): без него state-фильтр матчит и ДОКУМЕНТЫ — файл ключей
# перехватывался здесь (text=None → «пустой список») и никогда не доходил до fallback.on_document.
@bm.dp.message(bm.KwAdd.awaiting_keywords, bm.F.text)
async def kw_add_keywords(m: bm.Message, state: bm.FSMContext) -> None:
    """§7 list-UX + P3: менеджер прислал список ключей ТЕКСТОМ (по одному в строке/через запятую)
    ИЛИ ссылкой на Google Sheets (колонка A; таблица должна быть доступна аккаунту бота)."""
    text = m.text or ""
    if "spreadsheets" in text.lower():
        from reports.sheets import parse_spreadsheet_id, read_keyword_column

        sid = parse_spreadsheet_id(text)
        if not sid:
            # Ссылка на таблицу, которую не распарсили (форма /u/<n>/d/…) — НЕ трактуем URL как
            # «ключевое слово» (ревью 2026-07-07: URL минтился literal-ключом в proposal).
            await m.answer(bm.i18n.t("cc_kw_not_a_link"), reply_markup=bm.nav_kb())
            return
        try:
            kws = await bm.asyncio.to_thread(read_keyword_column, sid)
        except Exception as e:  # noqa: BLE001 — приватная таблица/нет scope → понятная подсказка
            # БЕЗ parse_mode: err_text — plain (сырой HttpError с '<' ломал HTML-парс Telegram,
            # и пользователь не получал подсказку вовсе — ревью 2026-07-07).
            await m.answer(
                bm.i18n.t("kw_add_sheet_read_failed", err=bm.ux.err_text(e)),
                reply_markup=bm.nav_kb(),
            )
            return  # остаёмся в состоянии — пришлёт файл/текст или расшарит таблицу
        # B5-класс: сырые ячейки колонки A фильтруем ЗДЕСЬ (длина/число слов) — иначе один
        # мусорный «ключ» валил весь батч на _build_proposal уже после расхода сессии.
        kws, dropped = bm._cc_sanitize_keywords(kws)
        if dropped:
            await m.answer(bm.i18n.t("cc_kw_dropped", n=dropped))
        await kw_add_accept(m, state, kws)
        return
    # Разбор — тем же парсером, что файловый путь (§19.4.1): снимает маркеры `[ключ]`/`"ключ"` и
    # валидирует ключ (assert_keyword_ok). Раньше текст резался по запятым как есть → маркер уезжал
    # в Google буквально → KEYWORD_HAS_INVALID_CHARS уже ПОСЛЕ ✅ (гейт потрачен, ключи не добавлены).
    # Тип соответствия здесь выбирается кнопкой ниже, поэтому per-key тип из маркера не используем.
    from keywords.ingest import parse_keywords_text

    parsed = parse_keywords_text(text)
    await kw_add_accept(m, state, [k.text for k in parsed])


@bm.dp.callback_query(bm.KwAddCB.filter(bm.F.action == "cancel"))
async def on_kw_add_cancel(cq: bm.CallbackQuery, callback_data: bm.KwAddCB) -> None:
    bm._KW_ADD.pop(callback_data.token, None)
    await cq.answer(bm.i18n.t("cb_cancelled"))
    await bm._safe_edit(cq, bm.i18n.t("rejected"))


@bm.dp.callback_query(bm.KwAddCB.filter(bm.F.action == "match"))
async def on_kw_add_match(cq: bm.CallbackQuery, callback_data: bm.KwAddCB) -> None:
    """Тип соответствия выбран → собрать черновик add_keywords (confirm-гейт + XLSX-для-списка,
    как любой keyword-черновик §5). Само добавление — только после ✅."""
    # НЕ pop до успешной сборки (ревью 2026-07-07): ошибка валидации схемы при уже расходованной
    # сессии превращала все кнопки в «kw_add_stale» — тупик; теперь сессию тратит только успех.
    sess = bm._KW_ADD.get(callback_data.token)
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
        await cq.answer(await bm._friendly_error(e, "kw:add", short=True), show_alert=True)
        return
    bm._KW_ADD.pop(callback_data.token, None)
    await cq.answer()
    # AD.3: ключи на активный аккаунт; при неоднозначности (не закреплён + живых >1) — форс-пикер,
    # затем черновик с баннером аккаунта (кампания резолвится на исполнении на выбранном аккаунте).
    await bm._present_proposal_active(
        msg,
        chat_id=bm._cq_chat_id(cq),
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
    )
