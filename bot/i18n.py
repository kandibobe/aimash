"""Каркас локализации RU/EN (ТЗ §4). Инкрементальная миграция: bot/texts.py продолжает
работать как есть; t() берёт перевод из CATALOG, а для НЕ-мигрированных ключей мостит к
texts.<KEY>. Так можно переводить сообщения по одному, не ломая RU.

Язык запроса — contextvar (_LANG), который ставит LangMiddleware на каждый апдейт по chat_id,
чтобы форматтеры (bot.texts.fmt_*) сами брали язык без проброса lang через ~80 call-site'ов.
Дефолт RU; t()/форматтеры без явного lang читают current_lang().

Хранилище языка — in-memory кэш (_CHAT_LANG) + персист в user_settings.language (load_langs на
старте, save_lang после смены): выбор переживает рестарт. get_lang/set_lang — синхронные (кэш),
а save_lang — async-upsert (как _save_model_override в bot.main).
"""

from __future__ import annotations

import contextvars

from bot import texts

LANGS = ("ru", "en")
DEFAULT_LANG = "ru"

# Язык ТЕКУЩЕГО запроса. Ставит LangMiddleware (set_current_lang) перед хендлером, сбрасывает в
# finally (reset_current_lang). Дефолт RU → код вне хендлера (тесты, прогрев) видит RU без настройки.
_LANG: contextvars.ContextVar[str] = contextvars.ContextVar("aimash_lang", default=DEFAULT_LANG)

