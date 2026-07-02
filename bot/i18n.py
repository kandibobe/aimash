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
    # — §8/§9: пикер отчётов (аккаунт → кампания → период) —
    "report_pick_account": {
        "ru": "🏢 По какому аккаунту отчёт?",
        "en": "🏢 Which account should I report on?",
    },
    "report_pick_campaign": {
        "ru": "📋 Весь аккаунт или конкретная кампания?",
        "en": "📋 Whole account or a specific campaign?",
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
            "<b>📣 Campaigns</b>\n"
            "/newcampaign — step-by-step creation wizard: account → settings → keywords → ad → assets\n"
            "/newsearch — quick search campaign from a brief (RSA + keywords), paused\n"
            "🖼 send a photo — I'll build a display campaign (GDN), created after “yes” (paused)\n"
            "🎬 send a video or /newvideo — campaign from video: Demand Gen / Video (YouTube, paused)\n"
            "/campaigns — campaigns + quick actions (pause/resume, 🎯 audiences, 🧩 extensions)\n"
            "/pause Name · /resume Name — pause/resume (with confirmation)\n"
            "/templates — campaign templates · /savetemplate name [from Campaign] — save as template\n"
            "/recent — recent actions: repeat in one tap (with confirmation)\n\n"
            "<b>🔑 Keywords and ad copy</b>\n"
            "/keywords — keyword research (volume, competition, clusters) + .xlsx\n"
            "/rsa — generate ad copy (RSA), element-by-element confirm (created paused)\n\n"
            "<b>📊 Reports</b>\n"
            "/status — quick stats (30 days)\n"
            "/report [7|30|90|MTD | YYYY-MM-DD [YYYY-MM-DD]] — period summary (default 30 days)\n"
            "/export [period] — deep report .xlsx · /sheets [period] — in Google Sheets (link)\n"
            "/mcc [period] — summary across all MCC child accounts (per-currency subtotals)\n"
            "/quota — daily Google Ads API operation quota\n\n"
            "<b>ℹ️ Clients</b>\n"
            "/clients — knowledge base: client profile as text + site crawling → relevant generation\n"
            "/client &lt;id&gt; — client card by account id\n\n"
            "<b>⚙️ Settings and service</b>\n"
            "/account &lt;id&gt; | reset — read account for reports/keywords (default Draft)\n"
            "/refresh — refresh the account list and caches without a restart\n"
            "/model — choose the AI model (OpenRouter) · /balance — AI budget: balance and spend\n"
            "/lang — interface language (RU/EN)\n"
            "/journal — change journal · /diag — error journal\n"
            "/cancel — cancel the current draft\n\n"
            "<b>Like another campaign / from a brief</b>\n"
            "• “create campaign N with settings like campaign X” — I clone the settings.\n"
            "• 🧩 in /campaigns → “Extensions”: sitelinks, callouts, structured snippets, image.\n"
            "• By text: “add phone +380… to campaign X”, “add a −20% summer promo”, “add prices: "
            "Basic 9.99/mo…” — I'll build the extension (call/promotion/price) and ask to confirm.\n"
            "• 📎 send a link or a file (.txt/.csv/.docx/.xlsx) + a task — I'll read it and act "
            "(e.g. “research keywords for this landing” or “make a campaign from this brief”).\n\n"
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
    "kw_add_edit_prompt": {
        "ru": (
            "✍️ <b>Ключи для «{camp}» — списком.</b>\n"
            "Скопируй список ниже, отредактируй (по одному в строке или через запятую — удали лишние, "
            "добавь свои) и пришли <b>обратно одним сообщением</b>. Потом выберешь тип соответствия, "
            "и я покажу «было → станет» перед добавлением."
        ),
        "en": (
            "✍️ <b>Keywords for “{camp}” — as a list.</b>\n"
            "Copy the list below, edit it (one per line or comma-separated — remove extras, add your "
            "own) and send it <b>back in one message</b>. Then pick the match type and I'll show the "
            "diff before adding."
        ),
    },
    "kw_add_list_empty": {
        "ru": "Список пуст. Пришли ключи (по одному в строке или через запятую).",
        "en": "The list is empty. Send keywords (one per line or comma-separated).",
    },
    "kw_add_pick_match": {
        "ru": "🔑 Тип соответствия для «{camp}» ({n} ключ.):",
        "en": "🔑 Match type for “{camp}” ({n} kw):",
    },
    "kw_add_stale": {
        "ru": "Список подобранных ключей устарел — запусти /keywords заново.",
        "en": "The researched keyword list expired — run /keywords again.",
    },
    "kw_acct_fallback": {
        "ru": (
            "⚠️ Аккаунт <code>{acct}</code> недоступен для подбора (не под настроенным MCC или "
            "деактивирован). Подбираю по Draft. Чтобы вернуть аккаунт чтения по умолчанию: "
            "<code>/account reset</code>."
        ),
        "en": (
            "⚠️ Account <code>{acct}</code> isn't accessible for research (not under the configured "
            "MCC or deactivated). Falling back to Draft. To reset the default read account: "
            "<code>/account reset</code>."
        ),
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
        "en": "📝 <b>Change draft</b>\n\n{summary}\n\nConfirm? <i>(draft valid for 24h)</i>",
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
    "rsa_list_prompt": {
        "ru": (
            "✍️ <b>Заголовки и описания — списком.</b>\n"
            "Скопируй список ниже, отредактируй (по одному в строке, нумерацию/заголовки секций "
            "оставь) и пришли <b>обратно одним сообщением</b>. Я проверю длину (кириллица = 1 символ) "
            "и покажу «было → станет» перед созданием.\n"
            "Или нажми «✅ Использовать как есть»."
        ),
        "en": (
            "✍️ <b>Headlines &amp; descriptions — as a list.</b>\n"
            "Copy the list below, edit it (one per line, keep the numbering/section headers) and send "
            "it <b>back in one message</b>. I'll check the length (Cyrillic = 1 char) and show the "
            "diff before creating.\nOr tap “✅ Use as is”."
        ),
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
            "Send in a single message: <b>name | link | daily budget [| geo]</b>.\n"
            "Geo is optional (comma-separated locations).\n"
            "For example: <code>Spring 2026 | https://shop.example | 50 | Kenya, Nairobi</code>\n\n"
            "I'll generate the copy myself — I'll show a “before → after” draft before creating."
        ),
    },
    "gdn_bad_brief": {
        "ru": texts.GDN_BAD_BRIEF,
        "en": (
            "Couldn't parse it. Need <b>name | link | budget [| geo]</b> (budget is a number; "
            "geo optional).\n"
            "For example: <code>Summer sale | https://shop.example | 30 | Kenya</code>"
        ),
    },
    "gdn_generating": {
        "ru": texts.GDN_GENERATING,
        "en": "⏳ Generating ad copy…",
    },
    # §11: визард кампании из видео (Demand Gen / Video). Убирает «тихий» проигрыш видео.
    "video_received": {
        "ru": (
            "🎬 <b>Видео принято.</b> Кампании из видео (Demand Gen / Video) используют видео, "
            "размещённое на YouTube — загрузить файл напрямую в Google Ads нельзя.\n"
            "Пришли <b>ссылку на это видео на YouTube</b> (или его 11-символьный id) — соберу "
            "черновик кампании. Всё создаётся <b>на паузе</b>; запуск — отдельной командой.\n\n"
            "Для медийной кампании из <b>картинки</b> (GDN) — пришли фото."
        ),
        "en": (
            "🎬 <b>Video received.</b> Video campaigns (Demand Gen / Video) use a video hosted on "
            "YouTube — direct file upload into Google Ads isn't possible.\n"
            "Send a <b>YouTube link</b> to this video (or its 11-char id) and I'll assemble a "
            "campaign draft. Everything is created <b>paused</b>; launch is a separate command.\n\n"
            "For a display campaign from an <b>image</b> (GDN) — send a photo."
        ),
    },
    "video_bad_link": {
        "ru": (
            "Не распознал ссылку на YouTube. Пришли ссылку вида "
            "<code>https://youtube.com/watch?v=…</code> / <code>youtu.be/…</code> "
            "или 11-символьный id видео."
        ),
        "en": (
            "Couldn't parse the YouTube link. Send a link like "
            "<code>https://youtube.com/watch?v=…</code> / <code>youtu.be/…</code> "
            "or the 11-char video id."
        ),
    },
    "video_pick_type": {
        "ru": (
            "▶️ Видео: <code>{vid}</code>\n"
            "Какой тип кампании собрать?\n"
            "• <b>Demand Gen</b> — YouTube/Discover/Gmail, лучший дефолт для лидов и продаж.\n"
            "• <b>Video</b> — охватная видеокампания (CPM)."
        ),
        "en": (
            "▶️ Video: <code>{vid}</code>\n"
            "Which campaign type should I build?\n"
            "• <b>Demand Gen</b> — YouTube/Discover/Gmail, best default for leads and sales.\n"
            "• <b>Video</b> — reach video campaign (CPM)."
        ),
    },
    "video_ask_brief": {
        "ru": (
            "Пришли одним сообщением: <b>название | ссылка на сайт | дневной бюджет [| гео]</b>.\n"
            "Гео — опционально (локации через запятую).\n"
            "Например: <code>Кения авто | https://kasimotors.co.ke | 40 | Кения</code>\n\n"
            "Тексты сгенерирую сам — покажу черновик «было → станет» перед созданием."
        ),
        "en": (
            "Send one message: <b>name | site link | daily budget [| geo]</b>.\n"
            "Geo is optional (comma-separated locations).\n"
            "E.g.: <code>Kenya cars | https://kasimotors.co.ke | 40 | Kenya</code>\n\n"
            "I'll generate the copy myself — you'll see a draft before anything is created."
        ),
    },
    "video_ask_logo": {
        "ru": (
            "🖼 Логотип для Demand Gen (опционально, квадратный 1:1): пришли <b>фото</b> "
            "или нажми «⏭ Пропустить»."
        ),
        "en": (
            "🖼 Logo for Demand Gen (optional, square 1:1): send a <b>photo</b> or press “⏭ Skip”."
        ),
    },
    "video_session_stale": {
        "ru": "Сессия кампании из видео устарела — пришли ссылку на YouTube заново.",
        "en": "The video-campaign session is stale — send the YouTube link again.",
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
    # — §2A: клон кампании «как в X» —
    "clone_bad_args": {
        "ru": "Нужны имя новой кампании и образец: «сделай кампанию N как в кампании X».",
        "en": "I need the new name and a source: “create campaign N like campaign X”.",
    },
    "clone_read_error": {
        "ru": "Не удалось прочитать кампанию-образец: {err}",
        "en": "Couldn't read the source campaign: {err}",
    },
    "clone_source_not_found": {
        "ru": "Кампания-образец «{name}» не найдена в аккаунте.",
        "en": "Source campaign “{name}” not found in the account.",
    },
    "clone_not_search": {
        "ru": "Клонирование поддержано только для ПОИСКОВЫХ кампаний, а «{name}» — другого типа.",
        "en": "Cloning is supported only for SEARCH campaigns; “{name}” is a different type.",
    },
    "clone_empty": {
        "ru": "У кампании «{name}» нет групп/текстов для клонирования.",
        "en": "Campaign “{name}” has no ad groups/copy to clone.",
    },
    "clone_no_url": {
        "ru": "У образца «{name}» не нашёлся финальный URL — укажи его в команде.",
        "en": "Source “{name}” has no final URL — specify one in the command.",
    },
    "clone_no_budget": {
        "ru": "Не удалось определить бюджет образца — укажи дневной бюджет в команде.",
        "en": "Couldn't determine the source budget — specify a daily budget in the command.",
    },
    "clone_name_taken": {
        "ru": "Кампания «{name}» уже существует — выбери другое имя для новой.",
        "en": "Campaign “{name}” already exists — choose a different name for the new one.",
    },
    # — ingest: чтение файлов и ссылок → задача агенту —
    "ingest_used": {
        "ru": "📎 Прочитал «{source}» — использую как контекст для задачи.",
        "en": "📎 Read “{source}” — using it as context for the task.",
    },
    "ingest_ask_task": {
        "ru": "📄 Файл «{source}» прочитан. Что с ним сделать? Напиши задачу одним сообщением "
        "(например: «сделай поисковую кампанию по этому брифу» или «подбери ключи»).",
        "en": "📄 File “{source}” read. What should I do with it? Send the task in one message "
        "(e.g. “make a search campaign from this brief” or “research keywords”).",
    },
    "ingest_empty_task": {
        "ru": "Напиши, что сделать с файлом, одним сообщением.",
        "en": "Send what to do with the file in a single message.",
    },
    "ingest_stale": {
        "ru": "Контент файла устарел — пришли файл заново.",
        "en": "The file content expired — send the file again.",
    },
    "ingest_too_big": {
        "ru": "Файл слишком большой (> {mb} МБ). Пришли поменьше или пришли текст.",
        "en": "File too large (> {mb} MB). Send a smaller one or paste text.",
    },
    "ingest_file_failed": {
        "ru": "Не удалось прочитать файл: {err}",
        "en": "Couldn't read the file: {err}",
    },
    "ingest_link_failed": {
        "ru": "Не смог прочитать ссылку: {err}. Выполняю задачу без её содержимого.",
        "en": "Couldn't read the link: {err}. Proceeding without its content.",
    },
    # — §3-assets: ассеты-расширения кампании —
    "ext_menu_pick": {
        "ru": "🧩 Расширения кампании «{camp}». Что добавить или показать?",
        "en": "🧩 Extensions for campaign “{camp}”. What to add or show?",
    },
    "ext_ask_sitelinks": {
        "ru": "Пришли быстрые ссылки построчно: <code>Текст | https://url [| описание1 [| описание2]]</code>.\n"
        "Текст ссылки ≤25, описания ≤35 симв.",
        "en": "Send sitelinks line by line: <code>Text | https://url [| description1 [| description2]]</code>.\n"
        "Link text ≤25, descriptions ≤35 chars.",
    },
    "ext_bad_sitelinks": {
        "ru": "Не разобрал ни одной ссылки. Формат построчно: <code>Текст | https://url</code>.",
        "en": "Couldn't parse any sitelink. Per line: <code>Text | https://url</code>.",
    },
    "ext_ask_callouts": {
        "ru": "Пришли уточнения через запятую (каждое ≤25 симв.), например: "
        "<code>Бесплатная доставка, Гарантия 2 года, Поддержка 24/7</code>.",
        "en": "Send callouts comma-separated (each ≤25 chars), e.g.: "
        "<code>Free shipping, 2-year warranty, 24/7 support</code>.",
    },
    "ext_bad_callouts": {
        "ru": "Не вижу уточнений. Пришли через запятую, например: <code>Гарантия, Доставка</code>.",
        "en": "No callouts found. Send them comma-separated, e.g.: <code>Warranty, Delivery</code>.",
    },
    "ext_ask_snippet_header": {
        "ru": "📑 Структурное описание для «{camp}». Выбери заголовок:",
        "en": "📑 Structured snippet for “{camp}”. Choose a header:",
    },
    "ext_ask_snippet_values": {
        "ru": "Заголовок «{header}». Пришли 3–10 значений через запятую (каждое ≤25 симв.).",
        "en": "Header “{header}”. Send 3–10 values comma-separated (each ≤25 chars).",
    },
    "ext_bad_snippet_values": {
        "ru": "Нужно минимум 3 значения через запятую (каждое ≤25 симв.).",
        "en": "Need at least 3 values, comma-separated (each ≤25 chars).",
    },
    "ext_ask_image": {
        "ru": "🖼 Пришли фото — добавлю его изображением-ассетом в кампанию (обрежу до 1.91:1).",
        "en": "🖼 Send a photo — I'll add it as an image asset to the campaign (cropped to 1.91:1).",
    },
    "ext_stale": {
        "ru": "Сессия расширений устарела — открой меню кампании в /campaigns заново.",
        "en": "The extensions session expired — reopen the campaign menu via /campaigns.",
    },
    "ext_empty": {
        "ru": "У кампании пока нет расширений. Добавь их из этого меню.",
        "en": "The campaign has no extensions yet. Add some from this menu.",
    },
    "ext_list_title": {
        "ru": "🧩 Текущие расширения ({n}). 🗑 — открепить (с подтверждением):",
        "en": "🧩 Current extensions ({n}). 🗑 — detach (with confirmation):",
    },
    "ext_list_stale": {
        "ru": "Список расширений устарел — открой «Показать текущие» заново.",
        "en": "The extensions list is outdated — open “Show current” again.",
    },
    "ext_show_error": {
        "ru": "Не удалось получить расширения: {err}",
        "en": "Couldn't fetch extensions: {err}",
    },
    # — §2B: именованные шаблоны кампаний —
    "tpl_list_empty": {
        "ru": "📋 Шаблонов пока нет. Создай кампанию (/newsearch или клон) и сохрани: "
        "<code>/savetemplate имя</code>, либо <code>/savetemplate имя from Кампания</code>.",
        "en": "📋 No templates yet. Create a campaign (/newsearch or clone) and save it: "
        "<code>/savetemplate name</code>, or <code>/savetemplate name from Campaign</code>.",
    },
    "tpl_list_title": {
        "ru": "📋 Шаблоны кампаний ({n}). «использовать» создаст кампанию по шаблону (с подтверждением).",
        "en": "📋 Campaign templates ({n}). “use” creates a campaign from a template (with confirmation).",
    },
    "tpl_save_hint": {
        "ru": "Укажи имя: <code>/savetemplate имя</code> (из последней созданной кампании) "
        "или <code>/savetemplate имя from Название кампании</code> (из живой кампании).",
        "en": "Specify a name: <code>/savetemplate name</code> (from the last created campaign) "
        "or <code>/savetemplate name from Campaign name</code> (from a live campaign).",
    },
    "tpl_save_none": {
        "ru": "Нет недавнего черновика кампании для сохранения. Сначала /newsearch или клонируй "
        "кампанию, либо используй <code>/savetemplate имя from Название</code>.",
        "en": "No recent campaign draft to save. First run /newsearch or clone a campaign, "
        "or use <code>/savetemplate name from Campaign</code>.",
    },
    "tpl_saved": {
        "ru": "✅ Шаблон «{name}» сохранён. Открой /templates, чтобы создать по нему кампанию.",
        "en": "✅ Template “{name}” saved. Open /templates to create a campaign from it.",
    },
    "tpl_deleted": {"ru": "🗑 Шаблон удалён", "en": "🗑 Template deleted"},
    "tpl_list_stale": {
        "ru": "Список шаблонов устарел — открой /templates заново.",
        "en": "The templates list is outdated — open /templates again.",
    },
    "tpl_ask_name": {
        "ru": "Имя НОВОЙ кампании из шаблона «{tpl}»? Пришли название одним сообщением.",
        "en": "Name of the NEW campaign from template “{tpl}”? Send it in one message.",
    },
    "tpl_name_empty": {
        "ru": "Пришли название новой кампании одним сообщением.",
        "en": "Send the new campaign name in a single message.",
    },
    "tpl_not_found": {
        "ru": "Шаблон не найден — открой /templates заново.",
        "en": "Template not found — open /templates again.",
    },
    # — §2C: авто-память (/recent — повтор недавних действий) —
    "recent_empty": {
        "ru": "📭 Пока нет применённых действий для повтора. Сделай что-нибудь (с подтверждением) — и оно появится тут.",
        "en": "📭 No applied actions to repeat yet. Do something (with confirmation) and it will show up here.",
    },
    "recent_title": {
        "ru": "↻ <b>Недавние действия</b> — нажми «↻ N», чтобы повторить (с подтверждением):",
        "en": "↻ <b>Recent actions</b> — tap “↻ N” to repeat (with confirmation):",
    },
    "recent_stale": {
        "ru": "Список устарел — открой /recent заново.",
        "en": "The list is outdated — open /recent again.",
    },
    "recent_unsupported": {
        "ru": "Это действие нельзя повторить автоматически.",
        "en": "This action can't be repeated automatically.",
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
    # — универсальная навигация мастеров (NavCB cancel) —
    "wizard_cancelled": {
        "ru": "✖ Отменено. Вышли из мастера.",
        "en": "✖ Cancelled. Left the wizard.",
    },
    "main_menu_back": {"ru": "Главное меню 👇", "en": "Main menu 👇"},
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
    # §6 /account: выбор аккаунта ЧТЕНИЯ для отчётов (мутации всегда на Draft)
    "account_current": {
        "ru": (
            "👤 Активный аккаунт отчётов: <code>{cid}</code>{draft}\n"
            "Сменить: <code>/account 123-456-7890</code> · Сброс: <code>/account reset</code>\n"
            "⚠️ Только ЧТЕНИЕ (статистика/отчёты). Изменения — всегда на Draft-аккаунте."
        ),
        "en": (
            "👤 Active reports account: <code>{cid}</code>{draft}\n"
            "Switch: <code>/account 123-456-7890</code> · Reset: <code>/account reset</code>\n"
            "⚠️ READ-only (stats/reports). Mutations always target the Draft account."
        ),
    },
    "account_set": {
        "ru": "✅ Аккаунт отчётов: <code>{cid}</code>. /status /report /export /sheets теперь по нему.",
        "en": "✅ Reports account: <code>{cid}</code>. /status /report /export /sheets now use it.",
    },
    "account_reset": {
        "ru": "↩️ Сброшено: отчёты снова по Draft-аккаунту.",
        "en": "↩️ Reset: reports use the Draft account again.",
    },
    "refresh_working": {
        "ru": "🔄 Обновляю аккаунты и сбрасываю кэши…",
        "en": "🔄 Refreshing accounts and clearing caches…",
    },
    "refresh_done": {
        "ru": (
            "✅ Обновлено без рестарта. Дочерних аккаунтов на чтение: <b>{n}</b>.\n"
            "Кэши SDK-клиентов и валют сброшены — /report /keywords /mcc возьмут свежие данные."
        ),
        "en": (
            "✅ Refreshed without a restart. Readable child accounts: <b>{n}</b>.\n"
            "SDK-client and currency caches cleared — /report /keywords /mcc will use fresh data."
        ),
    },
    "err_refresh": {
        "ru": "⚠️ Не удалось обновить: {err}",
        "en": "⚠️ Couldn't refresh: {err}",
    },
    "acct_reset_auto": {
        "ru": (
            "↩️ Аккаунт <code>{acct}</code> недоступен для чтения (не под настроенным MCC или "
            "деактивирован) — вернул аккаунт отчётов на Draft. Проверь, что аккаунт под тем же MCC "
            "(GOOGLE_ADS_LOGIN_CUSTOMER_ID), либо задай его MCC в GOOGLE_ADS_LOGIN_CUSTOMER_IDS."
        ),
        "en": (
            "↩️ Account <code>{acct}</code> isn't readable (not under the configured MCC or "
            "deactivated) — reverted the reports account to Draft. Check it's under the same MCC "
            "(GOOGLE_ADS_LOGIN_CUSTOMER_ID), or set its MCC in GOOGLE_ADS_LOGIN_CUSTOMER_IDS."
        ),
    },
    "account_denied": {
        "ru": (
            "⛔ Аккаунт <code>{cid}</code> не разрешён на чтение (fail-closed). Доступны: "
            "Draft, дочерние обнаруженного MCC и GOOGLE_ADS_READ_CUSTOMER_IDS."
        ),
        "en": (
            "⛔ Account <code>{cid}</code> is not read-allowed (fail-closed). Available: "
            "Draft, discovered MCC children and GOOGLE_ADS_READ_CUSTOMER_IDS."
        ),
    },
    # §8: сводный отчёт по всем дочерним аккаунтам MCC (/mcc)
    "mcc_preparing": {
        "ru": "🏢 Собираю сводку по дочерним аккаунтам MCC…",
        "en": "🏢 Building the MCC child-accounts summary…",
    },
    "mcc_no_manager": {
        "ru": (
            "⚠️ MCC не настроен: пуст GOOGLE_ADS_LOGIN_CUSTOMER_ID. Сводка по дочерним аккаунтам "
            "(§8) недоступна без менеджерского аккаунта."
        ),
        "en": (
            "⚠️ MCC is not configured: GOOGLE_ADS_LOGIN_CUSTOMER_ID is empty. The child-accounts "
            "summary (§8) needs a manager account."
        ),
    },
    "err_mcc": {
        "ru": "⚠️ Не удалось построить сводку по MCC: {err}",
        "en": "⚠️ Couldn't build the MCC summary: {err}",
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
            "2) есть ли у токена доступ drive.file (+ spreadsheets.readonly для чтения чужих таблиц): "
            "`python scripts/get_refresh_token.py` (отметь Google Ads, drive.file и "
            "spreadsheets.readonly), затем перезапусти бота.\n"
            "📄 Тот же отчёт без этой настройки доступен сразу через /export (.xlsx)."
        ),
        "en": (
            "⚠️ Couldn't export to Google Sheets: {err}\n"
            "Check the setup (docs/DEPLOYMENT.md → Google Sheets):\n"
            "1) is the Google Sheets API enabled in Google Cloud — the enable link is usually in "
            "the error text above (after enabling, wait 1–2 min);\n"
            "2) does the token have drive.file (+ spreadsheets.readonly to read others' sheets) access: "
            "`python scripts/get_refresh_token.py` (select Google Ads, drive.file and "
            "spreadsheets.readonly), then restart the bot.\n"
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
    # — §20: «Информация про клиентов» —
    "cli_pick_account": {
        "ru": "ℹ️ <b>Информация про клиентов</b>\nВыберите рекламный аккаунт (✅ — профиль заполнен):",
        "en": "ℹ️ <b>Client info</b>\nChoose an ad account (✅ — profile filled):",
    },
    "cli_accounts_error": {
        "ru": "⚠️ Не удалось получить список аккаунтов MCC: {err}",
        "en": "⚠️ Couldn't list MCC accounts: {err}",
    },
    "cli_access_denied": {
        "ru": "Нет доступа к этому аккаунту.",
        "en": "No access to this account.",
    },
    "cli_card_stale": {
        "ru": "Сессия устарела — откройте раздел «ℹ️ Клиенты» заново.",
        "en": "Session expired — open “ℹ️ Clients” again.",
    },
    "cli_ask_text": {
        "ru": (
            "Пришлите информацию о клиенте обычным текстом (можно несколькими сообщениями): "
            "бизнес, сайт, соцсети, услуги, цены, телефоны. Когда закончите — «💾 Сохранить»."
        ),
        "en": (
            "Send the client info as free text (several messages are fine): business, site, "
            "socials, services, prices, phones. When done — “💾 Save”."
        ),
    },
    "cli_accumulating": {
        "ru": "Принял (сообщений: {n}, символов: {chars}). Пришлите ещё или «💾 Сохранить».",
        "en": "Got it (messages: {n}, chars: {chars}). Send more or “💾 Save”.",
    },
    "cli_empty_input": {
        "ru": "Пусто — пришлите текст о клиенте.",
        "en": "Empty — send some text about the client.",
    },
    "cli_nothing_to_save": {
        "ru": "Нет текста для сохранения — пришлите информацию о клиенте.",
        "en": "Nothing to save — send the client info first.",
    },
    "cli_extract_empty": {
        "ru": "Не удалось извлечь данные из текста. Попробуйте переформулировать.",
        "en": "Couldn't extract any data from the text. Try rephrasing.",
    },
    "cli_extracting": {
        "ru": "⏳ Разбираю информацию о клиенте…",
        "en": "⏳ Parsing the client info…",
    },
    "cli_no_profile_to_clear": {
        "ru": "У этого клиента ещё нет профиля.",
        "en": "This client has no profile yet.",
    },
    "cli_usage_hint": {
        "ru": "Использование: <code>/client &lt;customer_id&gt;</code> — карточка клиента.",
        "en": "Usage: <code>/client &lt;customer_id&gt;</code> — the client card.",
    },
    "cli_no_website": {
        "ru": "У клиента не указан сайт — добавьте его через «➕ Добавить информацию».",
        "en": "No website in the profile — add it via “➕ Add info”.",
    },
    "cli_crawl_started": {
        "ru": "🕷 Краулю сайт {domain}… Пришлю сводку по готовности.",
        "en": "🕷 Crawling {domain}… I'll send a summary when it's done.",
    },
    "cli_recrawl_incr_started": {
        "ru": "🆕 Проверяю {domain} на новые/изменённые страницы… Пришлю сводку по готовности.",
        "en": "🆕 Checking {domain} for new/changed pages… I'll send a summary when it's done.",
    },
    "cli_crawl_already": {
        "ru": "🕷 Обход сайта {domain} уже идёт — дождитесь сводки, второй запуск не нужен.",
        "en": "🕷 Crawl of {domain} is already running — wait for the summary, no need to start again.",
    },
    "cli_autosaved": {
        "ru": "💾 Авто-сохранение по таймауту: показал черновик профиля — подтвердите «да» ниже.",
        "en": "💾 Auto-save on idle: here's the profile draft — confirm “yes” below.",
    },
    "cli_crawl_unchanged": {
        "ru": "✅ Сайт {domain} не изменился ({pages} стр.). Профиль актуален, обновление не требуется.",
        "en": "✅ Site {domain} is unchanged ({pages} pages). Profile is up to date, no update needed.",
    },
    "cli_crawl_diff": {
        "ru": "🆕 Новых страниц: {new} · ✏️ изменённых: {changed}",
        "en": "🆕 New pages: {new} · ✏️ changed: {changed}",
    },
    "cli_crawl_profile_updated": {
        "ru": "Профиль клиента обновлён. Готов использовать в кампаниях и объявлениях.",
        "en": "Client profile updated. Ready to use in campaigns and ads.",
    },
    "cli_crawl_confirm_update": {
        "ru": "Обновить профиль клиента этими данными? Подтвердите изменения ниже.",
        "en": "Update the client profile with this data? Confirm the changes below.",
    },
    "cli_crawl_empty": {
        "ru": "⚠️ Краулинг {domain}: не удалось собрать полезные данные с сайта.",
        "en": "⚠️ Crawl of {domain}: couldn't collect useful data from the site.",
    },
    "cli_crawl_failed": {
        "ru": "⚠️ Краулинг {domain} не удался: {err}",
        "en": "⚠️ Crawl of {domain} failed: {err}",
    },
    # — §19: визард «Создание кампании» —
    "cc_pick_account": {
        "ru": (
            "🆕 <b>Создание кампании</b>\nВыбери аккаунт клиента — или пришли часть названия, "
            "покажу совпадения (§19.2 поиск):"
        ),
        "en": (
            "🆕 <b>Create campaign</b>\nChoose the client account — or send part of a name "
            "to search (§19.2):"
        ),
    },
    "cc_acct_search_empty": {
        "ru": "🔍 По «{q}» ничего не нашёл. Пришлите другую часть названия или выберите из списка.",
        "en": "🔍 Nothing found for “{q}”. Send another part of the name or pick from the list.",
    },
    "cc_acct_search_results": {
        "ru": "🔍 Найдено: {n}. Выберите аккаунт (показаны первые {shown}):",
        "en": "🔍 Found: {n}. Choose an account (showing first {shown}):",
    },
    # §20.6: sitelinks предложены из РЕАЛЬНОЙ карты страниц краула (не выдуманы LLM)
    "cc_asset_sitelinks_from_crawl": {
        "ru": "🔗 Ссылки предложены из реальной карты сайта ({n} стр. из краула §20).",
        "en": "🔗 Links proposed from the real crawled site map ({n} pages, §20).",
    },
    # §20.2: после сохранения/обновления профиля — карточка в один тап
    "cli_view_card_hint": {
        "ru": "📋 Профиль сохранён — открыть карточку клиента:",
        "en": "📋 Profile saved — open the client card:",
    },
    # §UX «что дальше» после успешного создания кампании (все действия — advisory)
    "cc_created_next_steps": {
        "ru": (
            "Следующие шаги — кнопками ниже: запустить (покажу PAUSED → ENABLED на подтверждение), "
            "посмотреть кампании или добавить минус-слова."
        ),
        "en": (
            "Next steps — use the buttons below: launch (I'll show PAUSED → ENABLED to confirm), "
            "view campaigns, or add negative keywords."
        ),
    },
    "cc_neg_kw_hint": {
        "ru": (
            "➖ Пришли текстом: <i>«добавь минус-слова: слово1, слово2 в кампанию {name}»</i> — "
            "соберу черновик и спрошу подтверждение (сам ничего не добавляю)."
        ),
        "en": (
            "➖ Send as text: <i>“add negative keywords: word1, word2 to campaign {name}”</i> — "
            "I'll build a draft and ask you to confirm (I never add them on my own)."
        ),
    },
    "cc_accounts_error": {
        "ru": "⚠️ Не удалось получить список аккаунтов MCC: {err}\nПродолжим на основном аккаунте.",
        "en": "⚠️ Couldn't list MCC accounts: {err}\nProceeding with the main account.",
    },
    "cc_ask_description": {
        "ru": (
            "Опишите рекламную кампанию одним сообщением — что продвигаем, страна/город, бюджет, "
            "цель. Я разложу это на настройки."
        ),
        "en": (
            "Describe the campaign in one message — what you promote, country/city, budget, goal. "
            "I'll turn it into settings."
        ),
    },
    "cc_extracting": {
        "ru": "⏳ Разбираю описание на настройки…",
        "en": "⏳ Parsing the description into settings…",
    },
    "cc_empty_description": {
        "ru": "Опишите кампанию текстом — что продвигаем, ГЕО, бюджет, цель.",
        "en": "Describe the campaign in text — what you promote, geo, budget, goal.",
    },
    "cc_draft_stale": {
        "ru": "Черновик кампании не найден или устарел — начните заново: «➕ Создание кампании».",
        "en": "The campaign draft was not found or expired — start over: “➕ Create campaign”.",
    },
    "cc_resume_prompt": {
        "ru": "У вас есть незавершённый черновик кампании (этап {step}/7). Продолжить или начать заново?",
        "en": "You have an unfinished campaign draft (step {step}/7). Resume or start over?",
    },
    "cc_settings_saved": {
        "ru": "✅ Настройки приняты.",
        "en": "✅ Settings accepted.",
    },
    # Этап 3: объявление (URL → display path → 15 заголовков / 4 описания)
    "cc_ask_url": {
        "ru": (
            "✍️ <b>Объявление.</b> Пришлите конечный URL (Final URL) рекламируемой страницы — "
            "например <code>https://shop.example/used-cars</code>. Я сам соберу display path и "
            "сгенерирую заголовки и описания."
        ),
        "en": (
            "✍️ <b>Ad.</b> Send the Final URL of the promoted page — e.g. "
            "<code>https://shop.example/used-cars</code>. I'll build the display path and generate "
            "headlines and descriptions."
        ),
    },
    "cc_bad_url": {
        "ru": "Не вижу корректную ссылку (http/https, ≤2048). Пришлите Final URL ещё раз.",
        "en": "I don't see a valid link (http/https, ≤2048). Send the Final URL again.",
    },
    "cc_generating_ad": {
        "ru": "⏳ Читаю страницу и генерирую заголовки/описания (RSA)…",
        "en": "⏳ Reading the page and generating headlines/descriptions (RSA)…",
    },
    "cc_ad_saved": {
        "ru": "✅ Объявление готово (заголовки и описания утверждены).",
        "en": "✅ Ad is ready (headlines and descriptions approved).",
    },
    # Этап 4: изображения
    "cc_images_prompt": {
        "ru": (
            "🖼 <b>Изображения объявления</b> (если применимо).\n"
            "Пришлите фото — добавлю его image-ассетом (обрежу до 1.91:1 и 1:1), или пропустите этот "
            "этап."
        ),
        "en": (
            "🖼 <b>Ad images</b> (if applicable).\n"
            "Send a photo — I'll add it as an image asset (cropped to 1.91:1 and 1:1), or skip this "
            "stage."
        ),
    },
    "cc_image_saved": {
        "ru": "🖼 Изображение добавлено к черновику ({n} шт.).",
        "en": "🖼 Image added to the draft ({n}).",
    },
    "cc_stage_skipped": {
        "ru": "⏭ Этап пропущен.",
        "en": "⏭ Stage skipped.",
    },
    "cc_photo_wrong_stage": {
        "ru": "📷 Сейчас не этап изображений. Используйте кнопки текущего шага визарда.",
        "en": "📷 Not the images stage now. Use the current wizard step's buttons.",
    },
    # Этап 2: ключевые слова
    "cc_kw_prompt": {
        "ru": (
            "🔑 <b>Ключевые слова.</b>\n"
            "Пришлите свои ключи — текстом (через запятую/построчно), файлом или ссылкой на "
            "Google-таблицу бота. Тип соответствия можно задать маркерами: <code>[точное]</code>, "
            '<code>"фразовое"</code> (по умолчанию — фразовое).\n'
            "Или нажмите «🔎 Генерация ключевых слов» — подберу автоматически с проверкой в таблице."
        ),
        "en": (
            "🔑 <b>Keywords.</b>\n"
            "Send your keywords — as text (comma/line separated), a file, or a link to the bot's "
            'Google Sheet. Match type via markers: <code>[exact]</code>, <code>"phrase"</code> '
            "(default phrase).\n"
            "Or tap “🔎 Generate keywords” — I'll research them with sheet verification."
        ),
    },
    "cc_kw_generating": {
        "ru": "⏳ Генерирую seed-ключи → Discover → фильтрую релевантность → собираю таблицу…",
        "en": "⏳ Generating seeds → Discover → relevance filter → building the sheet…",
    },
    "cc_kw_sheet_ready": {
        "ru": (
            "📊 Таблица ключей готова: {url}\n"
            "Проверьте пометки «Релевантность», удалите лишние строки и пришлите ссылку обратно."
        ),
        "en": (
            "📊 Keyword sheet is ready: {url}\n"
            "Check the relevance marks, delete unneeded rows and send the link back."
        ),
    },
    "cc_kw_sheet_failed": {
        "ru": "⚠️ Не удалось выгрузить таблицу ключей: {err}\nПришлите свои ключи текстом/файлом.",
        "en": "⚠️ Couldn't export the keyword sheet: {err}\nSend your keywords as text/file.",
    },
    "cc_kw_verify_prompt": {
        "ru": "Когда отредактируете таблицу — пришлите ссылку на неё одним сообщением.",
        "en": "When you've edited the sheet — send its link in a single message.",
    },
    "cc_kw_read_failed": {
        "ru": (
            "⚠️ Не смог прочитать таблицу: {err}\n"
            "Проверьте, что таблица доступна аккаунту бота (своя или расшаренная), и что у токена есть "
            "scope spreadsheets.readonly. Либо пришлите ключи текстом."
        ),
        "en": (
            "⚠️ Couldn't read the sheet: {err}\n"
            "Make sure the sheet is accessible to the bot's account (owned or shared) and the token has "
            "the spreadsheets.readonly scope. Or send keywords as text."
        ),
    },
    "cc_kw_accepted": {
        "ru": "✅ Принял ключевые слова: {n} (тип соответствия — {mt}). Переходим к объявлению.",
        "en": "✅ Keywords accepted: {n} (match type — {mt}). Moving on to the ad.",
    },
    "cc_kw_empty": {
        "ru": "Не вижу ни одного валидного ключа. Пришлите ещё раз или нажмите «🔎 Генерация».",
        "en": "No valid keywords found. Send again or tap “🔎 Generate”.",
    },
    "cc_kw_dropped": {
        "ru": "⚠️ Отброшено невалидных ключей: {n} (слишком длинные / &gt;10 слов / мусор).",
        "en": "⚠️ Dropped invalid keywords: {n} (too long / &gt;10 words / junk).",
    },
    "cc_keywords_truncated": {
        "ru": "⚠️ Ключей {total} — больше потолка. В кампанию войдут первые {kept}, остальные отброшены.",
        "en": "⚠️ {total} keywords exceed the cap. The first {kept} will be used, the rest are dropped.",
    },
    "cc_stage_expects_button": {
        "ru": "На этом шаге я жду нажатие кнопки (или фото, если это этап изображений/логотипа), а не текст.",
        "en": "This step expects a button tap (or a photo on the image/logo step), not text.",
    },
    "cc_kw_not_a_link": {
        "ru": "Не вижу корректную ссылку на Google-таблицу (http/https). Пришлите ссылку ещё раз.",
        "en": "That's not a valid Google Sheets link (http/https). Please send the link again.",
    },
    "list_more": {"ru": " …ещё {n}", "en": " …{n} more"},
    "file_fallback": {"ru": "файл", "en": "file"},
    "kw_negatives_advisory": {
        "ru": (
            "\n\n🚫 <b>Минус-слова</b> (предложение): {shown}{more}"
            "\n<i>Добавлю отдельной командой — после «да».</i>"
        ),
        "en": (
            "\n\n🚫 <b>Negative keywords</b> (suggested): {shown}{more}"
            "\n<i>I'll add them via a separate command — after your “yes”.</i>"
        ),
    },
    "cc_asset_logo_prompt": {
        "ru": "🖼 Пришлите <b>фото логотипа</b> (обрежу в 1:1). Или «✖ Отмена».",
        "en": "🖼 Send the <b>logo photo</b> (I'll crop to 1:1). Or “✖ Cancel”.",
    },
    "cc_asset_logo_added": {
        "ru": "✅ Логотип добавлен в набор ассетов (привяжу при создании кампании).",
        "en": "✅ Logo added to the asset set (linked when the campaign is created).",
    },
    "cc_edit_hint": {
        "ru": (
            "✏️ Пришлите правку обычным текстом — например: <i>поставь бюджет 60</i>, "
            "<i>добавь город Найроби</i>, <i>расписание пн-пт 9-18</i>, <i>старт 2026-08-01</i>. "
            "Я обновлю сводку."
        ),
        "en": (
            "✏️ Send the edit as plain text — e.g. <i>set budget 60</i>, <i>add city Nairobi</i>, "
            "<i>schedule Mon-Fri 9-18</i>, <i>start 2026-08-01</i>. I'll refresh the summary."
        ),
    },
    "cc_kw_mixed_mt": {
        "ru": 'смешанный (по маркерам [точное] / "фразовое" / широкое)',
        "en": 'mixed (per-keyword markers [exact] / "phrase" / broad)',
    },
    # §19.4: явный гейт подтверждения списка ключей перед Этапом 3
    "cc_kw_review": {
        "ru": (
            "🔑 Ключевые слова готовы: <b>{n}</b> (тип соответствия — {mt}).\n{preview}\n"
            "Подтвердите список или пришлите другой (текстом/файлом/ссылкой)."
        ),
        "en": (
            "🔑 Keywords ready: <b>{n}</b> (match type — {mt}).\n{preview}\n"
            "Confirm the list or send another one (text/file/link)."
        ),
    },
    "cc_kw_negatives_hint": {
        "ru": (
            "💡 Возможные минус-слова (советую, сам НЕ добавляю): {negs}\n"
            "Добавить можно командой после создания кампании (через подтверждение)."
        ),
        "en": (
            "💡 Suggested negative keywords (advisory, I do NOT add them myself): {negs}\n"
            "You can add them by command after the campaign is created (with confirmation)."
        ),
    },
    "cc_kw_wrong_sheet": {
        "ru": (
            "⚠️ Это не та таблица: я жду ссылку на таблицу, которую создал для верификации "
            "(id …{sid}). Пришлите её же — отредактированную."
        ),
        "en": (
            "⚠️ That's a different spreadsheet: I expect the link to the sheet I created for "
            "verification (id …{sid}). Please send that one back — edited."
        ),
    },
    "cc_kw_file_accepted": {
        "ru": "📎 Файл «{name}» прочитан.",
        "en": "📎 File “{name}” parsed.",
    },
    # Этап 5: ассеты
    "cc_assets_prompt": {
        "ru": (
            "🧩 <b>Ассеты объявления.</b>\n"
            "Можно переиспользовать готовые ассеты аккаунта или пропустить (добавить позже через "
            "«Кампании → Расширения»)."
        ),
        "en": (
            "🧩 <b>Ad assets.</b>\n"
            "Reuse existing account assets or skip (add later via “Campaigns → Extensions”)."
        ),
    },
    "cc_assets_none": {
        "ru": "На аккаунте не нашлось готовых ассетов. Пропускаю этап.",
        "en": "No existing account assets found. Skipping this stage.",
    },
    "cc_assets_reused": {
        "ru": "✅ Переиспользую {n} ассет(ов) аккаунта.",
        "en": "✅ Reusing {n} account asset(s).",
    },
    "cc_assets_pick_type": {
        "ru": "Какой ассет добавить? Сгенерирую наполнение по теме и сайту, вы сможете подтвердить.",
        "en": "Which asset to add? I'll generate the content from the topic and site for you to confirm.",
    },
    "cc_asset_generating": {
        "ru": "⏳ Генерирую наполнение ассета…",
        "en": "⏳ Generating asset content…",
    },
    "cc_asset_added": {
        "ru": "✅ Добавлен ассет: {label}. Всего новых: {n}. Добавить ещё или продолжить?",
        "en": "✅ Asset added: {label}. New total: {n}. Add more or continue?",
    },
    "cc_asset_gen_failed": {
        "ru": "⚠️ Не удалось сгенерировать ассет: {err}. Попробуйте другой тип.",
        "en": "⚠️ Couldn't generate the asset: {err}. Try another type.",
    },
    # Этап 6: Ad URL options
    "cc_url_prompt": {
        "ru": (
            "🔗 <b>Ad URL options</b> (опционально).\n"
            "Пришлите одной строкой через «|»: шаблон отслеживания | суффикс | custom parameters "
            "(key=value через запятую):\n"
            "<code>{{lpurl}}?utm_source=google | utm_medium=cpc | promo=summer, src=tg</code>\n"
            "Ненужные части оставьте пустыми. Или нажмите «⏭ Пропустить»."
        ),
        "en": (
            "🔗 <b>Ad URL options</b> (optional).\n"
            "Send one line via “|”: tracking template | suffix | custom parameters "
            "(comma-separated key=value):\n"
            "<code>{{lpurl}}?utm_source=google | utm_medium=cpc | promo=summer, src=tg</code>\n"
            "Leave unused parts empty. Or tap “⏭ Skip”."
        ),
    },
    "cc_url_bad": {
        "ru": "⚠️ Не разобрал URL-опции: {err}\nПришлите ещё раз или «⏭ Пропустить».",
        "en": "⚠️ Couldn't parse URL options: {err}\nSend again or “⏭ Skip”.",
    },
    "cc_url_saved": {
        "ru": "✅ Ad URL options сохранены.",
        "en": "✅ Ad URL options saved.",
    },
    # Этап 7: финал / создание / запуск
    "cc_create_done": {
        "ru": "✅ <b>Черновик кампании создан (PAUSED).</b>\n{result}",
        "en": "✅ <b>Campaign draft created (PAUSED).</b>\n{result}",
    },
    "cc_launch_prompt": {
        "ru": "🚀 Кампания создана на паузе. Запустить её (перевести в ENABLED)?",
        "en": "🚀 Campaign created paused. Launch it (set to ENABLED)?",
    },
    "cc_edit_applied": {
        "ru": "✏️ Правка применена. Обновлённая сводка:",
        "en": "✏️ Edit applied. Updated summary:",
    },
    # §19.8: после создания черновика (PAUSED) — отдельная прямая команда «Запустить» → ENABLED.
    "cc_created_launch_prompt": {
        "ru": (
            "✅ Черновик кампании создан (статус PAUSED, расход $0).\n"
            "Запуск — отдельной командой: нажмите «🚀 Запустить кампанию», "
            "и я покажу «PAUSED → ENABLED» для подтверждения."
        ),
        "en": (
            "✅ Campaign draft created (status PAUSED, $0 spend).\n"
            "Launch is a separate command: tap “🚀 Launch campaign” and I'll show "
            "“PAUSED → ENABLED” for confirmation."
        ),
    },
    "cc_launch_stale": {
        "ru": "🚀 Не нашёл, какую кампанию запускать. Откройте /campaigns → «Возобновить».",
        "en": "🚀 Couldn't find which campaign to launch. Use /campaigns → “Resume”.",
    },
    "cc_bidding_downgraded": {
        "ru": (
            "ℹ️ Стратегия ставок изменена на «Максимум кликов»: на аккаунте не настроено "
            "отслеживание конверсий (оно обязательно для «Максимум конверсий»/Target CPA). "
            "Настройте конверсии в Google Ads, затем смените стратегию командой."
        ),
        "en": (
            "ℹ️ Bidding switched to Maximize Clicks: the account has no conversion tracking "
            "(required for Maximize Conversions/Target CPA). Set up conversions in Google Ads, "
            "then change the strategy by command."
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
