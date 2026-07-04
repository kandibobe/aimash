"""advisor: обратная связь 👍/👎 на рекомендацию (AdviseCB) → advisor.store.record_feedback.

Хендлер пишет ТОЛЬКО в recommendation_feedback (Слой B — сигнал для experience) и НЕ трогает
Google Ads / НЕ создаёт proposal (инвариант tests/test_advisor.py). Позднее связывание bm.<name>
(как confirm_flow); порядок регистрации задаёт HANDLER_MODULES (до fallback/on_text).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.callback_query(bm.AdviseCB.filter())
async def on_advise_feedback(cq: bm.CallbackQuery, callback_data: bm.AdviseCB) -> None:
    """AdviseCB: 👍/👎 по рекомендации ИЛИ тумблер проактива (action='auto'). Оба — НАСТРОЙКИ БОТА,
    только локальная БД: ни мутаций Google Ads, ни proposal (инвариант test_advisor)."""
    chat_id = bm._cq_chat_id(cq)

    # Тумблер проактивной подачи (ui_prefs.advise_proactive) — как /alerts, confirm-гейт не нужен.
    if callback_data.action == "auto":
        want_on = callback_data.rec == "on"
        await bm._save_ui_pref(chat_id, "advise_proactive", "1" if want_on else "0")
        lang = bm.i18n.get_lang(chat_id)
        await cq.answer(bm.i18n.t("advise_auto_on" if want_on else "advise_auto_off"))
        try:  # обновить кнопку под текущее состояние
            await cq.message.edit_reply_markup(reply_markup=bm.advise_header_kb(want_on, lang))
        except Exception:  # noqa: BLE001 — косметика, не критично
            pass
        return

    actor_id, actor_username = bm._actor(cq)
    rating = "up" if callback_data.action == "up" else "down"
    from advisor import store as advisor_store

    try:
        await advisor_store.record_feedback(
            callback_data.rec,
            chat_id,
            rating,
            actor_user_id=actor_id,
            actor_username=actor_username,
        )
    except Exception:  # noqa: BLE001 — фидбек не критичен, UI не роняем
        bm.log.warning("advise feedback: не записан для %s", callback_data.rec)
    await cq.answer(bm.i18n.t("advise_feedback_thanks"))