# Мигрированные сообщения. Ключ (lowercase имени константы в texts.py) → {lang: текст}.
# RU-сторона ССЫЛАЕТСЯ на texts.<KEY> (а не дублирует литерал) — чтобы каталог не дрейфовал от
# исходных RU-строк и существующие тесты на RU оставались зелёными. EN — чисто аддитивно;
# плейсхолдеры {...} и HTML-теги В ТОЧНОСТИ совпадают с RU-версией.
CATALOG: dict[str, dict[str, str]] = {
    # — мелкие статусные (исторически тут литералами; оставляем как есть, RU байт-в-байт) —
    "executing": {"ru": "⏳ Выполняю…", "en": "⏳ Working…"},
    "rejected": {"ru": "❌ Отменено", "en": "❌ Cancelled"},
    "stale": {"ru": "Черновик не найден или устарел", "en": "Draft not found or expired."},
    "no_proposal": {
        "ru": "Нет активного черновика для отмены.",
        "en": "No active draft to cancel.",
    },
    "lang_pick": {"ru": "🌐 Выбери язык интерфейса:", "en": "🌐 Choose interface language:"},
    "lang_set": {"ru": "🌐 Язык интерфейса: русский.", "en": "🌐 Interface language: English."},
    # — выбор периода под reply-кнопками (раньше литералами в bot.main) —
    "period_pick_report": {
        "ru": "📈 За какой период построить сводку?",
        "en": "📈 For what period should I build the summary?",
    },
    "period_pick_export": {
        "ru": "📄 За какой период .xlsx-отчёт?",
        "en": "📄 For what period the .xlsx report?",
    },
    "period_pick_sheets": {
        "ru": "🟢 За какой период Google Sheets?",
        "en": "🟢 For what period the Google Sheets?",
    },
    # — подсказки /pause /resume без имени кампании —
    "slash_pause_hint": {
        "ru": "Укажи кампанию: <code>/pause Название кампании</code> — чтобы приостановить.",
        "en": "Specify a campaign: <code>/pause Campaign name</code> — to pause it.",
    },
    "slash_resume_hint": {
        "ru": "Укажи кампанию: <code>/resume Название кампании</code> — чтобы возобновить.",
        "en": "Specify a campaign: <code>/resume Campaign name</code> — to resume it.",
    },
    # — крупные статичные тексты (RU = texts.<KEY>, EN — перевод) —
    "start": {
        "ru": texts.START,
        "en": (
            "👋 <b>Aimash is here.</b>\n\n"
            "I read your Google Ads and propose changes, but I'm an <b>executor, not an "
            "autopilot</b> — the final word is always yours.\n\n"
            "Any change (budget, bid, keywords, pause) — <b>only after your “yes”</b>. "
            "First I show <i>“before → after”</i> with ✅/❌ buttons, then I apply it. Nothing "
            "happens without confirmation — it's your money, and it stays under control.\n\n"
            "🔒 Every action is written to the journal: it's always clear what changed and when. "
            "I change the budget only on your direct command.\n\n"
            "Try plain text:\n"
            "• <i>show stats for 7 days</i>\n"
            "• <i>raise the Search Spring budget by 20%</i>\n"
            "• <i>pause Brand</i>\n\n"
            "Or tap the menu buttons. /help — more details."
        ),
    },
    "help": {
        "ru": texts.HELP,
        "en": (
            "<b>What I can do now</b>\n"
            "Before any change I show “before → after” and wait for your “yes”.\n\n"
            "<b>Changes</b> (by text, with confirmation):\n"
            "• budget, CPC bid, keywords, negative keywords, pause/resume\n\n"
            "<b>Commands</b>\n"
            "/status — quick stats (30 days)\n"
            "/campaigns — campaigns + quick actions (pause/resume, 🎯 audiences)\n"
            "/pause Name — pause a campaign (with confirmation)\n"
            "/resume Name — resume a campaign (with confirmation)\n"
            "/report [7|30|90|MTD | YYYY-MM-DD [YYYY-MM-DD]] — period summary (default 30 days)\n"
            "/export [period] — deep report .xlsx (preset or date range)\n"
            "/sheets [period] — deep report in Google Sheets (link)\n"
            "/rsa — generate ad copy (RSA), element-by-element confirm (created paused)\n"
            "/newsearch — create a search campaign (RSA + keywords), paused — launch separately\n"
            "/keywords — keyword research (volume, competition, clusters) + .xlsx\n"
            "🖼 send a photo — I'll build a display campaign (GDN), created after “yes” (paused)\n"
            "/model — choose the AI model (OpenRouter)\n"
            "/lang — interface language (RU/EN)\n"
            "/balance — AI budget: OpenRouter balance and spend\n"
            "/journal — change journal: what changed and when\n"
            "/cancel — cancel the current draft\n\n"
            "<i>New ads and campaigns are created paused — launch is separate, so nothing goes "
            "live without your decision. Scheduled reports and anomaly alerts run in the "
            "background.</i>"
        ),
    },
    # — модель ИИ (/model) —
    "model_set": {
        "ru": texts.MODEL_SET,
        "en": "🧠 Model switched to <code>{model}</code>.",
    },
    "model_reset": {
        "ru": texts.MODEL_RESET,
        "en": "↩️ Reset to the default model: <code>{model}</code>.",
    },
    "model_ask_custom": {
        "ru": texts.MODEL_ASK_CUSTOM,
        "en": (
            "✏️ Send an OpenRouter model slug in a single message.\n"
            "For example: <code>anthropic/claude-sonnet-4.6</code> or "
            "<code>openai/gpt-4o-mini</code>\n"
            "See the list at openrouter.ai/models. Function calling support is required."
        ),
    },
    "model_bad": {
        "ru": texts.MODEL_BAD,
        "en": (
            "That doesn't look like an OpenRouter model slug (expected "
            "<code>vendor/model</code>, up to 128 characters). Send it again or /model for the "
            "menu."
        ),
    },
    # — keyword research —
    "kw_ask": {
        "ru": texts.KW_ASK,
        "en": (
            "🔍 <b>Keyword research</b>\n"
            "Send seed words separated by commas and/or a link in a single message.\n"
            "For example: <code>flower delivery, bouquets, 101 roses</code>\n"
            "or a link <code>https://example.com</code>"
        ),
    },
    "kw_searching": {
        "ru": texts.KW_SEARCHING,
        "en": "⏳ Researching keywords and grouping them by intent…",
    },
    "kw_empty": {
        "ru": texts.KW_EMPTY,
        "en": "Nothing found for these seeds. Try other words or a link: /keywords",
    },
    "kw_bad_input": {
        "ru": texts.KW_BAD_INPUT,
        "en": "I need seed words or a link. Send, for example: <code>buy phone, smartphone</code>",
    },
    # — §7: добавление подобранных ключей в кампанию (после показа списка + типа + «да») —
    "kw_add_pick_campaign": {
        "ru": "В какую кампанию добавить ключи? Пришли название кампании одним сообщением.",
        "en": "Which campaign should I add the keywords to? Send the campaign name in one message.",
    },
    "kw_add_empty_campaign": {
        "ru": "Пришли НАЗВАНИЕ кампании одним сообщением (например: <code>Search Spring</code>).",
        "en": "Send the campaign NAME in a single message (e.g. <code>Search Spring</code>).",
    },
    "kw_add_pick_match": {
        "ru": "🔑 Тип соответствия для «{camp}» ({n} ключ.):",
        "en": "🔑 Match type for “{camp}” ({n} kw):",
    },
    "kw_add_stale": {
        "ru": "Список подобранных ключей устарел — запусти /keywords заново.",
        "en": "The researched keyword list expired — run /keywords again.",
    },
    # — §3: гео-таргетинг кампании из меню (локации / радиус → confirm-гейт) —
    "geo_mode_pick": {
        "ru": "📍 Гео-таргетинг кампании «{camp}». Как зададим географию?",
        "en": "📍 Geo targeting for campaign “{camp}”. How should we set it?",
    },
    "geo_pick_locations": {
        "ru": (
            "Пришли локации через запятую (страна/город/регион), "
            "например: <code>Киев, Львов, Одесса</code>."
        ),
        "en": (
            "Send locations separated by commas (country/city/region), "
            "for example: <code>Kyiv, Lviv, Odesa</code>."
        ),
    },
    "geo_pick_proximity": {
        "ru": (
            "Пришли «город, радиус_км» — например: <code>Киев, 10</code> "
            "(точку Google вычислит по адресу, радиус в км)."
        ),
        "en": (
            "Send “city, radius_km” — for example: <code>Kyiv, 10</code> "
            "(Google geocodes the address; radius is in km)."
        ),
    },
    "geo_empty_locations": {
        "ru": "Не вижу ни одной локации. Пришли через запятую, например: <code>Киев, Львов</code>.",
        "en": "No locations found. Send them comma-separated, e.g.: <code>Kyiv, Lviv</code>.",
    },
    "geo_bad_proximity": {
        "ru": "Не разобрал. Формат: «город, радиус_км», например: <code>Киев, 10</code>.",
        "en": "Couldn't parse it. Format: “city, radius_km”, e.g.: <code>Kyiv, 10</code>.",
    },
    "geo_stale": {
        "ru": "Сессия гео устарела — открой меню кампании в /campaigns заново.",
        "en": "The geo session expired — reopen the campaign menu via /campaigns.",
    },
    # — confirm-гейт / статусы —
    "proposal_pending": {
        "ru": texts.PROPOSAL_PENDING,
        "en": "📝 <b>Change draft</b>\n\n{summary}\n\nConfirm?",
    },
    "proposal_long_header": {
        "ru": (
            "📎 <b>Черновик изменения</b> — полный текст во вложении ⬆️\n"
            "Это одно изменение. Подтверди кнопками <b>✅ / ❌</b> ниже 👇"
        ),
        "en": (
            "📎 <b>Change draft</b> — full text in the attachment ⬆️\n"
            "This is a single change. Confirm with the <b>✅ / ❌</b> buttons below 👇"
        ),
    },
    "applied": {
        "ru": texts.APPLIED,
        "en": "✅ <b>Done.</b>\n{result}",
    },
    "failed": {
        "ru": texts.FAILED,
        "en": "⚠️ Failed to execute: {kind}: {err}",
    },
    "no_campaigns": {
        "ru": texts.NO_CAMPAIGNS,
        "en": "No campaigns.",
    },
    "camp_list_stale": {
        "ru": texts.CAMP_LIST_STALE,
        "en": "The campaign list is outdated — run /campaigns again.",
    },
    "no_audiences": {
        "ru": texts.NO_AUDIENCES,
        "en": "👥 No available audiences (remarketing lists) found in the account.",
    },
    "aud_list_stale": {
        "ru": texts.AUD_LIST_STALE,
        "en": "The audience list is outdated — open the campaign menu again.",
    },
    # — RSA-генерация —
    "rsa_pick_campaign": {
        "ru": texts.RSA_PICK_CAMPAIGN,
        "en": "✍️ <b>Ad copy generation</b>\nChoose a campaign:",
    },
    "rsa_pick_adgroup": {
        "ru": texts.RSA_PICK_ADGROUP,
        "en": "Choose an ad group:",
    },
    "rsa_no_adgroups": {
        "ru": texts.RSA_NO_ADGROUPS,
        "en": "The campaign has no ad groups — create a group first.",
    },
    "rsa_ask_brief": {
        "ru": texts.RSA_ASK_BRIEF,
        "en": (
            "Send the ad's <b>topic</b> and <b>link</b> in a single message.\n"
            "For example: <code>flower delivery | https://example.com</code>"
        ),
    },
    "rsa_bad_url": {
        "ru": texts.RSA_BAD_URL,
        "en": "I don't see a valid link (http/https). Send the topic and URL again.",
    },
    "rsa_generating": {
        "ru": texts.RSA_GENERATING,
        "en": "⏳ Generating variants…",
    },
    "rsa_gen_empty": {
        "ru": texts.RSA_GEN_EMPTY,
        "en": "Couldn't generate enough valid variants. Try again: /rsa",
    },
    "rsa_session_stale": {
        "ru": texts.RSA_SESSION_STALE,
        "en": "The generation session expired — start over: /rsa",
    },
    "rsa_refine_prompt": {
        "ru": texts.RSA_REFINE_PROMPT,
        "en": "✏️ What should I fix in this element? Send a short edit as text.",
    },
    "rsa_refine_too_long": {
        "ru": texts.RSA_REFINE_TOO_LONG,
        "en": "The refined variant didn't fit the limit ({n}/{limit}). Send the edit again.",
    },
    "rsa_below_min": {
        "ru": texts.RSA_BELOW_MIN,
        "en": "Need ≥3 approved headlines and ≥2 descriptions. Now: {h} headl. / {d} descr.",
    },
    "rsa_created": {
        "ru": texts.RSA_CREATED,
        "en": "✅ <b>Ad created (paused).</b>\n{result}",
    },
    # — GDN из фото —
    "gdn_ask_brief": {
        "ru": texts.GDN_ASK_BRIEF,
        "en": (
            "🖼 <b>Photo accepted.</b> I'll build a display campaign (GDN).\n"
            "Send in a single message: <b>name | link | daily budget</b>.\n"
            "For example: <code>Spring 2026 | https://shop.example | 50</code>\n\n"
            "I'll generate the copy myself — I'll show a “before → after” draft before creating."
        ),
    },
    "gdn_bad_brief": {
        "ru": texts.GDN_BAD_BRIEF,
        "en": (
            "Couldn't parse it. Need <b>name | link | budget</b> (budget is a number).\n"
            "For example: <code>Summer sale | https://shop.example | 30</code>"
        ),
    },
    "gdn_generating": {
        "ru": texts.GDN_GENERATING,
        "en": "⏳ Generating ad copy…",
    },
    "gdn_gen_empty": {
        "ru": texts.GDN_GEN_EMPTY,
        "en": "Couldn't generate valid copy. Send the photo and brief again.",
    },
    "gdn_session_stale": {
        "ru": texts.GDN_SESSION_STALE,
        "en": "The campaign-creation session expired — send the photo again.",
    },
    "gdn_created": {
        "ru": texts.GDN_CREATED,
        "en": "✅ <b>Campaign created (paused).</b>\n{result}",
    },
    # — поисковая кампания (/newsearch) —
    "search_ask_brief": {
        "ru": texts.SEARCH_ASK_BRIEF,
        "en": (
            "🆕 <b>New search campaign</b>\n"
            "Send in a single message, separated by <code>|</code>:\n"
            "<code>Name | https://site | daily_budget [| topic [| kw1, kw2]]</code>\n\n"
            "For example:\n"
            "<code>Flower delivery | https://flowers.ua | 300 | bouquet delivery Kyiv | "
            "flower delivery, rose bouquet</code>\n\n"
            "I'll generate headlines/descriptions (RSA) and show a draft. The campaign is "
            "created <b>paused</b> — launch is a separate action."
        ),
    },
    "search_generating": {
        "ru": texts.SEARCH_GENERATING,
        "en": "⏳ Generating ad copy (RSA) for the campaign…",
    },
    "search_gen_empty": {
        "ru": texts.SEARCH_GEN_EMPTY,
        "en": (
            "Couldn't generate enough copy (need ≥3 headlines and ≥2 descriptions). "
            "Try another topic: /newsearch"
        ),
    },
    "search_bad_brief": {
        "ru": texts.SEARCH_BAD_BRIEF,
        "en": (
            "Invalid format. Need: <code>Name | https://site | budget "
            "[| topic [| comma-separated keywords]]</code>\n"
            "Budget is a number in the account currency (0 &lt; budget ≤ 1 000 000). Send again."
        ),
    },
    # — короткие callback-тосты (cq.answer); раньше литералами в bot.main —
    "model_list_stale": {
        "ru": "Список устарел — открой /model заново",
        "en": "The list is outdated — open /model again",
    },
    "cb_done": {"ru": "Готово", "en": "Done"},
    "cb_reset": {"ru": "Сброшено", "en": "Reset"},
    "cb_error": {"ru": "Ошибка: {kind}", "en": "Error: {kind}"},
    "cb_working": {"ru": "Выполняю…", "en": "Working…"},
    "cb_cancelled": {"ru": "Отменено", "en": "Cancelled"},
    "cb_approved": {"ru": "Одобрено", "en": "Approved"},
    "cb_rejected": {"ru": "Отклонено", "en": "Rejected"},
    "cb_approved_all": {"ru": "Одобрены все валидные", "en": "All valid approved"},
    # — прогресс/итог отчётов —
    "report_preparing_xlsx": {"ru": "Готовлю .xlsx-отчёт…", "en": "Preparing the .xlsx report…"},
    "report_preparing_sheets": {
        "ru": "Готовлю Google Sheets-отчёт…",
        "en": "Preparing the Google Sheets report…",
    },
    "sheets_ready": {
        "ru": "✅ Google Sheets готов: {url}",
        "en": "✅ Google Sheets is ready: {url}",
    },
    # — префиксы ошибок (plain text, {err} = ux.err_text(e)) —
    "err_stats": {
        "ru": "⚠️ Не удалось получить статистику: {err}",
        "en": "⚠️ Couldn't fetch the stats: {err}",
    },
    "err_balance": {
        "ru": "⚠️ Не удалось получить баланс OpenRouter: {err}",
        "en": "⚠️ Couldn't fetch the OpenRouter balance: {err}",
    },
    "err_journal": {
        "ru": "⚠️ Не удалось прочитать журнал: {err}",
        "en": "⚠️ Couldn't read the journal: {err}",
    },
    "err_campaigns": {
        "ru": "⚠️ Не удалось получить кампании: {err}",
        "en": "⚠️ Couldn't fetch campaigns: {err}",
    },
    "err_report": {
        "ru": "⚠️ Не удалось построить отчёт: {err}",
        "en": "⚠️ Couldn't build the report: {err}",
    },
    "err_report_make": {
        "ru": "⚠️ Не удалось сформировать отчёт: {err}",
        "en": "⚠️ Couldn't generate the report: {err}",
    },
    "err_adgroups": {
        "ru": "⚠️ Не удалось получить группы: {err}",
        "en": "⚠️ Couldn't fetch ad groups: {err}",
    },
    "err_gen": {
        "ru": "⚠️ Генерация не удалась: {err}",
        "en": "⚠️ Generation failed: {err}",
    },
    "err_kw": {
        "ru": "⚠️ Не удалось подобрать ключи: {err}",
        "en": "⚠️ Couldn't research keywords: {err}",
    },
    "err_kw_xlsx": {
        "ru": "⚠️ Таблицу .xlsx сформировать не удалось: {err}",
        "en": "⚠️ Couldn't build the .xlsx table: {err}",
    },
    "err_refine": {
        "ru": "⚠️ Доработка не удалась: {err}",
        "en": "⚠️ Refinement failed: {err}",
    },
    "err_text_gen": {
        "ru": "⚠️ Генерация текстов не удалась: {err}",
        "en": "⚠️ Ad-copy generation failed: {err}",
    },
    "err_validate": {
        "ru": "⚠️ Параметры не прошли валидацию: {err}",
        "en": "⚠️ Parameters failed validation: {err}",
    },
    "err_photo": {
        "ru": "⚠️ Не удалось обработать фото: {err}",
        "en": "⚠️ Couldn't process the photo: {err}",
    },
    "err_period": {
        "ru": "⚠️ Не удалось разобрать период. Используй пресет 7/30/90/MTD или даты ГГГГ-ММ-ДД [ГГГГ-ММ-ДД].",
        "en": "⚠️ Couldn't parse the period. Use a preset 7/30/90/MTD or dates YYYY-MM-DD [YYYY-MM-DD].",
    },
    "err_unexpected": {
        "ru": "⚠️ Что-то пошло не так — записал в журнал ошибок (код {code}). Попробуй ещё раз.",
        "en": "⚠️ Something went wrong — logged for triage (code {code}). Please try again.",
    },
    "err_sheets": {
        "ru": (
            "⚠️ Не удалось выгрузить в Google Sheets: {err}\n"
            "Проверь настройку (docs/DEPLOYMENT.md → Google Sheets):\n"
            "1) включён ли Google Sheets API в Google Cloud — ссылка для включения обычно есть в "
            "тексте ошибки выше (после включения подожди 1–2 мин);\n"
            "2) есть ли у токена доступ drive.file: `python scripts/get_refresh_token.py` "
            "(отметь Google Ads и drive.file), затем перезапусти бота.\n"
            "📄 Тот же отчёт без этой настройки доступен сразу через /export (.xlsx)."
        ),
        "en": (
            "⚠️ Couldn't export to Google Sheets: {err}\n"
            "Check the setup (docs/DEPLOYMENT.md → Google Sheets):\n"
            "1) is the Google Sheets API enabled in Google Cloud — the enable link is usually in "
            "the error text above (after enabling, wait 1–2 min);\n"
            "2) does the token have drive.file access: `python scripts/get_refresh_token.py` "
            "(select Google Ads and drive.file), then restart the bot.\n"
            "📄 The same report without this setup is available right away via /export (.xlsx)."
        ),
    },
    # — agent.loop: пользовательские тексты (системный промпт НЕ переводим — он для модели) —
    "loop_unrecognized": {
        "ru": "Не удалось распознать команду — переформулируй.",
        "en": "Couldn't recognize the command — please rephrase.",
    },
    "loop_bad_tool_args": {
        "ru": "не удалось разобрать аргументы инструмента",
        "en": "couldn't parse the tool arguments",
    },
    "loop_clarify_default": {
        "ru": "Уточните, пожалуйста, команду.",
        "en": "Please clarify the command.",
    },
    "loop_bad_args": {
        "ru": "некорректные аргументы для {name}: {errors}",
        "en": "invalid arguments for {name}: {errors}",
    },
    "loop_unsupported": {
        "ru": (
            "Операция «{name}» пока не поддерживается — выполнить не смогу, поэтому "
            "не предлагаю подтверждение. Доступно: бюджет, ставка (CPC) и стратегия ставок, "
            "ключевые и минус-слова, гео-таргетинг, пауза/возобновление кампании, "
            "генерация RSA-текстов и подбор ключевых слов."
        ),
        "en": (
            "Operation “{name}” isn't supported yet — I can't execute it, so I won't offer "
            "confirmation. Available: budget, bid (CPC) and bidding strategy, keywords and "
            "negative keywords, geo-targeting, pause/resume of a campaign, RSA copy generation "
            "and keyword research."
        ),
    },
    "loop_unknown_tool": {
        "ru": "неизвестный инструмент: {name}",
        "en": "unknown tool: {name}",
    },
    "loop_no_accounts": {
        "ru": "нет разрешённых аккаунтов (allowed_customer_ids пуст)",
        "en": "no allowed accounts (allowed_customer_ids is empty)",
    },
    "loop_read_error": {
        "ru": "ошибка чтения Google Ads: {detail}",
        "en": "Google Ads read error: {detail}",
    },
    # — bot.ux: короткие подсказки к временным ошибкам (_err_hint) —
    "hint_timeout": {
        "ru": " — попробуй ещё раз через минуту.",
        "en": " — try again in a minute.",
    },
    "hint_network": {
        "ru": " — проверь сеть и попробуй ещё раз.",
        "en": " — check the connection and try again.",
    },
    "hint_ratelimit": {
        "ru": " — модель занята или достигнут лимит: упрости запрос или смени модель (/model).",
        "en": (
            " — the model is busy or the limit is reached: simplify the request or switch the "
            "model (/model)."
        ),
    },
}

