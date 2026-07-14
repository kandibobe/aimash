"""Confirm-гейт: ✅/❌ (ConfirmCB + легаси ok:/no:) → bm._do_confirm/_do_cancel

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.callback_query(bm.ConfirmCB.filter(bm.F.action == "ok"))
async def on_confirm(
    cq: bm.CallbackQuery, callback_data: bm.ConfirmCB, state: bm.FSMContext | None = None
) -> None:
    # state прокидываем для §12 2FA: опасная op → _do_confirm отложит исполнение и запросит PIN.
    # Дефолт None — прямой вызов в тестах (aiogram сам инжектит FSMContext по имени параметра).
    await bm._do_confirm(cq, callback_data.cid, state=state)


@bm.dp.callback_query(bm.ConfirmCB.filter(bm.F.action == "del1"))
async def on_confirm_stage1(cq: bm.CallbackQuery, callback_data: bm.ConfirmCB) -> None:
    """P1-6: первый шаг двойного подтверждения удаления (необратимо) — показать финальную кнопку."""
    await bm._do_confirm_stage1(cq, callback_data.cid)


@bm.dp.callback_query(bm.ConfirmCB.filter(bm.F.action == "no"))
async def on_cancel(cq: bm.CallbackQuery, callback_data: bm.ConfirmCB) -> None:
    await bm._do_cancel(cq, callback_data.cid)


# ── D2: «↩️ Откатить» применённую обратимую операцию → минт ОБРАТНОГО черновика ─────
@bm.dp.callback_query(bm.RollbackCB.filter())
async def on_rollback(cq: bm.CallbackQuery, callback_data: bm.RollbackCB) -> None:
    """Клик «↩️ Откатить» → собрать обратную операцию и показать её как ОБЫЧНЫЙ черновик
    (confirm-гейт ✅/❌; НЕ исполняем сразу). Одноразово: кэш снят, повторный клик → stale.
    _present_proposal сам ставит user_initiated=True и заново проходит ensure_allowed — бюджет-
    откат легитимен (правило 3), мутационный замок сохранён."""
    chat_id = bm._cq_chat_id(cq)
    spec = bm._ROLLBACK_CACHE.get(chat_id)
    if not spec or spec.get("token") != callback_data.token:
        await cq.answer(bm.i18n.t("rollback_stale"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    bm._ROLLBACK_CACHE.pop(chat_id, None)  # одноразово (следующий applied положит новый)
    await cq.answer()
    try:
        cid, operation, params, summary = bm._build_proposal(spec["operation"], **spec["params"])
    except Exception as e:  # noqa: BLE001 — валидация обратных params (маловероятно) → внятно
        await msg.answer(bm.i18n.t("rollback_failed", err=bm.ux.err_text(e)))
        return
    await bm._present_proposal(
        msg,
        chat_id=chat_id,
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=spec.get("customer_id"),
    )


# ── Доп.2B: «↩️ Откатить» из /journal — ПЕРСИСТЕНТНЫЙ откат (снимок из БД, не in-memory) ──
@bm.dp.callback_query(bm.JournalRollbackCB.filter())
async def on_journal_rollback(cq: bm.CallbackQuery, callback_data: bm.JournalRollbackCB) -> None:
    """Клик «↩️ Откатить» в /journal → снимок черновика берём из БД по confirmation_id (переживает
    рестарт, в отличие от in-memory _ROLLBACK_CACHE), строим ОБРАТНУЮ операцию из params[_before] и
    показываем ОБЫЧНЫМ черновиком (confirm-гейт ✅/❌; НЕ исполняем сразу).

    Fail-closed на клике: чужой чат / не applied / не в _ROLLBACKABLE_OPS → generic-«устарело» (не
    выдаём существование чужой строки). Реверс необратим (нет _before/неоднозначно) → внятный отказ.
    _present_proposal сам ставит user_initiated=True и заново проходит ensure_allowed — бюджет-откат
    легитимен (правило 3), мутационный замок сохранён."""
    chat_id = bm._cq_chat_id(cq)
    snap = await bm.STORE.get_confirmed(callback_data.cid)
    if (
        snap is None
        or snap.chat_id != chat_id  # владение: откатить можно ТОЛЬКО свою строку
        or snap.status != "applied"  # откатываем лишь применённое
        or snap.operation not in bm._ROLLBACKABLE_OPS  # allow-list обратимых
    ):
        await cq.answer(bm.i18n.t("rollback_stale"), show_alert=True)
        return
    rev = bm._reverse_spec(snap.operation, snap.params or {}, (snap.params or {}).get("_before"))
    if rev is None:  # снимка _before не хватает / реверс неоднозначен — честно не откатываем
        await cq.answer(bm.i18n.t("rollback_not_reversible"), show_alert=True)
        return
    msg = bm._cq_msg(cq)
    if msg is None:
        await cq.answer()
        return
    await cq.answer()
    try:
        cid, operation, params, summary = bm._build_proposal(rev[0], **rev[1])
    except Exception as e:  # noqa: BLE001 — валидация обратных params (маловероятно) → внятно
        await msg.answer(bm.i18n.t("rollback_failed", err=bm.ux.err_text(e)))
        return
    await bm._present_proposal(
        msg,
        chat_id=chat_id,
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=snap.customer_id,  # откат на ТОМ ЖЕ аккаунте (execute заново пройдёт ensure_allowed)
    )


# Legacy-fallback: старые сообщения с "ok:/no:" (до рестарта). После переходного периода удалить.
@bm.dp.callback_query(bm.F.data.startswith("ok:"))
async def on_confirm_legacy(cq: bm.CallbackQuery, state: bm.FSMContext | None = None) -> None:
    await bm._do_confirm(cq, (cq.data or "")[3:], state=state)  # state → §12 2FA-гейт


@bm.dp.callback_query(bm.F.data.startswith("no:"))
async def on_cancel_legacy(cq: bm.CallbackQuery) -> None:
    await bm._do_cancel(cq, (cq.data or "")[3:])


# ── §12 2FA: приём PIN-кода перед исполнением опасной операции ─────────────────────
@bm.dp.message(bm.TwoFactor.awaiting_code)
async def on_twofa_code(m: bm.Message, state: bm.FSMContext) -> None:
    """Верный код → исполняем (re-enter _do_confirm, twofa_ok=True, на ИСХОДНОМ ✅-сообщении);
    неверный (до N) → повтор; N-й/отмена → черновик остаётся `pending` (повторить ✅ можно).
    Сообщение с кодом пытаемся удалить (гигиена: PIN не остаётся в истории; в привате бот не
    может удалить чужое сообщение → best-effort). Ключ = chat_id, как весь pending-стейт бота."""
    chat_id = m.chat.id
    waiting = bm._TWOFA_PENDING.get(chat_id)
    code = (m.text or "").strip()
    try:
        await m.delete()  # best-effort: не храним PIN в чате (в привате может не сработать)
    except Exception:  # noqa: BLE001 — удаление не критично, не роняем приём кода
        pass
    if waiting is None:  # состояние осиротело (рестарт/гонка) — выходим чисто
        await state.clear()
        await m.answer(bm.i18n.t("twofa_stale"))
        return
    if code.lower() in ("/cancel", "cancel", "отмена"):  # отмена — черновик остаётся pending
        bm._TWOFA_PENDING.pop(chat_id, None)
        await state.clear()
        await m.answer(bm.i18n.t("twofa_aborted"))
        return
    if not bm.twofa.verify(code):
        # A14: неудачи копятся в ПЕРСИСТЕНТНОМ счётчике (bm._TWOFA_FAILS), а не в waiting — новый ✅
        # больше не обнуляет лимит. Порог достигнут → кулдаун (fail-closed) + алерт админам.
        fails, lock_s = bm._twofa_register_fail(chat_id)
        if lock_s > 0:  # локаут — сбрасываем ожидание (черновик НЕ сжигаем), уведомляем админов
            bm._TWOFA_PENDING.pop(chat_id, None)
            await state.clear()
            lock_min = max(1, round(lock_s / 60))
            await m.answer(bm.i18n.t("twofa_too_many", min=lock_min))
            if m.bot is not None:
                await bm._notify_admins_twofa_lockout(m.bot, chat_id, lock_min)
            return
        await m.answer(bm.i18n.t("twofa_wrong", left=bm._TWOFA_MAX_ATTEMPTS - fails))
        return
    # Верный код: сбрасываем счётчик неудач/локаутов, очищаем ожидание/состояние и исполняем на
    # ИСХОДНОМ CallbackQuery (минуя гейт).
    bm._twofa_reset_fails(chat_id)
    bm._TWOFA_PENDING.pop(chat_id, None)
    await state.clear()
    await bm._do_confirm(waiting["cq"], waiting["cid"], state=state, twofa_ok=True)
