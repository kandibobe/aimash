"""3A: гард кнопок главного меню при АКТИВНОМ визарде — кнопки всегда работают.

Проблема (предсдаточный аудит): текстовые state-хендлеры визардов (cc_settings_desc,
cli_accumulate_text, gdn_brief, kw_seeds…) матчат ЛЮБОЙ F.text в своём состоянии, поэтому
подпись reply-кнопки «съедалась» как ввод, если хендлер кнопки жил в позже импортируемом
модуле: «📊 Статистика» (commands, первый) работала, «ℹ️ Клиенты» (clients_kb, последний)
уходила в LLM как описание кампании. Клавиатура is_persistent — тап во время визарда легален.

Решение: ЭТОТ модуль импортируется ПЕРВЫМ в хвосте bot/main.py → один гард-хендлер стоит
раньше всех state-хендлеров. Фильтр `~StateFilter(None)` — гард матчится ТОЛЬКО при активном
FSM; «мирный» путь (state=None) идёт к обычным btn_*-хендлерам как раньше (нулевой риск).
Работа не теряется (_suspend_active_flow_soft): черновик §19 остаётся active («▶️ Продолжить»),
буфер §20 сбрасывается в confirm-черновик, лёгкие состояния закрываются. Диалог «выйти из
визарда?» отвергнут: при lossless-suspension цена мисс-клика ≈ 0, а подписи кнопок различимы.

Имена из bot.main — через bm.<name> (позднее связывание, monkeypatch тестов работает).
"""

from __future__ import annotations

from aiogram.filters import StateFilter

import bot.main as bm


async def _dispatch_menu_button(m: bm.Message, state: bm.FSMContext) -> None:
    """Развести подпись кнопки в ТОТ ЖЕ entry, что и обычный btn_*-хендлер (переиспользуем их
    как функции через bm — тела не дублируются). state уже очищен _suspend_active_flow_soft."""
    text = m.text or ""
    if text in bm.BTN_NEWCAMPAIGN_ALL:
        await bm.btn_newcampaign(m, state)
    elif text in bm.BTN_CLIENTS_ALL:
        await bm.btn_clients(m, state)
    elif text in bm.BTN_STATUS_ALL:
        await bm.btn_status(m)
    elif text in bm.BTN_CAMPAIGNS_ALL:
        await bm.btn_campaigns(m)
    elif text in bm.BTN_AUDIT_ALL:
        await bm.btn_audit(m, state)
    elif text in bm.BTN_ADVISE_ALL:
        await bm.btn_advise(m)
    elif text in bm.BTN_BIDS_ALL:
        await bm.btn_bids(m)
    elif text in bm.BTN_CREATE_ALL:
        await bm.btn_create(m)
    elif text in bm.BTN_REPORTS_ALL:
        await bm.btn_reports(m)
    elif text in bm.BTN_REPORT_ALL:
        await bm.btn_report(m)
    elif text in bm.BTN_EXPORT_ALL:
        await bm.btn_export(m)
    elif text in bm.BTN_SHEETS_ALL:
        await bm.btn_sheets(m)
    elif text in bm.BTN_MCC_ALL:
        await bm.btn_mcc(m)
    elif text in bm.BTN_KEYWORDS_ALL:
        await bm.btn_keywords(m, state)
    elif text in bm.BTN_RSA_ALL:
        await bm.btn_rsa(m, state)
    elif text in bm.BTN_MODEL_ALL:
        await bm.btn_model(m, state)
    elif text in bm.BTN_BALANCE_ALL:
        await bm.btn_balance(m)
    elif text in bm.BTN_JOURNAL_ALL:
        await bm.btn_journal(m)
    elif text in bm.BTN_LANG_ALL:
        await bm.btn_lang(m)
    elif text in bm.BTN_HELP_ALL:
        await bm.btn_help(m)
    elif text in bm.BTN_MORE_ALL:
        await bm.btn_more(m)


@bm.dp.message(StateFilter("*"), bm.F.text.in_(bm.ALL_MENU_BUTTONS))
async def btn_guard_menu(m: bm.Message, state: bm.FSMContext) -> None:
    """Кнопка главного меню во время активного FSM: мягко свернуть флоу (без потери работы)
    и выполнить кнопку. StateFilter("*") матчит ЛЮБОЕ состояние; при state=None этот хендлер
    тоже сработает — тогда suspend просто no-op (нет данных/состояния) и кнопка выполняется
    как обычно (эквивалентно прямому btn_*; их регистрация ниже остаётся как фолбэк-документация)."""
    cur = await state.get_state()
    if cur is None:
        # мирный путь: состояния нет — сразу к entry (suspend незачем)
        await _dispatch_menu_button(m, state)
        return
    cc_step, cli_flushed = await bm._suspend_active_flow_soft(m, state)
    if cc_step is not None:
        await m.answer(bm.i18n.t("cc_wizard_suspended", step=max(1, min(int(cc_step), 7))))
    if cli_flushed:
        await m.answer(bm.i18n.t("cli_buf_flushed"))
    await _dispatch_menu_button(m, state)