_CHAT_LANG: dict[int, str] = {}


def normalize_lang(lang: str | None) -> str:
    return lang if lang in LANGS else DEFAULT_LANG


def current_lang() -> str:
    """Язык ТЕКУЩЕГО запроса (из contextvar; ставит LangMiddleware). Дефолт RU вне хендлера."""
    return normalize_lang(_LANG.get())


def set_current_lang(lang: str | None) -> contextvars.Token:
    """Поставить язык запроса в contextvar (для middleware). Возвращает Token для reset()."""
    return _LANG.set(normalize_lang(lang))


def reset_current_lang(token: contextvars.Token) -> None:
    """Снять язык запроса (в finally middleware), вернув предыдущее значение contextvar."""
    _LANG.reset(token)


def get_lang(chat_id: int) -> str:
    return _CHAT_LANG.get(chat_id, DEFAULT_LANG)


def set_lang(chat_id: int, lang: str | None) -> str:
    norm = normalize_lang(lang)
    _CHAT_LANG[chat_id] = norm
    return norm


async def load_langs() -> None:
    """Загрузить сохранённые языки из user_settings.language в кэш _CHAT_LANG (вызов на старте,
    после init_db). Персист переживает рестарт; строки без языка (NULL) пропускаем."""
    from sqlalchemy import select

    from db.models import UserSettings
    from db.session import Session

    async with Session() as s:
        rows = (await s.execute(select(UserSettings.chat_id, UserSettings.language))).all()
    for chat_id, language in rows:
        if language in LANGS:
            _CHAT_LANG[int(chat_id)] = language


