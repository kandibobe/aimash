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
        from core.access import ensure_account_allowed_for_user

        await ensure_account_allowed_for_user(m.chat.id, cid)  # пер-юзер грант (2B, fail-closed)
    except PermissionError:
        await m.answer(
            bm.i18n.t("account_denied", cid=bm.texts.esc(cid)), parse_mode=bm.ParseMode.HTML
        )
        return
    await bm._save_selected_account(m.chat.id, cid)
    await m.answer(bm.i18n.t("account_set", cid=cid), parse_mode=bm.ParseMode.HTML)


# ── 2C: пер-пользовательские гранты аккаунтов (админ) + самодиагностика доступа ──
def _is_admin(chat_id: int) -> bool:
    """Админ бота (env ADMIN_CHAT_IDS). Пусто ⇒ никто (fail-closed, фича опциональна)."""
    return chat_id in bm.settings.admin_ids


@bm.dp.message(bm.Command("grant"))
async def grant_cmd(m: bm.Message, command: bm.CommandObject) -> None:
    """/grant <chat_id> <customer_id> — выдать оператору доступ к аккаунту ЧТЕНИЯ (только админ).
    cid обязан пройти глобальный read-замок (гранты только на реально читаемое). МУТАЦИИ гранты
    НЕ открывают (замок ensure_allowed/ALLOWED_CEILING отдельный, golden rule 9)."""
    if not _is_admin(m.chat.id):
        await m.answer(bm.i18n.t("admin_only"))
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].lstrip("-").isdigit():
        await m.answer(bm.i18n.t("grant_bad_args"), parse_mode=bm.ParseMode.HTML)
        return
    target_chat, raw_cid = int(parts[0]), parts[1]
    cid = bm.normalize_customer_id(raw_cid)
    try:
        bm.ensure_read_allowed(cid)  # гранты только на читаемое (fail-closed; подсказка /refresh)
    except PermissionError:
        await m.answer(
            bm.i18n.t("grant_unknown_account", cid=bm.texts.esc(cid or raw_cid)),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    from core.access import grant_account_access, per_user_enforcement_active

    was_enforced = await per_user_enforcement_active()
    await grant_account_access(target_chat, cid)
    note = "" if was_enforced else "\n" + bm.i18n.t("grant_enforcement_note")
    await m.answer(
        bm.i18n.t("grant_ok", chat=target_chat, cid=cid) + note, parse_mode=bm.ParseMode.HTML
    )


@bm.dp.message(bm.Command("revoke"))
async def revoke_cmd(m: bm.Message, command: bm.CommandObject) -> None:
    """/revoke <chat_id> <customer_id> — снять грант (только админ). Активный аккаунт оператора
    сам откатится на Draft при следующем чтении (get_active_account перепроверяет грант)."""
    if not _is_admin(m.chat.id):
        await m.answer(bm.i18n.t("admin_only"))
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].lstrip("-").isdigit():
        await m.answer(bm.i18n.t("grant_bad_args"), parse_mode=bm.ParseMode.HTML)
        return
    target_chat, cid = int(parts[0]), bm.normalize_customer_id(parts[1])
    from core.access import revoke_account_access

    await revoke_account_access(target_chat, cid)
    await m.answer(bm.i18n.t("revoke_ok", chat=target_chat, cid=cid), parse_mode=bm.ParseMode.HTML)


# ── P0-A: рантайм-управление операторами (whitelist env ∪ БД) — только админ ──
def _all_readable_ids() -> list[str]:
    """Все читаемые ботом аккаунты (кроме Draft — он доступен всем без гранта): мутационный набор ∪
    env read-list ∪ обнаруженные дочерние MCC. Для bulk-гранта «Все аккаунты» новому оператору."""
    from ads.client import discovered_read_children

    ids = (
        bm.settings.allowed_customer_ids
        | bm.settings.read_customer_ids
        | discovered_read_children()
    )
    return sorted(c for c in ids if c and c != bm.DRAFT_ACCOUNT_ID)


