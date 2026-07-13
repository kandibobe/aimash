"""§10 быстрый /newsearch + §11 кампании из медиа (GDN из фото, Video/Demand Gen)

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm

# A7: контекст «✏️ Изменить ставку» per chat_id — параметры последнего черновика /newsearch,
# валюта аккаунта и токен (== cid текущей карточки). Правка ставки минтит НОВЫЙ черновик.
_BID_EDIT: dict[int, dict] = {}


async def _account_currency_safe(customer_id: str) -> str:
    """Валюта аккаунта (ISO) best-effort — для валютно-верного дефолта CPC и подписи. Сбой → ''."""
    try:
        from ads.client import build_client_async

        return await bm._read_currency(await build_client_async(customer_id), customer_id)
    except Exception:  # noqa: BLE001 — валюту не определить → без подписи, дефолт по fallback
        return ""


async def _present_search_proposal(
    m: bm.Message,
    chat_id: int,
    *,
    name: str,
    url: str,
    budget_units: float,
    headlines: list,
    descriptions: list,
    keywords: list,
    cpc_micros: int,
    currency: str,
    customer_id: str,
) -> None:
    """A7: собрать params create_search_campaign + сводку (С видимой CPC-ставкой) и показать
    черновик с кнопкой «✏️ Изменить ставку». Используется и первичным показом, и после правки
    ставки (тогда cpc_micros новый). Исполнение — только после ✅ (тот же confirm-гейт)."""
    params = {
        "campaign_name": name,
        "final_url": url,
        "headlines": headlines,
        "descriptions": descriptions,
        "budget_daily_micros": int(round(budget_units * 1_000_000)),
        "keywords": keywords,
        "match_type": "phrase",
        "cpc_bid_micros": int(cpc_micros),
    }
    validated: dict = bm.SCHEMAS["create_search_campaign"](**params).model_dump()
    cpc_units = int(validated["cpc_bid_micros"]) / 1_000_000
    summary = bm.texts.fmt_search_proposal_summary(
        name,
        url,
        budget_units,
        validated["headlines"],
        validated["descriptions"],
        validated["keywords"],
        validated["match_type"],
        cpc_units=cpc_units,
        currency=currency,
    )
    p = bm.Proposal(
        operation="create_search_campaign", summary=summary, params=validated, chat_id=chat_id
    )
    # Контекст правки ставки: токен = cid этой карточки; параметры для пере-сборки нового черновика.
    _BID_EDIT[chat_id] = {
        "token": p.confirmation_id,
        "name": name,
        "url": url,
        "budget_units": budget_units,
        "headlines": list(headlines),
        "descriptions": list(descriptions),
        "keywords": list(keywords),
        "cpc_micros": int(validated["cpc_bid_micros"]),
        "currency": currency,
        "customer_id": customer_id,
    }
    await bm._present_proposal(
        m,
        chat_id=chat_id,
        operation="create_search_campaign",
        params=validated,
        summary=summary,
        cid=p.confirmation_id,
        customer_id=customer_id,
        extra_confirm_top=(
            bm.i18n.t("search_edit_bid_btn"),
            bm.SearchBidCB(token=p.confirmation_id),
        ),
    )


@bm.dp.message(bm.Command("newsearch"))
async def newsearch_cmd(m: bm.Message, state: bm.FSMContext) -> None:
    await state.clear()
    await state.set_state(bm.SearchWizard.awaiting_brief)
    # AD.3: активный аккаунт не закреплён + живых несколько → пикер ДО брифа (тап пинит аккаунт,
    # состояние не трогаем — бриф придёт следующим сообщением уже на выбранный аккаунт). Раньше
    # /newsearch и GDN были единственными мут-флоу без пикера: черновик молча уходил на Draft.
    await bm.prompt_account_if_ambiguous(m, m.chat.id)
    await m.answer(
        bm.i18n.t("search_ask_brief"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
    )


@bm.dp.message(bm.SearchWizard.awaiting_brief)
async def search_brief(m: bm.Message, state: bm.FSMContext) -> None:
    """Бриф → генерация RSA → ЧЕРНОВИК create_search_campaign (как GDN-визард). Кампания PAUSED,
    исполнение только после ✅ (двойной гейт + user_initiated держит apply_create_search_campaign)."""
    parsed = bm._parse_search_brief(m.text or "")
    if parsed is None:
        await m.answer(
            bm.i18n.t("search_bad_brief"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
        return  # остаёмся в состоянии — пользователь пришлёт бриф ещё раз (или «✖ Отмена»)
    name, url, budget_units, topic, keywords = parsed
    await m.answer(bm.i18n.t("search_generating"))
    # §8/§10: резолвим активный аккаунт и профиль клиента ДО генерации — RSA получает ключи брифа
    # (были распарсены на :118, но терялись) и §20-профиль (бренд/бизнес/УТП). Раньше бриф был
    # topic-only → тексты слабее по релевантности, чем в /rsa и визарде §19.
    customer_id = await bm._active_read_account(m.chat.id)  # §8: создаём на активном аккаунте
    prof = await bm._cc_profile_ctx_account(customer_id)
    try:
        async with bm.ux.typing_action(m):  # 3I: живой индикатор на 10–30с генерации
            draft = await bm._generate_rsa(
                bm.CopyBrief(
                    topic=topic,
                    keywords=(keywords or [])[:50],  # §10: ключи в контекст генерации
                    profile=(prof or None),  # §20: бренд/бизнес/УТП клиента
                    n_headlines=15,
                    n_descriptions=4,
                )
            )
    except Exception as e:  # LLM/сеть
        await state.clear()
        await m.answer(bm.i18n.t("err_text_gen", err=bm.ux.err_text(e)))
        return
    headlines = list(draft.headlines or [])[: bm.RSA_MAX_HEADLINES]
    descriptions = list(draft.descriptions or [])[: bm.RSA_MAX_DESCRIPTIONS]
    if len(headlines) < bm.RSA_MIN_HEADLINES or len(descriptions) < bm.RSA_MIN_DESCRIPTIONS:
        await state.clear()
        await m.answer(bm.i18n.t("search_gen_empty"))
        return
    # A7: max CPC-ставка — валютно-верный дефолт (не хардкод 0.5, абсурдный для UGX/JPY), из
    # core.limits по валюте АКТИВНОГО аккаунта; ставка ВИДНА на карточке и правится кнопкой.
    from core.limits import wizard_default_money_units

    currency = await _account_currency_safe(
        customer_id
    )  # customer_id уже резолвнут выше (до генерации)
    _, cpc_units = wizard_default_money_units(currency)
    cpc_micros = int(round(cpc_units * 1_000_000))
    try:  # defense-in-depth: схема ещё раз проверит длины/составы/URL/бюджет + нормализует ключи
        await _present_search_proposal(
            m,
            m.chat.id,
            name=name,
            url=url,
            budget_units=budget_units,
            headlines=headlines,
            descriptions=descriptions,
            keywords=keywords,
            cpc_micros=cpc_micros,
            currency=currency,
            customer_id=customer_id,
        )
    except Exception as e:
        await state.clear()
        await m.answer(bm.i18n.t("err_validate", err=bm.ux.humanize_validation(e)))
        return
    await state.clear()


@bm.dp.callback_query(bm.SearchBidCB.filter())
async def search_edit_bid(
    cq: bm.CallbackQuery, callback_data: bm.SearchBidCB, state: bm.FSMContext
) -> None:
    """A7: «✏️ Изменить ставку» → спросить новое значение CPC. Не исполняет; правка минтит НОВЫЙ
    черновик за тем же confirm-гейтом. Токен одноразово сверяется с активным контекстом чата."""
    chat_id = bm._cq_chat_id(cq)
    ctx = _BID_EDIT.get(chat_id)
    if not ctx or ctx.get("token") != callback_data.token:
        await cq.answer(bm.i18n.t("search_bid_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer(bm.i18n.t("search_bid_stale"), show_alert=True)
        return
    await cq.answer()
    await state.set_state(bm.SearchWizard.awaiting_bid)
    sfx = f" {ctx['currency']}" if ctx.get("currency") else ""
    await msg.answer(
        bm.i18n.t("search_ask_new_bid", cur=int(ctx["cpc_micros"]) / 1_000_000, sfx=sfx),
        reply_markup=bm.nav_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.SearchWizard.awaiting_bid)
async def search_new_bid(m: bm.Message, state: bm.FSMContext) -> None:
    """Новое значение CPC → пере-собрать черновик /newsearch с этой ставкой (новый confirm-гейт)."""
    ctx = _BID_EDIT.get(m.chat.id)
    if not ctx:
        await state.clear()
        await m.answer(bm.i18n.t("search_bid_stale"))
        return
    raw = (m.text or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if value <= 0:
        await m.answer(bm.i18n.t("search_bid_bad"), reply_markup=bm.nav_kb())
        return  # остаёмся в состоянии — пользователь пришлёт число ещё раз (или «✖ Отмена»)
    await state.clear()
    try:
        await _present_search_proposal(
            m,
            m.chat.id,
            name=ctx["name"],
            url=ctx["url"],
            budget_units=ctx["budget_units"],
            headlines=ctx["headlines"],
            descriptions=ctx["descriptions"],
            keywords=ctx["keywords"],
            cpc_micros=int(round(value * 1_000_000)),
            currency=ctx["currency"],
            customer_id=ctx["customer_id"],
        )
    except Exception as e:  # новая ставка вне диапазона схемы (gt=0, le потолок)
        await m.answer(bm.i18n.t("err_validate", err=bm.ux.humanize_validation(e)))


@bm.dp.message(bm.F.photo)
async def on_photo(m: bm.Message, state: bm.FSMContext, bot: bm.Bot) -> None:
    """Приём фото для GDN-кампании (§11): скачиваем, режем в 1.91:1 + 1:1, запускаем визард.
    Бинарь НЕ в proposal/логах — подготовленные кадры кладём во временное хранилище по media_id."""
    if not m.photo:  # фильтр F.photo гарантирует фото, но узкий гард снимает Optional и страхует
        return
    # §3-assets: фото в состоянии «жду изображение-ассет» → ветка image-extension (не GDN).
    if await state.get_state() == bm.ExtWizard.awaiting_image:
        await bm._ext_image_from_photo(m, state, bot)
        return
    # §19 Этап 4: фото в визарде «Создание кампании» → image-ассет в черновик (не GDN).
    if await state.get_state() == bm.CreateCampaignWizard.images:
        await bm._cc_image_from_photo(m, state, bot)
        return
    # §11: фото в визарде кампании из видео (шаг логотипа DG) → квадратный логотип (не GDN).
    if await state.get_state() == bm.VideoWizard.awaiting_logo:
        await bm._video_logo_from_photo(m, state, bot)
        return
    # §19.7.1: фото на шаге «Business logo» Этапа 5 → логотип-спек в набор ассетов черновика.
    if await state.get_state() == bm.CreateCampaignWizard.asset_logo:
        await bm._cc_asset_logo_from_photo(m, state, bot)
        return
    # 3I (инверсный гард): фото в ЛЮБОМ активном состоянии, кроме повторного фото GDN-брифа,
    # НЕ перехватывается GDN-веткой — она делает state.clear() и молча теряла ТЕКУЩИЙ флоу
    # (напр. фото во время ввода текста профиля §20 убивало сессию клиентов). Раньше белый
    # список покрывал только CreateCampaignWizard/VideoWizard.
    cur_state = await state.get_state()
    if cur_state is not None and cur_state != bm.GdnWizard.awaiting_brief.state:
        await m.answer(bm.i18n.t("photo_in_flow_hint"))
        return
    prev = await state.get_data()  # новое фото отменяет прежнее → чистим его медиа (без утечки)
    old_id = prev.get("gdn_media_id")
    if old_id:
        await bm.asyncio.to_thread(bm.clear_pending_media, old_id)
    await state.clear()
    try:
        buf = bm.io.BytesIO()
        await bot.download(m.photo[-1], destination=buf)  # самое большое разрешение
        landscape, square = await bm.asyncio.to_thread(bm.prepare_display_images, buf.getvalue())
    except Exception as e:  # сеть/битый файл/не картинка
        await m.answer(bm.i18n.t("err_photo", err=bm.ux.err_text(e)))
        return
    media_id = bm.uuid.uuid4().hex
    await bm.asyncio.to_thread(bm.save_pending_media, media_id, landscape, square)
    await state.update_data(gdn_media_id=media_id)
    await state.set_state(bm.GdnWizard.awaiting_brief)
    await bm.prompt_account_if_ambiguous(m, m.chat.id)  # AD.3: пикер аккаунта ДО брифа (см. §11)
    # «✖ Отмена» при выходе чистит pending-media (on_nav_cancel читает gdn_media_id из state).
    await m.answer(
        bm.i18n.t("gdn_ask_brief"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
    )


# ── §11: кампании из видео (Demand Gen / Video) — YouTube-ссылка → тип → бриф → гейт ─
_VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")


def _is_video_document(doc) -> bool:
    """Видео, присланное ФАЙЛОМ («отправить как файл», .mov с iPhone), приходит как document, а не
    video → раньше уезжало в txt/csv-ingest (fallback.on_document) и получало «пришли txt/csv».
    Тот же приём, что для изображения-документа в fallback: mime ИЛИ расширение имени."""
    if doc is None:
        return False
    mime = (getattr(doc, "mime_type", None) or "").lower()
    name = (getattr(doc, "file_name", None) or "").lower()
    return mime.startswith("video/") or name.endswith(_VIDEO_EXT)


@bm.dp.message(bm.F.video | bm.F.document.func(_is_video_document))
async def on_video(m: bm.Message, state: bm.FSMContext) -> None:
    """§11: приём видео (в т.ч. файлом) → визард кампании из видео. Загрузить файл в Google Ads
    напрямую нельзя — видео живёт на YouTube (примечание §11), поэтому просим ссылку. 3I (инверсный
    гард, как on_photo): видео в ЛЮБОМ активном флоу — подсказка, а не молчаливый state.clear()."""
    cur_state = await state.get_state()
    if cur_state is not None and cur_state != bm.VideoWizard.awaiting_link.state:
        await m.answer(bm.i18n.t("photo_in_flow_hint"))
        return
    await state.clear()
    await state.set_state(bm.VideoWizard.awaiting_link)
    await m.answer(
        bm.i18n.t("video_received"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
    )


@bm.dp.message(bm.Command("newvideo"))
async def newvideo_cmd(m: bm.Message, state: bm.FSMContext) -> None:
    """§11: кампания из видео без загрузки файла — сразу спрашиваем ссылку на YouTube."""
    await state.clear()
    await state.set_state(bm.VideoWizard.awaiting_link)
    await m.answer(
        bm.i18n.t("video_received"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
    )


@bm.dp.message(bm.VideoWizard.awaiting_link)
async def video_link(m: bm.Message, state: bm.FSMContext) -> None:
    """Ссылка на YouTube (или 11-символьный id) → выбор типа кампании (Demand Gen / Video)."""
    from ads.assets import parse_youtube_video_id

    vid = parse_youtube_video_id(m.text or "")
    if not vid:
        await m.answer(
            bm.i18n.t("video_bad_link"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
        return  # остаёмся в состоянии — пользователь пришлёт ссылку ещё раз (или «✖ Отмена»)
    await state.update_data(video_yt=vid)
    await m.answer(
        bm.i18n.t("video_pick_type", vid=vid),
        reply_markup=bm.video_type_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.VideoCB.filter(bm.F.action.in_({"dg", "video"})))
async def video_pick_type(
    cq: bm.CallbackQuery, callback_data: bm.VideoCB, state: bm.FSMContext
) -> None:
    """Тип выбран → просим бриф (тот же формат, что GDN: название | сайт | бюджет [| гео])."""
    data = await state.get_data()
    if not data.get("video_yt"):
        await cq.answer(bm.i18n.t("video_session_stale"), show_alert=True)
        await state.clear()
        return
    kind = callback_data.action
    # B4: Video через API возможен только для allowlist-аккаунтов Google (иначе mutate →
    # MUTATE_NOT_ALLOWED). Если владелец не включил его явно (GOOGLE_ADS_VIDEO_ENABLED), НЕ ведём
    # менеджера в гарантированный тупик после полного брифа — честно объясняем и продолжаем на
    # Demand Gen (тот же ролик, рабочий путь). Video-цепочка в коде готова: включить = один флаг.
    if kind == "video" and not bm.settings.google_ads_video_enabled:
        kind = "dg"
        await cq.answer(bm.i18n.t("video_disabled_use_dg"), show_alert=True)
    elif kind == "video":
        # Аккаунт заявлен как allowlisted (флаг включён) — предупреждаем о риске, но продолжаем Video.
        await cq.answer(bm.i18n.t("video_allowlist_warn"), show_alert=True)
    else:
        await cq.answer()
    await state.update_data(video_kind=kind)
    await state.set_state(bm.VideoWizard.awaiting_brief)
    msg = bm._cq_msg(cq)
    if msg is not None:
        await msg.answer(
            bm.i18n.t("video_ask_brief"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )


@bm.dp.message(bm.VideoWizard.awaiting_brief)
async def video_brief(m: bm.Message, state: bm.FSMContext) -> None:
    """Бриф → генерация текстов → (DG: шаг логотипа | Video: сразу confirm-гейт)."""
    data = await state.get_data()
    vid, kind = data.get("video_yt"), data.get("video_kind") or "dg"
    if not vid:
        await state.clear()
        await m.answer(bm.i18n.t("video_session_stale"))
        return
    parsed = bm._parse_gdn_brief(m.text or "")  # тот же формат «название | url | бюджет [| гео]»
    if parsed is None:
        await m.answer(
            bm.i18n.t("video_ask_brief"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
        return
    name, url, budget_units, geo_locations = parsed
    await m.answer(bm.i18n.t("gdn_generating"))
    prof = await bm._cc_profile_ctx_account(
        await bm._active_read_account(m.chat.id)
    )  # §20 контекст
    try:
        async with bm.ux.typing_action(m):  # 3I: живой индикатор на генерации
            draft = await bm._generate_rsa(
                bm.CopyBrief(topic=name, profile=(prof or None), n_headlines=5, n_descriptions=5)
            )
    except Exception as e:  # LLM/сеть
        await state.clear()
        await m.answer(bm.i18n.t("err_text_gen", err=bm.ux.err_text(e)))
        return
    # Video responsive: описания консервативно ≤70 (лимит КОДА, ads.mutations.VIDEO_DESCRIPTION_MAX);
    # длинный заголовок = первое подходящее описание (как GDN), без дубля в descriptions.
    from adcopy.validate import rsa_len

    all_desc = draft.descriptions[:5]
    if kind == "video":
        all_desc = [d for d in all_desc if rsa_len(d) <= bm.VIDEO_DESCRIPTION_MAX]
    if not draft.headlines or len(all_desc) < 2:
        await state.clear()
        await m.answer(bm.i18n.t("gdn_gen_empty"))
        return
    params = {
        "campaign_name": name,
        "youtube_video_id": vid,
        "headlines": draft.headlines[:5],
        "long_headline": draft.descriptions[0],  # ≤90 — валиден для обоих типов
        "descriptions": all_desc[1:] if all_desc[0] == draft.descriptions[0] else all_desc,
        "business_name": name[: bm.GDN_BUSINESS_NAME_MAX],
        "final_url": url,
        "budget_daily_micros": int(round(budget_units * 1_000_000)),
        "geo_locations": geo_locations,
    }
    if not params["descriptions"]:  # после фильтра ≤70 могло не остаться описаний
        await state.clear()
        await m.answer(bm.i18n.t("gdn_gen_empty"))
        return
    await state.update_data(video_params=params)
    if kind == "dg":  # DG: опциональный логотип (фото или «⏭ Пропустить»)
        await state.set_state(bm.VideoWizard.awaiting_logo)
        await m.answer(
            bm.i18n.t("video_ask_logo"),
            reply_markup=bm.video_logo_kb(),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    await bm._video_mint_proposal(m, m.chat.id, state, logo_media_id=None)


@bm.dp.callback_query(bm.VideoCB.filter(bm.F.action == "logo_skip"))
async def video_logo_skip(
    cq: bm.CallbackQuery, callback_data: bm.VideoCB, state: bm.FSMContext
) -> None:
    """C1 (live 2026-07): «Пропустить» логотип для DG убран — без logo_images Google отвергает
    create (ASSET_LINK TOO_FEW) с откатом всей структуры, т.е. кнопка вела к гарантированному
    отказу. Хендлер оставлен для СТАРЫХ кнопок в истории чата: объясняем, минтить не пытаемся."""
    await cq.answer(bm.i18n.t("video_logo_required"), show_alert=True)


@bm.dp.message(bm.GdnWizard.awaiting_brief)
async def gdn_brief(m: bm.Message, state: bm.FSMContext) -> None:
    data = await state.get_data()
    media_id = data.get("gdn_media_id")
    if not media_id:
        await state.clear()
        await m.answer(bm.i18n.t("gdn_session_stale"))
        return
    parsed = bm._parse_gdn_brief(m.text or "")
    if parsed is None:
        await m.answer(
            bm.i18n.t("gdn_bad_brief"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
        return  # остаёмся в состоянии — пользователь пришлёт бриф ещё раз (или «✖ Отмена»)
    name, url, budget_units, geo_locations = parsed
    await m.answer(bm.i18n.t("gdn_generating"))
    prof = await bm._cc_profile_ctx_account(
        await bm._active_read_account(m.chat.id)
    )  # §20 контекст
    try:
        async with bm.ux.typing_action(m):  # 3I: живой индикатор на генерации
            draft = await bm._generate_rsa(
                bm.CopyBrief(topic=name, profile=(prof or None), n_headlines=5, n_descriptions=4)
            )
    except Exception as e:  # LLM/сеть
        await bm._gdn_cleanup(state, media_id)
        await m.answer(bm.i18n.t("err_text_gen", err=bm.ux.err_text(e)))
        return
    # Нужно ≥1 заголовка и ≥2 описаний: первое описание идёт в long_headline, остальные — в
    # descriptions (иначе long_headline дублировал бы единственное описание).
    if not draft.headlines or len(draft.descriptions) < 2:
        await bm._gdn_cleanup(state, media_id)
        await m.answer(bm.i18n.t("gdn_gen_empty"))
        return
    headlines = draft.headlines[:5]
    all_desc = draft.descriptions[:5]
    long_headline = all_desc[0]  # ≤90, уже провалидировано генератором
    descriptions = all_desc[1:]  # ≥1 (len(all_desc) ≥ 2) — без дубля long_headline
    business_name = name[: bm.GDN_BUSINESS_NAME_MAX]
    budget_micros = int(round(budget_units * 1_000_000))
    params = {
        "campaign_name": name,
        "headlines": headlines,
        "long_headline": long_headline,
        "descriptions": descriptions,
        "business_name": business_name,
        "final_url": url,
        "budget_daily_micros": budget_micros,
        "media_id": media_id,
        "geo_locations": geo_locations,  # §11: опц. ГЕО (пусто ⇒ без гео)
    }
    try:  # defense-in-depth: схема ещё раз проверит длины/составы/URL/бюджет/media_id
        bm.SCHEMAS["create_gdn_campaign"](**params)
    except Exception as e:
        await bm._gdn_cleanup(state, media_id)
        await m.answer(bm.i18n.t("err_validate", err=bm.ux.humanize_validation(e)))
        return
    summary = bm.texts.fmt_gdn_proposal_summary(
        name, url, budget_units, headlines, descriptions, business_name, geo_locations
    )
    p = bm.Proposal(
        operation="create_gdn_campaign", summary=summary, params=params, chat_id=m.chat.id
    )
    await state.clear()
    await bm._present_proposal(
        m,
        chat_id=m.chat.id,
        operation="create_gdn_campaign",
        params=params,
        summary=summary,
        cid=p.confirmation_id,
        customer_id=await bm._active_read_account(m.chat.id),  # §8: создаём на активном аккаунте
    )