async def save_lang(chat_id: int, lang: str | None) -> None:
    """Upsert выбранного языка в user_settings.language (переживает рестарт). Зеркалит паттерн
    bot.main._save_model_override. Кэш set_lang ставит вызывающий ДО этого — БД лишь персист."""
    from sqlalchemy import select

    from db.models import UserSettings
    from db.session import Session

    norm = normalize_lang(lang)
    async with Session() as s:
        row = (
            await s.execute(select(UserSettings).where(UserSettings.chat_id == chat_id))
        ).scalar_one_or_none()
        if row is None:
            s.add(UserSettings(chat_id=chat_id, language=norm))
        else:
            row.language = norm
        await s.commit()


def t(key: str, lang: str | None = None, /, **kw: object) -> str:
    """Перевод по ключу. lang=None → current_lang() (язык текущего запроса из contextvar).
    Приоритет: CATALOG[key][lang] → CATALOG[key][RU] → texts.<KEY> (мост) → key.
    Если переданы kw — применяется .format(**kw) (совместимо с texts.X.format(...))."""
    lang = current_lang() if lang is None else normalize_lang(lang)
    entry = CATALOG.get(key)
    if entry is not None:
        s = entry.get(lang) or entry.get(DEFAULT_LANG) or key
    else:
        s = getattr(texts, key.upper(), key)  # мост к не-мигрированным RU-константам
    return s.format(**kw) if kw else s