@bm.dp.message(bm.Command("adduser"))
async def adduser_cmd(m: bm.Message, command: bm.CommandObject) -> None:
    """/adduser <chat_id> [заметка] — открыть новому оператору доступ к боту БЕЗ рестарта (рантайм-
    whitelist, env ∪ БД). Затем inline-выбор объёма ЧТЕНИЯ (Все / Выбрать / только бот). Мутации НЕ
    открывает (замок ensure_allowed отдельный). Только админ (ADMIN_CHAT_IDS)."""
    if not _is_admin(m.chat.id):
        await m.answer(bm.i18n.t("admin_only"))
        return
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].lstrip("-").isdigit():
        await m.answer(bm.i18n.t("adduser_bad_args"), parse_mode=bm.ParseMode.HTML)
        return
    target = int(parts[0])
    note = parts[1].strip() if len(parts) > 1 else None
    from core.access import add_whitelisted_user

    added = await add_whitelisted_user(target, added_by=m.chat.id, note=note)
    key = "adduser_added" if added else "adduser_exists"
    await m.answer(
        bm.i18n.t(key, chat=target),
        reply_markup=bm.adduser_access_kb(target),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.Command("removeuser"))
async def removeuser_cmd(m: bm.Message, command: bm.CommandObject) -> None:
    """/removeuser <chat_id> — закрыть оператору доступ к боту (убрать из рантайм-whitelist + снять
    гранты аккаунтов). Env-операторов (.env) убрать нельзя — только правкой .env. Только админ."""
    if not _is_admin(m.chat.id):
        await m.answer(bm.i18n.t("admin_only"))
        return
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await m.answer(bm.i18n.t("removeuser_bad_args"), parse_mode=bm.ParseMode.HTML)
        return
    target = int(arg)
    if target in bm.settings.whitelist:  # env-оператор — из БД убирать нечего
        await m.answer(bm.i18n.t("removeuser_env", chat=target), parse_mode=bm.ParseMode.HTML)
        return
    from core.access import remove_whitelisted_user
    from db.models import AccountAccess
    from db.session import Session
    from sqlalchemy import delete as _sa_delete

    await remove_whitelisted_user(target)
    async with Session() as s:  # заодно снимаем все гранты аккаунтов этого оператора
        await s.execute(_sa_delete(AccountAccess).where(AccountAccess.chat_id == target))
        await s.commit()
    from core.access import _invalidate_enforcement_cache

    _invalidate_enforcement_cache()
    await m.answer(bm.i18n.t("removeuser_ok", chat=target), parse_mode=bm.ParseMode.HTML)


@bm.dp.message(bm.Command("users"))
async def users_cmd(m: bm.Message) -> None:
    """/users — операторы бота: env (.env, бутстрап) + БД (/adduser). Только админ."""
    if not _is_admin(m.chat.id):
        await m.answer(bm.i18n.t("admin_only"))
        return
    from core.access import list_whitelisted_users

    env_ids = sorted(bm.settings.whitelist)
    env_txt = ", ".join(f"<code>{i}</code>" for i in env_ids) or bm.i18n.t("users_empty_db")
    rows = await list_whitelisted_users()
    if rows:
        db_txt = "\n" + "\n".join(
            f"• <code>{r.chat_id}</code>"
            + (f" — {bm.texts.esc(r.note)}" if r.note else "")
            + (f" (by <code>{r.added_by}</code>)" if r.added_by else "")
            for r in rows
        )
    else:
        db_txt = bm.i18n.t("users_empty_db")
    await m.answer(bm.i18n.t("users_title", env=env_txt, db=db_txt), parse_mode=bm.ParseMode.HTML)


async def _render_adduser_pick(cq: bm.CallbackQuery, target: int, page: int) -> None:
    """Отрисовать пикер выбора аккаунтов для оператора (тап-тогл гранта чтения). Список — обнаруженные
    дочерние (meta), отметки ✅ — уже выданные гранты. Пусто ⇒ подсказка /refresh."""
    from ads.client import discovered_read_children_meta
    from core.access import list_user_account_ids

    meta = discovered_read_children_meta()
    accounts = sorted(
        ((cid, (ch.name or cid)) for cid, ch in meta.items() if cid != bm.DRAFT_ACCOUNT_ID),
        key=lambda t: t[1].casefold(),
    )
    if not accounts:
        await bm._safe_edit(cq, bm.i18n.t("adduser_pick_empty"))
        return
    granted = set(await list_user_account_ids(target))
    await bm._safe_edit(
        cq,
        bm.i18n.t("adduser_pick_title", chat=target),
        reply_markup=bm.adduser_pick_kb(target, accounts, granted, page=page),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.AdminCB.filter())
