"""Базовые команды/кнопки меню (/start /help /status /model /lang /account /refresh /diag …)

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


# ── Команды ──────────────────────────────────────────────────────────────────────
@bm.dp.message(bm.CommandStart())
async def start(m: bm.Message) -> None:
    """Приветствие баннером (фото + подпись) + постоянное меню. Фолбэк на текст, если фото
    не отправилось (нет файла / сбой Telegram) — приветствие НЕ должно падать из-за картинки."""
    # (global _welcome_file_id → пишем через bm._welcome_file_id)
    kb = bm.main_menu()  # язык — из contextvar (LangMiddleware), как и текст START
    start_text = bm.i18n.t("start")
    if bm._welcome_file_id or bm.WELCOME_IMG.exists():
        photo = bm._welcome_file_id or bm.FSInputFile(bm.WELCOME_IMG)
        try:
            sent = await m.answer_photo(
                photo, caption=start_text, reply_markup=kb, parse_mode=bm.ParseMode.HTML
            )
            if sent.photo:  # кэшируем file_id — следующие /start без перезаливки PNG
                bm._welcome_file_id = sent.photo[-1].file_id
            await bm._offer_wizard_resume(m)  # §UX-память: продолжить незавершённый визард
            return
        except Exception as e:  # сеть/Telegram/битый файл — деградируем до текста, не падаем
            bm.log.warning("welcome-баннер не отправлен (%s) — шлю текст", type(e).__name__)
            bm._welcome_file_id = None
    await m.answer(start_text, reply_markup=kb, parse_mode=bm.ParseMode.HTML)
    await bm._offer_wizard_resume(m)  # §UX-память: продолжить незавершённый визард


@bm.dp.message(bm.Command("help"))
async def help_(m: bm.Message) -> None:
    await bm._send_help(m)


@bm.dp.message(bm.Command("status"))
async def status_(m: bm.Message) -> None:
    await bm._send_status(m)


@bm.dp.message(bm.Command("balance"))
async def balance_(m: bm.Message) -> None:
    """Бюджет ИИ: баланс OpenRouter + траты (read-only, без подтверждения)."""
    await bm._send_balance(m)


@bm.dp.message(bm.Command("journal"))
async def journal_(m: bm.Message) -> None:
    """ТЗ §12/§18: журнал изменений (что/когда/кто/результат). Read-only из audit_log."""
    await bm._send_journal(m)


@bm.dp.message(bm.Command("campaigns"))
async def campaigns_(m: bm.Message) -> None:
    """Read-only список кампаний с быстрыми действиями (точные имена нужны для ставки/ключей)."""
    await bm._send_campaigns(m, m.chat.id)


@bm.dp.message(bm.Command("cancel"))
async def cancel_cmd(m: bm.Message, state: bm.FSMContext) -> None:
    # B14: /cancel сворачивает АКТИВНЫЙ визард/сбор ввода (Создание кампании / Клиенты / KW / RSA), а
    # не только последний proposal — раньше в визарде отвечал «нет черновика» и оставлял юзера в FSM.
    if await bm._abandon_active_flow(m.chat.id, state):
        await m.answer(bm.i18n.t("wizard_cancelled"), reply_markup=bm.main_menu())
        return
    cid = bm._LAST_PENDING.get(m.chat.id)
    if not cid:
        await m.answer(bm.i18n.t("no_proposal"))
        return
    actor_id, actor_name = bm._actor(m)
    await bm.STORE.reject(cid, chat_id=m.chat.id, actor_user_id=actor_id, actor_username=actor_name)
    bm._LAST_PENDING.pop(m.chat.id, None)
    await m.answer(bm.i18n.t("rejected"))


@bm.dp.message(bm.Command("lang", "language"))
async def lang_cmd(m: bm.Message, command: bm.CommandObject) -> None:
    """Язык интерфейса RU/EN. С аргументом (/lang en) — сразу; иначе кнопки выбора.
    /language — тихий алиас (ТЗ §6 называет команду так; в меню — /lang)."""
    arg = (command.args or "").strip().lower()
    if arg in bm.i18n.LANGS:
        lang = bm.i18n.set_lang(m.chat.id, arg)  # кэш
        bm.i18n.set_current_lang(lang)  # ответ ниже сразу на выбранном языке (для текущего апдейта)
        await bm.i18n.save_lang(m.chat.id, lang)  # персист — переживает рестарт (§4)
        await m.answer(bm.i18n.t("lang_set", lang))
        return
    await m.answer(bm.i18n.t("lang_pick"), reply_markup=bm.lang_kb())


@bm.dp.callback_query(bm.LangCB.filter())
async def on_lang(cq: bm.CallbackQuery, callback_data: bm.LangCB) -> None:
    chat_id = bm._cq_chat_id(cq)
    lang = bm.i18n.set_lang(chat_id, callback_data.code)  # кэш
    bm.i18n.set_current_lang(lang)  # ответ ниже сразу на выбранном языке (для текущего апдейта)
    await bm.i18n.save_lang(chat_id, lang)  # персист — переживает рестарт (§4)
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    try:
        await msg.edit_text(bm.i18n.t("lang_set", lang))
    except bm.TelegramBadRequest:
        pass


@bm.dp.message(bm.Command("model"))
async def model_cmd(m: bm.Message, command: bm.CommandObject, state: bm.FSMContext) -> None:
    """Выбор модели ИИ (OpenRouter). С аргументом (/model vendor/slug) — сразу; иначе меню."""
    await state.clear()
    arg = (command.args or "").strip()
    if arg:
        slug = bm._valid_model_slug(arg)
        if not slug:
            await m.answer(bm.i18n.t("model_bad"), parse_mode=bm.ParseMode.HTML)
            return
        await bm._persist_and_set_model(slug)
        await m.answer(
            bm.i18n.t("model_set", model=bm.texts.esc(slug)), parse_mode=bm.ParseMode.HTML
        )
        return
    await m.answer(
        bm.texts.fmt_model_menu(
            bm.router.get_active_model(),
            bm.router.effective_model("parsing"),
            bm.router.effective_model("copy"),
        ),
        reply_markup=bm.model_kb(
            bm.router.MODEL_CHOICES, bm.router.get_active_model(), bm.router.MODEL_LABELS
        ),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.ModelCB.filter(bm.F.action == "set"))
async def on_model_set(cq: bm.CallbackQuery, callback_data: bm.ModelCB) -> None:
    choices = bm.router.MODEL_CHOICES
    if callback_data.idx < 0 or callback_data.idx >= len(choices):
        await cq.answer(bm.i18n.t("model_list_stale"), show_alert=True)
        return
    slug = choices[callback_data.idx]
    await bm._persist_and_set_model(slug)
    await cq.answer(bm.i18n.t("cb_done"))
    await bm._safe_edit(
        cq, bm.i18n.t("model_set", model=bm.texts.esc(slug)), parse_mode=bm.ParseMode.HTML
    )


@bm.dp.callback_query(bm.ModelCB.filter(bm.F.action == "reset"))
async def on_model_reset(cq: bm.CallbackQuery) -> None:
    await bm._persist_and_set_model(None)
    await cq.answer(bm.i18n.t("cb_reset"))
    await bm._safe_edit(
        cq,
        bm.i18n.t("model_reset", model=bm.texts.esc(bm.router.effective_model("parsing"))),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.ModelCB.filter(bm.F.action == "custom"))
async def on_model_custom(cq: bm.CallbackQuery, state: bm.FSMContext) -> None:
    await state.set_state(bm.ModelWizard.awaiting_model)
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is not None:
        await msg.answer(
            bm.i18n.t("model_ask_custom"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )


@bm.dp.message(bm.Command("pause"))
async def pause_(m: bm.Message, command: bm.CommandObject) -> None:
    """ТЗ §6: /pause <кампания> — поставить на паузу (через confirm-гейт)."""
    await bm._slash_mutate(m, command, "pause_campaign")


@bm.dp.message(bm.Command("resume"))
async def resume_(m: bm.Message, command: bm.CommandObject) -> None:
    """ТЗ §6: /resume <кампания> — возобновить (через confirm-гейт)."""
    await bm._slash_mutate(m, command, "resume_campaign")


@bm.dp.message(bm.Command("account"))
async def account_cmd(m: bm.Message, command: bm.CommandObject) -> None:
    """ТЗ §6 /account <id>: выбрать аккаунт ЧТЕНИЯ для /status /report /export /sheets (per-chat,
    переживает рестарт). Гейт — ensure_read_allowed (fail-closed). МУТАЦИИ не затрагиваются:
    они всегда идут на Draft (ensure_allowed, golden rule 9)."""
    arg = (command.args or "").strip()
    if not arg:
        cur = await bm._active_read_account(m.chat.id)
        draft_mark = " (Draft)" if cur == bm.DRAFT_ACCOUNT_ID else ""
        await m.answer(
            bm.i18n.t("account_current", cid=cur, draft=draft_mark), parse_mode=bm.ParseMode.HTML
        )
        return
    if arg.lower() in ("reset", "draft", "сброс"):
        await bm._save_selected_account(m.chat.id, None)
        await m.answer(bm.i18n.t("account_reset"))
        return
    cid = bm.normalize_customer_id(arg)
    try:
        bm.ensure_read_allowed(cid)
    except PermissionError:
        await m.answer(
            bm.i18n.t("account_denied", cid=bm.texts.esc(cid)), parse_mode=bm.ParseMode.HTML
        )
        return
    await bm._save_selected_account(m.chat.id, cid)
    await m.answer(bm.i18n.t("account_set", cid=cid), parse_mode=bm.ParseMode.HTML)


@bm.dp.message(bm.Command("refresh"))
async def refresh_(m: bm.Message) -> None:
    """§8: обновить данные БЕЗ рестарта бота. Пере-обход дочерних MCC (список аккаунтов для пикера +
    имена/валюты) и сброс кэшей: SDK-клиенты (подхватить ставший доступным аккаунт), валюты/таймзоны.
    Нужно после изменения аккаунтов/доступов в Google Ads или правки GOOGLE_ADS_READ_CUSTOMER_IDS."""
    from ads.client import clear_client_cache, discover_read_children
    from ads.read import clear_read_caches

    await m.answer(bm.i18n.t("refresh_working"))
    try:
        clear_client_cache()  # пересобрать клиентов (новый токен/ставший доступным аккаунт)
        clear_read_caches()  # сбросить кэш валют/таймзон (могли закэшироваться пустыми)
        async with bm.ux.typing_action(m):
            n = await discover_read_children()  # пере-обнаружить дочерние MCC (read-list + meta)
    except Exception as e:  # сеть/доступ/SDK
        await m.answer(bm.i18n.t("err_refresh", err=bm.ux.err_text(e)))
        return
    await m.answer(bm.i18n.t("refresh_done", n=n), parse_mode=bm.ParseMode.HTML)


@bm.dp.message(bm.Command("quota"))
async def quota_cmd(m: bm.Message) -> None:
    """§3/§15: дневная квота операций Google Ads API (Basic 15 000/сутки) — срез счётчика.
    Read-only; сам гейт (warn 80% / блок мутаций 95%) живёт в core.quota."""
    from core import quota as q

    snap = q.snapshot()
    lim, used = snap["limit"], snap["used"]
    pct = f"{snap['pct'] * 100:.0f}%"
    per = "\n".join(
        f"  • <code>{bm.texts.esc(str(a))}</code>: {n}"
        for a, n in sorted(snap["by_account"].items(), key=lambda kv: -kv[1])[:10]
    )
    if bm.i18n.current_lang() == "en":
        body = (
            f"📊 <b>Google Ads API daily quota</b> (window {snap['window_hours']}h)\n"
            f"Used: {used} / {lim if lim > 0 else '∞'} ({pct})\n"
            "Mutations are blocked at 95% (reads are never blocked)."
        )
    else:
        body = (
            f"📊 <b>Дневная квота Google Ads API</b> (окно {snap['window_hours']}ч)\n"
            f"Израсходовано: {used} / {lim if lim > 0 else '∞'} ({pct})\n"
            "Мутации блокируются на 95% (чтение не блокируется)."
        )
    if per:
        body += ("\nBy account:\n" if bm.i18n.current_lang() == "en" else "\nПо аккаунтам:\n") + per
    await m.answer(body, parse_mode=bm.ParseMode.HTML)


# ── Reply-кнопки (ОБЯЗАТЕЛЬНО до общего F.text-хендлера — иначе перехватит on_text) ─
# Матчим по МНОЖЕСТВУ языковых подписей (F.text.in_(BTN_*_ALL)), а не по одному RU-литералу:
# EN-пользователь шлёт EN-подпись, и `== BTN_*` (RU) её бы не поймал → «мёртвая» кнопка (§4).
@bm.dp.message(bm.F.text.in_(bm.BTN_STATUS_ALL))
async def btn_status(m: bm.Message) -> None:
    await bm._send_status(m)


@bm.dp.message(bm.F.text.in_(bm.BTN_CAMPAIGNS_ALL))
async def btn_campaigns(m: bm.Message) -> None:
    await bm._send_campaigns(m, m.chat.id)


@bm.dp.message(bm.F.text.in_(bm.BTN_BALANCE_ALL))
async def btn_balance(m: bm.Message) -> None:
    await bm._send_balance(m)


@bm.dp.message(bm.F.text.in_(bm.BTN_JOURNAL_ALL))
async def btn_journal(m: bm.Message) -> None:
    await bm._send_journal(m)


@bm.dp.message(bm.F.text.in_(bm.BTN_HELP_ALL))
async def btn_help(m: bm.Message) -> None:
    await bm._send_help(m)


@bm.dp.message(bm.F.text.in_(bm.BTN_MODEL_ALL))
async def btn_model(m: bm.Message, state: bm.FSMContext) -> None:
    """Кнопка «Модель» = /model без аргументов: меню выбора модели ИИ."""
    await state.clear()
    await m.answer(
        bm.texts.fmt_model_menu(
            bm.router.get_active_model(),
            bm.router.effective_model("parsing"),
            bm.router.effective_model("copy"),
        ),
        reply_markup=bm.model_kb(
            bm.router.MODEL_CHOICES, bm.router.get_active_model(), bm.router.MODEL_LABELS
        ),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.F.text.in_(bm.BTN_LANG_ALL))
async def btn_lang(m: bm.Message) -> None:
    """Кнопка «Язык» = /lang без аргументов: выбор языка интерфейса."""
    await m.answer(bm.i18n.t("lang_pick"), reply_markup=bm.lang_kb())


@bm.dp.callback_query(bm.NavCB.filter(bm.F.action == "cancel"))
async def on_nav_cancel(cq: bm.CallbackQuery, state: bm.FSMContext) -> None:
    """Универсальная «✖ Отмена» любого мастера: очистить FSM + вернуть главное меню. НИЧЕГО не
    мутирует и не трогает _LAST_PENDING (черновика тут нет — это лишь выход из сбора ввода). Старую
    inline-подсказку правим в «отменено», а reply-меню шлём НОВЫМ сообщением: ReplyKeyboardMarkup
    нельзя прицепить через edit_text (только новое сообщение)."""
    # Общий свёрт активного визарда (abandon черновика §19 + чистка временных медиа/буферов/контекста).
    await bm._abandon_active_flow(bm._cq_chat_id(cq), state)
    await cq.answer(bm.i18n.t("cb_cancelled"))
    await bm._safe_edit(cq, bm.i18n.t("wizard_cancelled"))
    msg = bm._cq_msg(cq)
    if msg is not None:
        await msg.answer(bm.i18n.t("main_menu_back"), reply_markup=bm.main_menu())


@bm.dp.message(bm.ModelWizard.awaiting_model)
async def model_custom_text(m: bm.Message, state: bm.FSMContext) -> None:
    """Своя модель из /model → ✏️ Своя модель. Валидируем slug, применяем + персистим."""
    slug = bm._valid_model_slug(m.text or "")
    if not slug:
        await m.answer(
            bm.i18n.t("model_bad"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
        return  # остаёмся в состоянии — пользователь пришлёт slug ещё раз (или «✖ Отмена»)
    await state.clear()
    await bm._persist_and_set_model(slug)
    await m.answer(bm.i18n.t("model_set", model=bm.texts.esc(slug)), parse_mode=bm.ParseMode.HTML)


@bm.dp.message(bm.Command("diag"))
async def diag(m: bm.Message) -> None:
    """§15: последние перехваченные ошибки (error_events) для триажа. Только whitelisted
    (WhitelistMiddleware). Read-only; message/traceback уже редактированы (секретов нет)."""
    from sqlalchemy import desc, select

    from db.models import ErrorEvent as DBErrorEvent
    from db.session import Session

    try:
        async with Session() as s:
            rows = (
                (
                    await s.execute(
                        select(DBErrorEvent).order_by(desc(DBErrorEvent.created_at)).limit(10)
                    )
                )
                .scalars()
                .all()
            )
        await m.answer(bm.texts.fmt_errors(rows), parse_mode=bm.ParseMode.HTML)
    except Exception as e:  # noqa: BLE001 — диагностика не должна сама падать наружу
        await m.answer(bm.i18n.t("err_journal", err=bm.ux.err_text(e)))