async def admin_access_cb(cq: bm.CallbackQuery, callback_data: bm.AdminCB) -> None:
    """P0-A: выбор объёма чтения после /adduser. Только админ. Выдаёт ТОЛЬКО гранты чтения
    (account_access) — мутации не открывает. all=грант всех дочерних; pick=пикер; grant=тогл одного;
    done=завершить."""
    if not _is_admin(cq.from_user.id):
        await cq.answer(bm.i18n.t("admin_only"), show_alert=True)
        return
    target = callback_data.chat
    action = callback_data.action
    from core.access import (
        grant_account_access,
        list_user_account_ids,
        revoke_account_access,
    )

    if action == "all":
        ids = _all_readable_ids()
        for cid in ids:
            await grant_account_access(target, cid)
        await bm._safe_edit(
            cq, bm.i18n.t("adduser_all_done", chat=target, n=len(ids)), parse_mode=bm.ParseMode.HTML
        )
        await cq.answer()
        return
    if action == "pick":
        page = 0
        if callback_data.cid.startswith("p") and callback_data.cid[1:].isdigit():
            page = int(callback_data.cid[1:])
        await _render_adduser_pick(cq, target, page)
        await cq.answer()
        return
    if action == "grant":
        cid = bm.normalize_customer_id(callback_data.cid)
        current = set(await list_user_account_ids(target))
        if cid in current:  # тогл: уже выдан → снять
            await revoke_account_access(target, cid)
        else:
            await grant_account_access(target, cid)
        await _render_adduser_pick(cq, target, 0)
        await cq.answer()
        return
    # done
    n = len([c for c in await list_user_account_ids(target) if c != bm.DRAFT_ACCOUNT_ID])
    key = "adduser_none_done" if n == 0 else "adduser_pick_done"
    await bm._safe_edit(cq, bm.i18n.t(key, chat=target, n=n), parse_mode=bm.ParseMode.HTML)
    await cq.answer()


@bm.dp.message(bm.Command("accounts"))
async def accounts_cmd(m: bm.Message) -> None:
    """/accounts — аккаунты, доступные ЭТОМУ оператору на чтение (граница = read-замок × грант),
    с именами из meta обхода MCC. Draft помечен."""
    rows = await bm._read_account_rows(m.chat.id)
    lines = []
    for r in rows:
        mark = " · Draft ✅" if bm.normalize_customer_id(r.id) == bm.DRAFT_ACCOUNT_ID else ""
        name = r.name if r.name and r.name != r.id else ""
        cur = f" ({r.currency})" if getattr(r, "currency", "") else ""
        lines.append(f"• <code>{bm.texts.esc(str(r.id))}</code> {bm.texts.esc(name)}{cur}{mark}")
    await m.answer(
        bm.i18n.t("accounts_title", n=len(rows)) + "\n" + "\n".join(lines),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.Command("whoami"))
async def whoami_cmd(m: bm.Message) -> None:
    """/whoami — chat_id, активный аккаунт чтения, режим пер-юзер изоляции, admin-флаг.
    Помогает узнать chat_id для /grant и понять, почему аккаунт не виден."""
    from core.access import per_user_enforcement_active

    active = await bm._active_read_account(m.chat.id)
    mode = (bm.settings.account_access_mode or "auto").strip().lower()
    enforced = await per_user_enforcement_active()
    await m.answer(
        bm.i18n.t(
            "whoami_text",
            chat=m.chat.id,
            active=active,
            mode=mode,
            enforced="✅" if enforced else "—",
            admin="✅" if _is_admin(m.chat.id) else "—",
        ),
        parse_mode=bm.ParseMode.HTML,
    )


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
    """§15: последние перехваченные ошибки (error_events) для триажа + кнопки (🔄 обновить /
    ⚠️ за сегодня / 🔍 подробнее). Только whitelisted (WhitelistMiddleware). Read-only;
    message/traceback уже редактированы (секретов нет). detail-кнопки — только админу."""
    try:
        rows = await bm._load_error_events(today=False)
    except Exception as e:  # noqa: BLE001 — диагностика не должна сама падать наружу
        await bm._capture_cmd_error(e, "cmd:diag")  # A2
        await m.answer(bm.i18n.t("err_journal", err=bm.ux.err_text(e)))
        return
    await m.answer(
        bm.texts.fmt_errors(rows),
        reply_markup=bm.diag_kb(rows, today=False, is_admin=_is_admin(m.chat.id)),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.callback_query(bm.DiagCB.filter())
async def on_diag_cb(cq: bm.CallbackQuery, callback_data: bm.DiagCB) -> None:
    """A3 (§15): кнопки под /diag. refresh/today/all — перечитать список и отредактировать сообщение
    (без дублей); detail — полный редактированный traceback инцидента (ТОЛЬКО админ). Read-only:
    БД/Ads не трогаем — только чтение error_events."""
    action = callback_data.action
    if action == "detail":
        if not _is_admin(cq.from_user.id):  # traceback — операционная деталь (диаг открыт всем WL)
            await cq.answer(bm.i18n.t("admin_only"), show_alert=True)
            return
        row = await bm._load_error_detail(callback_data.rid)
        await cq.answer()
        if row is None:
            await bm._safe_edit(cq, bm.i18n.t("diag_detail_not_found"))
            return
        await bm._safe_edit(cq, bm.texts.fmt_error_detail(row), parse_mode=bm.ParseMode.HTML)
        return
    today = action == "today"
    try:
        rows = await bm._load_error_events(today=today)
    except Exception:  # noqa: BLE001 — диагностика не должна падать наружу
        await cq.answer(bm.i18n.t("stale"), show_alert=True)
        return
    await cq.answer()
    await bm._safe_edit(
        cq,
        bm.texts.fmt_errors(rows),
        reply_markup=bm.diag_kb(rows, today=today, is_admin=_is_admin(cq.from_user.id)),
        parse_mode=bm.ParseMode.HTML,
    )


# ── 3E: «➕ Ещё» — inline-хаб вторичных флоу (обнаружимость без ручного ввода команд) ─
@bm.dp.message(bm.F.text.in_(bm.BTN_MORE_ALL))
async def btn_more(m: bm.Message) -> None:
    await m.answer(bm.i18n.t("more_menu_title"), reply_markup=bm.more_menu_kb())


@bm.dp.callback_query(bm.MoreCB.filter())
async def on_more(cq: bm.CallbackQuery, callback_data: bm.MoreCB, state: bm.FSMContext) -> None:
    """Маршрутизация хаба «Ещё» в ТЕ ЖЕ entry, что и слэш-команды (тела не дублируются).
    Кнопки только двигают UI — мутаций не создают."""
    await cq.answer()
    msg = bm._cq_msg(cq)
    if msg is None:
        return
    action = callback_data.action
    if action == "newsearch":
        await bm.newsearch_cmd(msg, state)
    elif action == "newvideo":
        await bm.newvideo_cmd(msg, state)
    elif action == "templates":
        await bm._send_templates(msg, bm._cq_chat_id(cq))
    elif action == "recent":
        await bm._send_recent(msg, bm._cq_chat_id(cq))
    elif action == "quota":
        await bm.quota_cmd(msg)
    elif action == "alerts":
        await bm.alerts_cmd(msg)  # 3H: настройка порогов аномалий
    elif action == "advise":
        await bm._advise_run(msg, bm._cq_chat_id(cq))  # 💡 рекомендации (advisory, read-only)


# ── advisor: /advise — рекомендации по улучшению активного аккаунта (advisory, read-only) ─
_ADVISE_TOPICS = {"optimize", "keywords", "rsa", "structure", "all"}


@bm.dp.message(bm.Command("advise"))
async def advise_cmd(m: bm.Message) -> None:
    """/advise [optimize|keywords|rsa|structure] — рекомендации по аккаунту (advisory). Без темы —
    все. НИЧЕГО не меняет — исполнение любого совета идёт отдельной командой через confirm-гейт."""
    parts = (m.text or "").split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    topic = arg if arg in _ADVISE_TOPICS else None
    await bm._advise_run(m, m.chat.id, topic=topic)


# ── 3H (M10): /alerts — настройка порогов аномалий per-chat (раньше только вставкой в БД) ─
@bm.dp.message(bm.Command("alerts"))
async def alerts_cmd(m: bm.Message) -> None:
    """Пороги алертов планировщика (spend_spike/conv_drop/min_spend) — НАСТРОЙКА БОТА
    (UserSettings.alert_thresholds), не Google Ads: confirm-гейт не нужен (как /lang)."""
    cur = await bm._load_alert_thresholds(m.chat.id)
    await m.answer(
        bm.texts.fmt_alerts(cur), reply_markup=bm.alerts_kb(cur), parse_mode=bm.ParseMode.HTML
    )


@bm.dp.callback_query(bm.AlertCB.filter())
async def on_alert_preset(
    cq: bm.CallbackQuery, callback_data: bm.AlertCB, state: bm.FSMContext
) -> None:
    chat_id = bm._cq_chat_id(cq)
    field, value = callback_data.field, callback_data.value
    if field == "reset":
        await bm._save_alert_threshold(chat_id, "", None)
        await cq.answer(bm.i18n.t("alerts_reset_done"))
        cur = await bm._load_alert_thresholds(chat_id)
        await bm._safe_edit(
            cq,
            bm.texts.fmt_alerts(cur),
            reply_markup=bm.alerts_kb(cur),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    key = bm._ALERT_FIELD_KEYS.get(field)
    if key is None:
        await cq.answer(bm.i18n.t("stale"), show_alert=True)
        return
    if value == "custom":  # ручной ввод числа через FSM
        await state.set_state(bm.AlertsWizard.awaiting_value)
        await state.update_data(alert_field=key)
        await cq.answer()
        msg = bm._cq_msg(cq)
        if msg is not None:
            await msg.answer(bm.i18n.t("alerts_ask_value"), reply_markup=bm.nav_kb())
        return
    try:
        num = float(value)
    except ValueError:
        await cq.answer(bm.i18n.t("stale"), show_alert=True)
        return
    if not bm._alert_value_ok(key, num):  # диапазон считает КОД
        await cq.answer(bm.i18n.t("alerts_bad_value"), show_alert=True)
        return
    await bm._save_alert_threshold(chat_id, key, num)
    await cq.answer(bm.i18n.t("alerts_saved"))
    cur = await bm._load_alert_thresholds(chat_id)
    await bm._safe_edit(
        cq, bm.texts.fmt_alerts(cur), reply_markup=bm.alerts_kb(cur), parse_mode=bm.ParseMode.HTML
    )


@bm.dp.message(bm.AlertsWizard.awaiting_value)
async def alerts_value(m: bm.Message, state: bm.FSMContext) -> None:
    """Ручной ввод порога («✏️»): float-парс + валидация КОДОМ; невалид → retry (state остаётся)."""
    data = await state.get_data()
    key = data.get("alert_field") or ""
    raw = (m.text or "").strip().replace(",", ".").rstrip("%")
    try:
        num = float(raw)
    except ValueError:
        await m.answer(bm.i18n.t("alerts_bad_value"), reply_markup=bm.nav_kb())
        return
    if not key or not bm._alert_value_ok(key, num):
        await m.answer(bm.i18n.t("alerts_bad_value"), reply_markup=bm.nav_kb())
        return
    await bm._save_alert_threshold(m.chat.id, key, num)
    await state.clear()
    cur = await bm._load_alert_thresholds(m.chat.id)
    await m.answer(bm.i18n.t("alerts_saved"))
    await m.answer(
        bm.texts.fmt_alerts(cur), reply_markup=bm.alerts_kb(cur), parse_mode=bm.ParseMode.HTML
    )
