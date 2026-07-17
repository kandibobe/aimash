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
    "throttle_warn": {
        "ru": "⏳ Слишком часто — подожди пару секунд.",
        "en": "⏳ Too frequent — wait a couple of seconds.",
    },
    "rejected": {"ru": "❌ Отменено", "en": "❌ Cancelled"},
    # Кнопки/статусы выгрузки результата /audit (bot.keyboards.audit_export_kb)
    "audit_export_btn_sheets": {"ru": "📄 В Google Sheets", "en": "📄 To Google Sheets"},
    "audit_export_btn_xlsx": {"ru": "📊 Скачать .xlsx", "en": "📊 Download .xlsx"},
    "audit_export_btn_docx": {"ru": "📝 Скачать .docx", "en": "📝 Download .docx"},
    "audit_export_stale": {
        "ru": "⏳ Результат аудита устарел — запусти /audit заново, затем жми кнопку выгрузки.",
        "en": "⏳ The audit result has expired — run /audit again, then tap the export button.",
    },
    # #6 Режим доп-вопросов (Q&A) по последнему /audit — свободный текст = вопрос READ-ONLY аналитику
    "audit_qa_hint": {
        "ru": "💬 Есть вопросы по этому аудиту? Спрашивай прямо здесь — отвечу по цифрам разбора "
        "(меняю аккаунт только отдельной командой). Любая команда или кнопка меню закроют режим.",
        "en": "💬 Questions about this audit? Just ask here — I’ll answer from the report’s numbers "
        "(I change the account only via a separate command). Any command or menu button closes this mode.",
    },
    "audit_qa_exit_btn": {"ru": "✖ Выйти из режима вопросов", "en": "✖ Exit Q&A mode"},
    "audit_qa_exited": {
        "ru": "Режим вопросов закрыт. Открыть снова — /audit.",
        "en": "Q&A mode closed. Reopen it with /audit.",
    },
    "audit_qa_stale": {
        "ru": "⏳ Контекст аудита устарел — запусти /audit заново, чтобы задавать вопросы.",
        "en": "⏳ The audit context has expired — run /audit again to ask questions.",
    },
    "audit_qa_failed": {
        "ru": "🤔 Не смог уверенно ответить по этому аудиту. Переформулируй вопрос или запусти "
        "/audit заново. Изменения в аккаунте — отдельной командой (например /pause).",
        "en": "🤔 I couldn’t answer that confidently from this audit. Rephrase, or run /audit again. "
        "Account changes go through a separate command (e.g. /pause).",
    },
    # §12 2FA — PIN перед исполнением опасной операции (opt-in, дефолт OFF)
    "twofa_prompt": {
        "ru": "🔐 Операция <b>{op}</b> требует подтверждения кодом. Введи PIN одним сообщением "
        "(или «отмена», чтобы прервать — черновик сохранится).",
        "en": "🔐 Operation <b>{op}</b> needs a PIN. Send the code in one message "
        "(or “cancel” to abort — the draft is kept).",
    },
    "twofa_wrong": {
        "ru": "❌ Неверный код. Осталось попыток: {left}. Введи PIN ещё раз или «отмена».",
        "en": "❌ Wrong code. Attempts left: {left}. Send the PIN again or “cancel”.",
    },
    "twofa_too_many": {
        "ru": "🚫 Слишком много неверных попыток PIN — ввод кода заблокирован на {min} мин "
        "(fail-closed). Черновик сохранён; админы уведомлены.",
        "en": "🚫 Too many wrong PIN attempts — code entry is locked for {min} min "
        "(fail-closed). The draft is kept; admins were notified.",
    },
    "twofa_locked": {
        "ru": "🚫 Ввод PIN временно заблокирован после серии неверных попыток. "
        "Подожди ещё {min} мин и повтори ✅.",
        "en": "🚫 PIN entry is temporarily locked after repeated wrong attempts. "
        "Wait {min} more min and tap ✅ again.",
    },
    "twofa_aborted": {
        "ru": "↩️ Подтверждение прервано. Черновик сохранён — нажми ✅ ещё раз, чтобы повторить.",
        "en": "↩️ Confirmation aborted. The draft is kept — tap ✅ again to retry.",
    },
    "twofa_stale": {
        "ru": "Ожидание кода истекло. Нажми ✅ на нужном черновике заново.",
        "en": "Code wait expired. Tap ✅ on the draft again.",
    },
    "twofa_not_configured": {
        "ru": "🔒 2FA включён, но PIN не задан — опасная операция заблокирована (fail-closed). "
        "Задай TWO_FACTOR_PIN в конфиге или выключи TWO_FACTOR_ENABLED.",
        "en": "🔒 2FA is on but no PIN is set — the dangerous operation is blocked (fail-closed). "
        "Set TWO_FACTOR_PIN in config or turn TWO_FACTOR_ENABLED off.",
    },
    "twofa_need_button": {
        "ru": "🔐 Подтверди операцию кнопкой ✅ (нужен запрос PIN).",
        "en": "🔐 Confirm via the ✅ button (a PIN prompt is required).",
    },
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
    # — 3.1: выбор периода во всех отчётных командах —
    "period_pick_audit": {
        "ru": "🩺 За какой период аудит?",
        "en": "🩺 For what period should I audit?",
    },
    "period_pick_status": {
        "ru": "📊 За какой период статистика?",
        "en": "📊 For what period the stats?",
    },
    "period_pick_bids": {
        "ru": "📈 За какой период смотреть ставки?",
        "en": "📈 For what period should I review bids?",
    },
    "period_pick_searchterms": {
        "ru": "🚫 За какой период поисковые запросы?",
        "en": "🚫 For what period the search terms?",
    },
    "period_pick_mcc": {
        "ru": "🏢 За какой период сводка по всем аккаунтам?",
        "en": "🏢 For what period the all-accounts summary?",
    },
    "period_custom_prompt": {
        "ru": (
            "📅 Напиши период текстом — например: «вчера», «прошлая неделя», "
            "«с 1 по 15 июня», «14.06-30.06», «2026-06-01 2026-06-15»."
        ),
        "en": (
            "📅 Type the period — e.g. “yesterday”, “last week”, "
            "“june 1-15”, “14.06-30.06”, “2026-06-01 2026-06-15”."
        ),
    },
    # — §8/§9: пикер отчётов (аккаунт → кампания → период) —
    "report_pick_account": {
        "ru": "🏢 По какому аккаунту отчёт?",
        "en": "🏢 Which account should I report on?",
    },
    "status_pick_account": {
        "ru": "🏢 По какому аккаунту статистика?",
        "en": "🏢 Which account's stats?",
    },
    "advise_pick_account": {
        "ru": "🏢 По какому аккаунту рекомендации?",
        "en": "🏢 Which account should I advise on?",
    },
    "setacct_pick_account": {
        "ru": "🔄 Выбери активный аккаунт для отчётов/ключей (сохранится для этого чата):",
        "en": "🔄 Pick the active account for reports/keywords (saved for this chat):",
    },
    "pick_live_account_first": {
        "ru": "⚠️ У бота несколько живых аккаунтов, а активный не выбран — не угадываю, "
        "чтобы не показать не те деньги. Выбери аккаунт:",
        "en": "⚠️ Several live accounts are visible and none is active — I won't guess "
        "whose money to show. Pick an account:",
    },
    # AD.3: перед МУТАЦИЕЙ активный аккаунт не закреплён + живых несколько → заставляем выбрать
    # (чтобы не изменить не тот аккаунт — это чужие деньги на живых кампаниях).
    "pick_account_before_mutation": {
        "ru": "⚠️ Активный аккаунт не выбран, а живых несколько. Изменение не того аккаунта — "
        "чужие деньги. Выбери аккаунт для этой правки:",
        "en": "⚠️ No active account is set and several are live. Changing the wrong account means "
        "someone's real money. Pick the account for this change:",
    },
    "campaigns_pick_account": {
        "ru": "🏢 Кампании какого аккаунта показать?",
        "en": "🏢 Which account's campaigns?",
    },
    "service_menu_title": {
        "ru": "⚙️ <b>Сервис / Аккаунты</b> — смена аккаунта, доступы и диагностика:",
        "en": "⚙️ <b>Service / Accounts</b> — switch account, access and diagnostics:",
    },
    "live_account_hint": {
        "ru": "💡 Сейчас активен тестовый аккаунт (черновик) — данных мало. Выбери живой: "
        "/account &lt;id&gt; · /accounts (или ⚙️ Сервис в «➕ Ещё»).",
        "en": "💡 The test (draft) account is active — little data. Pick a live one: "
        "/account &lt;id&gt; · /accounts (or ⚙️ Service in “➕ More”).",
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
    # D4: пикер кампаний для /pause · /resume без аргумента (отфильтровано по статусу)
    # D9: нераспознанная слэш-команда → подсказка (не уводим в LLM)
    "unknown_command": {
        "ru": (
            "🤔 Не знаю команду <code>{cmd}</code>.\n"
            "Полный список — /help. Частое: /report · /campaigns · /keywords · /newcampaign · "
            "/addkeys · /pause · /resume.\n"
            "Или просто напишите задачу словами — я пойму без команды."
        ),
        "en": (
            "🤔 I don't know the command <code>{cmd}</code>.\n"
            "Full list — /help. Common: /report · /campaigns · /keywords · /newcampaign · "
            "/addkeys · /pause · /resume.\n"
            "Or just describe the task in plain words — no command needed."
        ),
    },
    "slash_pause_pick": {
        "ru": "⏸ Какую кампанию приостановить? Выберите активную из списка:",
        "en": "⏸ Which campaign to pause? Pick an active one from the list:",
    },
    "slash_resume_pick": {
        "ru": "▶️ Какую кампанию возобновить? Выберите приостановленную из списка:",
        "en": "▶️ Which campaign to resume? Pick a paused one from the list:",
    },
    # N1.4: опечатка в имени кампании → подсказка ТОЧНЫХ имён кнопками (никогда не исполняем на
    # угаданном имени — выбор всегда за оператором, дальше обычный confirm-гейт).
    "campaign_typo_suggest": {
        "ru": "🔎 Кампания «{name}» не найдена. Возможно, вы имели в виду:",
        "en": "🔎 Campaign “{name}” not found. Did you mean:",
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
            "/addkeys — add keywords to a campaign (your file/link/text, via confirmation)\n"
            "/searchterms — wasteful search terms (clicks, no conversions) → negatives (via confirmation)\n"
            "/rsa — generate ad copy (RSA), element-by-element confirm (created paused)\n\n"
            "<b>📊 Reports</b>\n"
            "/status — quick stats (30 days)\n"
            "/report [7|30|90|MTD | YYYY-MM-DD [YYYY-MM-DD]] — period summary (default 30 days)\n"
            "/export [period] — deep report .xlsx · /sheets [period] — in Google Sheets (link)\n"
            "/mysheets — my Google Sheets: links to the reports and keyword sheets I created\n"
            "/mcc [period] — summary across all MCC child accounts (per-currency subtotals)\n"
            "/quota — daily Google Ads API operation quota\n"
            "/advise [optimize|keywords|rsa|structure] — 💡 recommendations from LIVE metrics "
            "(spend/clicks/conversions). Multiple accounts — a picker. Empty/test account has no "
            "advice (pick a working one: /account). Suggestions only — I change nothing myself\n"
            "/audit [period] — 🩺 account health audit: score 0-100 + where money leaks + what to "
            "fix first (safely, with one «yes»). Next to it — Google's own optimization score\n"
            "/bids [period] — 📈 bid opportunities: which keywords to raise and how far "
            "(Google position estimates + bid simulator), biggest forecast conversion gain first\n"
            "/competitors — 🥊 who stands next to you in the auction: send the Auction insights CSV "
            "from Google Ads (the API doesn't return competitor names — only the file), and I'll "
            "compare it with the previous import\n"
            "/target &lt;CPA&gt; — account target CPA (unlocks pausing pricey campaigns in /audit: CPA ≥ 3× target)\n"
            "/alerts — anomaly alert thresholds (spend spike / conversions drop)\n\n"
            "<b>ℹ️ Clients</b>\n"
            "/clients — knowledge base: client profile as text + site crawling → relevant generation\n"
            "/client &lt;id&gt; — client card by account id\n"
            "/crawl [url] — crawl a client's site (from the argument or the profile); summary when done\n\n"
            "<b>⚙️ Settings and service</b>\n"
            "/account &lt;id&gt; | reset — read account for reports/keywords (default Draft)\n"
            "/accounts — my accessible accounts · /whoami — my chat_id and access mode\n"
            "/refresh — refresh the account list and caches without a restart\n"
            "/model — choose the AI model (OpenRouter) · /balance — AI budget: balance and spend\n"
            "/lang — interface language (RU/EN)\n"
            "/journal — change journal · /diag — error journal\n"
            "/reportbug — 🐞 report a bug (I'll pass it to the admin)\n"
            "/cancel — cancel the current draft\n\n"
            "<b>👤 For the admin (ADMIN_CHAT_IDS)</b>\n"
            "/adduser &lt;chat_id&gt; — let an operator use the bot (no restart) + pick accounts\n"
            "/removeuser &lt;chat_id&gt; — revoke access · /users — list operators\n"
            "/grant &lt;chat_id&gt; &lt;id&gt; · /revoke &lt;chat_id&gt; &lt;id&gt; — per-account read access\n"
            "/addadmin &lt;chat_id&gt; · /removeadmin &lt;chat_id&gt; — admin role, no restart · "
            "/admins — list\n"
            "/bugs — bug-report queue (triage) · /mutready &lt;id&gt; — mutation readiness\n\n"
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
    "kw_metrics_test_note": {
        "ru": (
            "ℹ️ Живой аккаунт для метрик не выбран — считаю на тест-аккаунте, где Keyword Planner "
            "возвращает пустые объёмы/CPC (мало идей, сортировка не работает). Выбери живой "
            "аккаунт командой /account — и подбор станет полным и отсортированным."
        ),
        "en": (
            "ℹ️ No live account selected for metrics — using the test account, where Keyword "
            "Planner returns empty volumes/CPC (few ideas, sorting won't work). Pick a live "
            "account with /account for full, sorted results."
        ),
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
    # D3: пикер кампаний /addkeys (текст-ввод названия остаётся фолбэком)
    "kw_add_pick_campaign_list": {
        "ru": "В какую кампанию добавить ключи? Выберите из списка — или пришлите название текстом.",
        "en": "Which campaign to add keywords to? Pick from the list — or send the name as text.",
    },
    # P3 (фидбэк заказчика 2026-07-06): вместо кнопки под отчётом — отдельный вход /addkeys.
    "kw_addkeys_hint": {
        "ru": (
            "➕ Добавить ключи в кампанию (свои файлом/ссылкой/текстом или из этого списка): "
            "/addkeys или меню «➕ Ещё»."
        ),
        "en": (
            "➕ Add keywords to a campaign (your own file/link/text or from this list): "
            "/addkeys or the “➕ More” menu."
        ),
    },
    "kw_add_send_list": {
        "ru": (
            "Пришлите список ключей для «{camp}» любым способом:\n"
            "• файлом <b>xlsx / csv / txt</b> (ключи в первой колонке/по строкам);\n"
            "• ссылкой на <b>Google Sheets</b> (ключи в колонке A; таблица должна быть доступна "
            "по ссылке или расшарена аккаунту бота);\n"
            "• простым текстом — по одному в строке или через запятую."
        ),
        "en": (
            "Send the keyword list for “{camp}” in any form:\n"
            "• an <b>xlsx / csv / txt</b> file (keywords in the first column/lines);\n"
            "• a <b>Google Sheets</b> link (keywords in column A; the sheet must be link-accessible "
            "or shared with the bot's account);\n"
            "• plain text — one per line or comma-separated."
        ),
    },
    "kw_add_sheet_read_failed": {
        "ru": (
            "⚠️ Не смог прочитать таблицу: {err}\n"
            "Откройте доступ по ссылке (или расшарьте аккаунту бота) и пришлите ссылку ещё раз — "
            "либо пришлите ключи файлом/текстом."
        ),
        "en": (
            "⚠️ Couldn't read the sheet: {err}\n"
            "Enable link access (or share it with the bot's account) and resend the link — "
            "or send the keywords as a file/text."
        ),
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
    # §7: /searchterms — «мусорные» поисковые запросы → минус-слова (за confirm-гейтом)
    "searchterms_loading": {
        "ru": "🔎 Читаю отчёт по поисковым запросам…",
        "en": "🔎 Reading the search-terms report…",
    },
    "searchterms_none": {
        "ru": "✅ «Мусорных» запросов (клики без конверсий) за период не найдено.",
        "en": "✅ No wasteful search terms (clicks without conversions) in this period.",
    },
    "err_searchterms": {
        "ru": "⚠️ Не удалось получить поисковые запросы: {err}",
        "en": "⚠️ Couldn't fetch search terms: {err}",
    },
    "searchterms_stale": {
        "ru": "Список устарел — запусти /searchterms заново.",
        "en": "The list is stale — run /searchterms again.",
    },
    "searchterms_cancel_btn": {"ru": "✖ Закрыть", "en": "✖ Close"},
    # 3.2а: батч минус-слов чекбоксами — тип соответствия, уровень, «Минусовать выбранные»
    "searchterms_mt_btn": {"ru": "Тип: {mt} ▸", "en": "Match: {mt} ▸"},
    "searchterms_lvl_btn": {"ru": "Куда: {lvl} ▸", "en": "Level: {lvl} ▸"},
    "searchterms_lvl_campaign": {"ru": "кампания", "en": "campaign"},
    "searchterms_lvl_adgroup": {"ru": "группа объявлений", "en": "ad group"},
    "searchterms_lvl_shared": {"ru": "общий список", "en": "shared list"},
    "searchterms_apply_btn": {
        "ru": "➖ Минусовать выбранные ({n})",
        "en": "➖ Add selected to negatives ({n})",
    },
    "searchterms_none_selected": {
        "ru": "Сначала отметь запросы галочками — тапни по строкам списка.",
        "en": "Select terms first — tap the list rows to tick them.",
    },
    "searchterms_ss_btn": {"ru": "📋 Список: {name}", "en": "📋 List: {name}"},
    "searchterms_ss_new_btn": {"ru": "➕ Новый: «{name}»", "en": "➕ New: “{name}”"},
    "searchterms_ss_back_btn": {"ru": "↩️ Назад", "en": "↩️ Back"},
    "searchterms_ss_default_name": {
        "ru": "Минус-слова из поисковых запросов",
        "en": "Negatives from search terms",
    },
    "searchterms_ss_pick_hint": {
        "ru": "Выбери общий список минус-слов",
        "en": "Pick a shared negative list",
    },
    "err_searchterms_ss": {
        "ru": "⚠️ Не удалось получить общие списки минус-слов: {err}",
        "en": "⚠️ Couldn't fetch shared negative lists: {err}",
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
    "mcc_manager_failed": {
        "ru": "⚠️ MCC <code>{mid}</code>: сводка недоступна (см. /diag).",
        "en": "⚠️ MCC <code>{mid}</code>: summary unavailable (see /diag).",
    },
    "geo_read_failed": {
        "ru": "<i>Текущее гео прочитать не удалось — изменение всё равно доступно.</i>",
        "en": "<i>Couldn't read the current geo — you can still change it.</i>",
    },
    "geo_stale": {
        "ru": "Сессия гео устарела — открой меню кампании в /campaigns заново.",
        "en": "The geo session expired — reopen the campaign menu via /campaigns.",
    },
    # — confirm-гейт / статусы —
    "proposal_pending": {
        "ru": texts.PROPOSAL_PENDING,
        "en": "📝 <b>Change draft</b>\n\n{summary}\n\nConfirm? <i>(draft valid for {ttl_h}h)</i>",
    },
    "photo_in_flow_hint": {
        "ru": (
            "📎 Сейчас идёт другой шаг — фото/видео здесь не ожидается. Заверши текущий флоу "
            "(или «✖ Отмена»), а для кампании из медиа просто пришли фото/видео из главного меню."
        ),
        "en": (
            "📎 Another step is in progress — a photo/video isn't expected here. Finish the "
            "current flow (or “✖ Cancel”), then send the media from the main menu."
        ),
    },
    # — 3F (§7): параметры keyword research —
    "kw_params_title": {
        "ru": (
            "⚙️ <b>Параметры подбора</b> — проверь и жми «🚀 Подобрать».\n"
            "<i>Тап по строке меняет значение.</i>"
        ),
        "en": (
            "⚙️ <b>Research parameters</b> — review and tap “🚀 Search ideas”.\n"
            "<i>Tap a row to change its value.</i>"
        ),
    },
    "kw_params_pick_geo": {
        "ru": "🌍 Какая страна (рынок) для подбора?",
        "en": "🌍 Which country (market) for the research?",
    },
    "kw_params_ask_geo": {
        "ru": "Напиши страну (например «Кения» или код <code>KE</code>).",
        "en": "Type a country (e.g. “Kenya” or code <code>KE</code>).",
    },
    "kw_params_geo_unknown": {
        "ru": "Не распознал страну. Попробуй название («Польша») или ISO-код (<code>PL</code>).",
        "en": "Couldn't recognize the country. Try a name (“Poland”) or ISO code (<code>PL</code>).",
    },
    "kw_params_pick_lang": {
        "ru": "🗣 На каком языке подбирать ключи?",
        "en": "🗣 Which language for the keywords?",
    },
    "kw_params_ask_lang": {
        "ru": "Напиши язык (например «немецкий» или код <code>de</code>).",
        "en": "Type a language (e.g. “German” or code <code>de</code>).",
    },
    "kw_params_lang_unknown": {
        "ru": "Не распознал язык. Попробуй название («немецкий») или ISO-код (<code>de</code>, "
        "<code>ja</code>, <code>pt</code>…).",
        "en": "Couldn't recognize the language. Try a name (“German”) or ISO code (<code>de</code>, "
        "<code>ja</code>, <code>pt</code>…).",
    },
    # — Ф1: /bids — возможности по ставкам (read-only; ставку меняет ОТДЕЛЬНАЯ команда через гейт) —
    "bids_loading": {
        "ru": "📈 Считаю возможности по ставкам (оценки позиций + симулятор Google)…",
        "en": "📈 Computing bid opportunities (position estimates + Google simulator)…",
    },
    "bids_none": {
        "ru": (
            "✅ Возможностей по ставкам не нашёл: ставки не ниже оценок Google, а окупаемого "
            "прироста симулятор не обещает.\n\nСовет «подними ставку» осмыслен только на ручной "
            "стратегии (MANUAL_CPC/ECPC) — на Smart Bidding ставку ключа решает Google."
        ),
        "en": (
            "✅ No bid opportunities: bids aren't below Google's estimates and the simulator "
            "promises no profitable gain.\n\nRaising a keyword bid only makes sense on manual "
            "bidding (MANUAL_CPC/ECPC) — under Smart Bidding Google sets the bid."
        ),
    },
    "bids_no_data": {  # ЧТЕНИЕ не удалось — это не «всё хорошо» (GR8: нет данных ≠ здорово)
        "ru": "⚠️ Слой ставок и позиций прочитать не удалось — данных для совета нет.",
        "en": "⚠️ Couldn't read the bid/position layer — no data to advise on.",
    },
    "err_bids": {
        "ru": "⚠️ Не удалось посчитать возможности по ставкам: {err}",
        "en": "⚠️ Couldn't compute bid opportunities: {err}",
    },
    # — Ф5б: /competitors — импорт CSV «Статистика аукционов» (имён конкурентов API не отдаёт) —
    "competitors_ask_file": {
        "ru": (
            "🥊 <b>Статистика аукционов</b> — кто стоит рядом в аукционе.\n\n"
            "Имена конкурентов Google отдаёт <b>только файлом</b> — через API их нет вовсе. "
            "Выгрузи отчёт и пришли его сюда:\n"
            "1. Google Ads → <b>Кампании</b> → <b>Статистика</b> → <b>Статистика аукционов</b>\n"
            "2. Кнопка выгрузки (↓) → <b>.csv</b>\n"
            "3. Пришли файл в этот чат\n\n"
            "Разбираю его <b>кодом</b>, в ИИ не отдаю."
        ),
        "en": (
            "🥊 <b>Auction insights</b> — who stands next to you in the auction.\n\n"
            "Google gives competitor names <b>only as a file</b> — the API doesn't return them at "
            "all. Export the report and send it here:\n"
            "1. Google Ads → <b>Campaigns</b> → <b>Insights</b> → <b>Auction insights</b>\n"
            "2. Download (↓) → <b>.csv</b>\n"
            "3. Send the file to this chat\n\n"
            "It's parsed by <b>code</b>, never sent to the AI."
        ),
    },
    "competitors_bad_file": {  # схему не узнали — говорим ЧТО прислать, а не «ошибка формата»
        "ru": (
            "⚠️ Это не похоже на выгрузку «Статистика аукционов»: не нашёл колонок "
            "«Домен отображаемого URL» и «Процент полученных показов».\n\n"
            "Нужен .csv именно из раздела <b>Статистика аукционов</b> (не отчёт по кампаниям)."
        ),
        "en": (
            "⚠️ This doesn't look like an Auction insights export: couldn't find the "
            "«Display URL domain» and «Impr. share» columns.\n\n"
            "A .csv from the <b>Auction insights</b> section is required (not a campaign report)."
        ),
    },
    "competitors_not_saved": {  # карточка показана, но БД не приняла — молчать об этом нельзя
        "ru": "⚠️ Показал, но сохранить срез не удалось — сравнить со следующим импортом не смогу.",
        "en": "⚠️ Shown, but the snapshot wasn't saved — I won't be able to compare it next time.",
    },
    "competitors_wait_file": {
        "ru": "Жду <b>файл</b> .csv со статистикой аукционов (или «✖ Отмена»).",
        "en": "Waiting for the auction insights <b>.csv file</b> (or «✖ Cancel»).",
    },
    "competitors_empty": {
        "ru": "Импортов ещё не было — пришли первый файл, и я запомню срез для сравнения.",
        "en": "No imports yet — send the first file and I'll remember it for comparison.",
    },
    "err_competitors": {
        "ru": "⚠️ Не удалось разобрать файл: {err}",
        "en": "⚠️ Couldn't parse the file: {err}",
    },
    # — advisor: /advise — рекомендации (advisory, read-only) —
    "advise_header": {
        "ru": "💡 Рекомендации · аккаунт {account} · {period}",
        "en": "💡 Recommendations · account {account} · {period}",
    },
    "advise_disclaimer": {
        # 3.2в: под частью советов ЕСТЬ кнопка «применить» — старый текст «сам я ничего не меняю,
        # дай команду» противоречил ей. Честно: кнопка лишь открывает подтверждение (confirm-гейт).
        "ru": (
            "Это подсказки — без твоего «да» ничего не меняется. Где есть кнопка «применить», "
            "она откроет подтверждение «было → станет»; остальное делается командой."
        ),
        "en": (
            "These are suggestions — nothing changes without your “yes”. Where there's an apply "
            "button, it opens a before → after confirmation; the rest is done by command."
        ),
    },
    "advise_empty": {
        "ru": "✅ Рекомендаций нет — по ключевым метрикам всё в норме за выбранный период.",
        "en": "✅ No recommendations — key metrics look fine for the selected period.",
    },
    "advise_empty_no_data": {
        "ru": (
            "🟡 Нет данных для советов: за период не было расхода/показов на этом аккаунте "
            "(похоже на пустой тест/черновик). Advisor анализирует ЖИВЫЕ метрики — выбери "
            "рабочий аккаунт: /account · /accounts."
        ),
        "en": (
            "🟡 No data to advise on: no spend/impressions on this account for the period "
            "(looks like an empty test/draft). Advisor analyses LIVE metrics — pick a working "
            "account: /account · /accounts."
        ),
    },
    "advise_error": {
        "ru": "⚠️ Не удалось собрать рекомендации: {err}",
        "en": "⚠️ Couldn't build recommendations: {err}",
    },
    "advise_feedback_thanks": {"ru": "Спасибо, учту 🙏", "en": "Thanks, noted 🙏"},
    "advise_dismissed": {
        "ru": "Скрыл. Такие советы по этой кампании буду показывать реже.",
        "en": "Hidden. I'll show this kind of advice for this campaign less often.",
    },
    # «Утренний экран действий» (проактивный дайджест advisor): топ по доле расхода под риском
    # по всем аккаунтам разом, каждая карточка — с кнопками 👍/👎/🙈/применить.
    "advise_digest_header": {
        "ru": "💡 Утренний экран действий — топ-{n} по деньгам-под-риском по всем аккаунтам:",
        "en": "💡 Morning action screen — top {n} by money-at-risk across all accounts:",
    },
    "advise_digest_item": {
        "ru": "🏢 {account}\n{body}",
        "en": "🏢 {account}\n{body}",
    },
    "advise_digest_share": {
        "ru": "≈{p}% расхода аккаунта",
        "en": "≈{p}% of account spend",
    },
    # 2.12 (C3): видимость пер-юзер дневного лимита LLM в /balance.
    "balance_llm_cap": {
        "ru": "🚦 Твой дневной лимит LLM-вызовов: {used}/{limit}.",
        "en": "🚦 Your daily LLM-call limit: {used}/{limit}.",
    },
    # 2.11 (§14): /myschedule — персональное расписание планового отчёта.
    "mysched_title": {
        "ru": "🗓 <b>Личное расписание отчёта.</b> Текущее: <code>{cur}</code>",
        "en": "🗓 <b>Personal report schedule.</b> Current: <code>{cur}</code>",
    },
    "mysched_global": {
        "ru": "глобальное (по умолчанию)",
        "en": "global (default)",
    },
    "mysched_ask_cron": {
        "ru": "Пришли crontab-строку, напр. <code>0 9 * * 1-5</code> (мин час день месяц день_недели).",
        "en": "Send a crontab string, e.g. <code>0 9 * * 1-5</code> (min hour day month weekday).",
    },
    "mysched_bad_cron": {
        "ru": "⚠️ Не похоже на валидный crontab. Пример: <code>0 9 * * 1</code> (пн 09:00).",
        "en": "⚠️ Doesn't look like a valid crontab. Example: <code>0 9 * * 1</code> (Mon 09:00).",
    },
    "mysched_saved": {
        "ru": "✅ Готово: <code>{cron}</code>. Уже действует (глобальная рассылка тебя пропустит).",
        "en": "✅ Done: <code>{cron}</code>. Already active (the global digest will skip you).",
    },
    "mysched_saved_restart": {
        "ru": "✅ Сохранено: <code>{cron}</code>. Применится после рестарта бота.",
        "en": "✅ Saved: <code>{cron}</code>. Takes effect after the bot restarts.",
    },
    "mysched_off_done": {
        "ru": "🔕 Личное расписание выключено — снова действует глобальное.",
        "en": "🔕 Personal schedule is off — the global one applies again.",
    },
    # 2.11 (§14): предложение авто-подстройки порогов аномалий (scheduler.run_threshold_tuning).
    "thr_tune_offer": {
        "ru": "🔧 По волатильности <b>{account}</b> за {weeks} нед. предлагаю персональные пороги "
        "алертов: рост расхода ≥{spike}% (сейчас {cur_spike}%), падение конверсий ≥{drop}% "
        "(сейчас {cur_drop}%), мин. расход {minspend} {currency} (сейчас {cur_minspend}).\n"
        "<i>Это настройка бота (как /alerts) — Google Ads не меняется.</i>",
        "en": "🔧 Based on <b>{account}</b> volatility over {weeks} weeks I suggest personal alert "
        "thresholds: spend spike ≥{spike}% (now {cur_spike}%), conversion drop ≥{drop}% "
        "(now {cur_drop}%), min spend {minspend} {currency} (now {cur_minspend}).\n"
        "<i>This is a bot setting (like /alerts) — Google Ads is not changed.</i>",
    },
    "thr_tune_accepted": {
        "ru": "✅ Готово — пороги для {account} обновлены (см. /alerts).",
        "en": "✅ Done — thresholds for {account} updated (see /alerts).",
    },
    "thr_tune_declined": {
        "ru": "Ок, оставляю как есть и не буду предлагать ~4 недели.",
        "en": "OK, keeping current — I won't suggest again for ~4 weeks.",
    },
    "thr_tune_stale": {
        "ru": "Предложение устарело — дождись следующего (или настрой /alerts вручную).",
        "en": "This suggestion is stale — wait for the next one (or set /alerts manually).",
    },
    # 2.7: /report без данных за период — внятный empty-state вместо «стены нулей».
    "report_empty_state": {
        "ru": "🟡 За выбранный период на этом аккаунте нет данных (показы/клики/расход = 0).\n"
        "Проверь даты периода или выбери рабочий аккаунт: /account · /accounts.",
        "en": "🟡 No data on this account for the selected period (impressions/clicks/cost = 0).\n"
        "Check the period dates or pick a working account: /account · /accounts.",
    },
    # 2.6: обрезка батча ключей до лимита схемы (n = agent.tools.schemas.ADD_KEYWORDS_MAX).
    "kw_add_truncated": {
        "ru": "Оставил первые {n} ключей (лимит одного добавления).",
        "en": "Kept the first {n} keywords (single-batch limit).",
    },
    # 2.5: /mutready — чек-лист готовности к включению мутаций (админ; бот конфиг НЕ меняет).
    "mutready_usage": {
        "ru": "Использование: /mutready &lt;id или имя&gt; · /mutready all — сводка по всем видимым (без аргумента — активный).",
        "en": "Usage: /mutready &lt;id or name&gt; · /mutready all — summary for all visible (no argument — the active account).",
    },
    # 2.4: cooldown /refresh (анти-спам API; admin-gate осознанно не ставим — read-only).
    "refresh_cooldown": {
        "ru": "⏳ /refresh уже выполнялся только что — подожди минуту.",
        "en": "⏳ /refresh just ran — wait a minute.",
    },
    # 2.3: явное чтение НЕАКТИВНЫХ дочерних (история CANCELED/SUSPENDED по прямому запросу).
    "account_inactive_note": {
        "ru": "😴 Аккаунт {name} в статусе {status} — данные исторические; часть чтений Google "
        "может отклонять для неактивных аккаунтов.",
        "en": "😴 Account {name} is {status} — data is historical; Google may reject some reads "
        "for inactive accounts.",
    },
    "account_inactive_read_failed": {
        "ru": "😴 Аккаунт {name} в статусе {status} — Google отклоняет API-чтение отменённых/"
        "приостановленных аккаунтов. Данные за прошлые периоды недоступны через API.",
        "en": "😴 Account {name} is {status} — Google rejects API reads for canceled/suspended "
        "accounts. Historical data is not available via the API.",
    },
    # 2.2: deep-xlsx по всем дочерним MCC (кнопка «Все аккаунты» в пикере /export).
    "mcc_deep_preparing": {
        "ru": "⏳ Собираю глубокий отчёт по всем аккаунтам MCC — лист на аккаунт. "
        "Это может занять до пары минут…",
        "en": "⏳ Building the deep report for all MCC accounts — one sheet per account. "
        "This can take up to a couple of minutes…",
    },
    "mcc_deep_failed": {
        "ru": "⚠️ MCC {mid}: глубокий отчёт не собран — {err}",
        "en": "⚠️ MCC {mid}: deep report failed — {err}",
    },
    "mcc_deep_empty": {
        "ru": "🤷 Не нашлось ни одного аккаунта с данными для глубокого отчёта.",
        "en": "🤷 No accounts with data for the deep report.",
    },
    # 1.6: недельный бизнес-дайджест (/bizdigest, scheduler.run_business_digest).
    "bizdigest_header": {
        "ru": "📈 Недельный бизнес-дайджест ({period})",
        "en": "📈 Weekly business digest ({period})",
    },
    "bizdigest_recs_title": {
        "ru": "Топ-3 рекомендации:",
        "en": "Top-3 recommendations:",
    },
    "bizdigest_anomalies_title": {
        "ru": "Аномалии недели:",
        "en": "This week's anomalies:",
    },
    # 3.3 (2026-07-17): плановый дайджест — «что горит»/находки/применено/тихий режим/кнопка.
    "sched_digest_hot_title": {
        "ru": "🔥 Что горит:",
        "en": "🔥 Needs attention:",
    },
    "sched_digest_findings_title": {
        "ru": "Главное из аудита:",
        "en": "Top audit findings:",
    },
    "sched_digest_applied": {
        "ru": "🛠 За сутки применено изменений: {n}",
        "en": "🛠 Changes applied in the last 24h: {n}",
    },
    "sched_digest_quiet": {
        "ru": "😴 Без событий: {n} акк. — нет расходов, аномалий и изменений",
        "en": "😴 No events: {n} account(s) — no spend, anomalies, or changes",
    },
    "sched_digest_all_quiet": {
        "ru": "Тишина: {n} акк. без расходов, аномалий и изменений — показывать нечего.",
        "en": "All quiet: {n} account(s) with no spend, anomalies, or changes — nothing to show.",
    },
    "sched_digest_apply_now": {
        "ru": "⚡ Можно применить сейчас (кнопка откроет подтверждение «было → станет»):",
        "en": "⚡ Ready to apply (the button opens a “was → will be” confirmation):",
    },
    "sched_digest_auction_stale": {
        "ru": "🥊 Срезу аукционов уже {d} дн. — обнови выгрузку: /competitors",
        "en": "🥊 Auction insights snapshot is {d} days old — refresh it: /competitors",
    },
    "bizdigest_on": {
        "ru": "📈 Недельный бизнес-дайджест ВКЛючён — пришлю сводку по всем аккаунтам "
        "(расход/конверсии/CPA неделя к неделе + топ-3 совета) по расписанию. Выключить: /bizdigest.",
        "en": "📈 Weekly business digest is ON — you'll get a cross-account summary "
        "(spend/conversions/CPA week-over-week + top-3 tips) on schedule. Turn off: /bizdigest.",
    },
    "bizdigest_off": {
        "ru": "🔕 Недельный бизнес-дайджест выключен. Включить снова: /bizdigest.",
        "en": "🔕 Weekly business digest is off. Turn on again: /bizdigest.",
    },
    "advise_auto_on": {
        "ru": "🔔 Авто-советы включены — буду присылать рекомендации по расписанию.",
        "en": "🔔 Auto-advice on — I'll send recommendations on schedule.",
    },
    "advise_auto_off": {
        "ru": "🔕 Авто-советы выключены — только по команде /advise.",
        "en": "🔕 Auto-advice off — only on /advise.",
    },
    "advise_outcome_improved": {
        "ru": "✅ Твоё изменение по кампании «{campaign}» сработало — метрики улучшились после совета.",
        "en": "✅ Your change on campaign “{campaign}” paid off — metrics improved after the advice.",
    },
    "advise_outcome_worse": {
        "ru": "⚠️ После изменения по кампании «{campaign}» метрики просели — возможно, стоит "
        "пересмотреть или откатить.",
        "en": "⚠️ After the change on campaign “{campaign}” metrics dropped — consider reviewing "
        "or reverting.",
    },
    "advise_apply_btn_pause": {"ru": "⏸ Поставить на паузу", "en": "⏸ Pause it"},
    "advise_apply_btn_negatives": {"ru": "➖ В минус-слова", "en": "➖ Add as negatives"},
    "advise_apply_btn_display_off": {"ru": "🚫 Выключить КМС", "en": "🚫 Turn off Display"},
    "advise_apply_btn_geo_presence": {"ru": "📍 Только присутствие", "en": "📍 Presence only"},
    "advise_apply_btn_remove_negative": {
        "ru": "🧹 Снять минус-слово",
        "en": "🧹 Remove the negative",
    },
    "advise_apply_stale": {
        "ru": "Рекомендация устарела — запусти /advise заново.",
        "en": "Recommendation expired — run /advise again.",
    },
    "advise_apply_not_actionable": {
        "ru": "Этот совет применяется только вручную командой (деньги/ставки — не в один тап).",
        "en": "This advice is applied only via a manual command (money/bids are never one-tap).",
    },
    "advise_negatives_hint": {
        "ru": "💡 Возможные минус-слова (советую, сам НЕ добавляю): {words}",
        "en": "💡 Possible negative keywords (advice, I do NOT add them myself): {words}",
    },
    # advise_rec_* (6 ключей) удалены 2026-07-13 вместе с детекторами advisor: текст рекомендации
    # теперь ОДИН — audit.render.finding_text (RU/EN живут в audit/render.py), см. advisor/from_findings.py.
    # — 3H (M10): /alerts — пороги аномалий —
    "alerts_saved": {"ru": "✅ Порог сохранён.", "en": "✅ Threshold saved."},
    "alerts_reset_done": {"ru": "↩️ Пороги сброшены к дефолтам.", "en": "↩️ Thresholds reset."},
    "alerts_ask_value": {
        "ru": "Пришли число: процент (напр. <code>35</code>) или мин. расход.",
        "en": "Send a number: percent (e.g. <code>35</code>) or min spend.",
    },
    "alerts_bad_value": {
        "ru": "Не похоже на допустимое число (проценты 1–1000). Попробуй ещё раз.",
        "en": "Doesn't look like a valid number (percent 1–1000). Try again.",
    },
    "more_menu_title": {
        "ru": "⚙️ <b>Ещё</b> — модель ИИ, сводки, конкуренты, квоты и справка:",
        "en": "⚙️ <b>More</b> — AI model, summaries, competitors, quotas and help:",
    },
    "create_menu_title": {
        "ru": "➕ <b>Создать</b> — кампании, тексты RSA, подбор ключей, шаблоны:",
        "en": "➕ <b>Create</b> — campaigns, RSA copy, keyword research, templates:",
    },
    "reports_menu_title": {
        "ru": "📄 <b>Отчёты</b> — статистика и выгрузки в Excel / Google Sheets:",
        "en": "📄 <b>Reports</b> — stats and exports to Excel / Google Sheets:",
    },
    "cc_edit_not_understood": {
        "ru": (
            "🤷 Не понял правку. Примеры: «поставь бюджет 60», «добавь город Найроби», "
            "«смени „Быстро и надёжно“ на „Надёжно и быстро“»."
        ),
        "en": (
            "🤷 Couldn't parse the edit. Examples: “set budget 60”, “add city Nairobi”, "
            "“change ‘Fast and reliable’ to ‘Reliable and fast’”."
        ),
    },
    # W5: диалог выхода из визарда с накопленной работой (вместо безвозвратного abandon).
    "cc_exit_confirm": {
        "ru": (
            "Выйти из создания кампании? Черновик на шаге {step}/7 можно сохранить и продолжить "
            "позже (хранится {ttl_h} ч) или удалить."
        ),
        "en": (
            "Leave campaign creation? The draft at step {step}/7 can be kept for later "
            "(stored {ttl_h} h) or deleted."
        ),
    },
    "cc_draft_kept": {
        "ru": (
            "💾 Черновик сохранён (шаг {step}/7). Вернуться: «➕ Создание кампании» "
            "или /newcampaign."
        ),
        "en": '💾 Draft kept (step {step}/7). Return via "➕ Create campaign" or /newcampaign.',
    },
    # B2: TTL-жизнь черновика больше не молчаливая — предупреждение до и уведомление после.
    "cc_draft_expiring": {
        "ru": (
            "⏳ Черновик кампании (шаг {step}/7) будет удалён примерно через {left_h} ч "
            "без активности. Продолжить: /newcampaign → «▶️ Продолжить»."
        ),
        "en": (
            "⏳ Your campaign draft (step {step}/7) will be removed in about {left_h} h "
            "of inactivity. Resume: /newcampaign → “▶️ Continue”."
        ),
    },
    "cc_draft_expired": {
        "ru": (
            "🗑 Черновик кампании (шаг {step}/7) удалён по истечении срока хранения. "
            "Начать заново: /newcampaign."
        ),
        "en": (
            "🗑 Your campaign draft (step {step}/7) was removed after its storage period. "
            "Start again: /newcampaign."
        ),
    },
    # W4: «Вперёд ›» по старому сообщению, когда идти уже некуда (черновик на максимуме маршрута).
    "cc_no_forward": {
        "ru": "Дальше пока некуда — этот шаг ещё не пройден.",
        "en": "Nothing ahead yet — that step hasn't been reached.",
    },
    # Этап 1: правка настроек не распознана — честный отказ вместо молчаливого ре-рендера той же
    # карточки (живой тест 2026-07-06: «максимальная цена за клик 75 йен» выглядела применённой).
    "cc_settings_edit_not_understood": {
        "ru": (
            "🤷 Не понял, что поменять в настройках. Можно изменить: бюджет, макс. цену за клик "
            "(CPC), гео, язык, название, стратегию/цель, сети, расписание, даты, валюту. "
            "Например: <i>поставь максимальную цену за клик 75</i>."
        ),
        "en": (
            "🤷 Couldn't parse the settings edit. You can change: budget, max CPC, geo, language, "
            "name, strategy/goal, networks, schedule, dates, currency. "
            "E.g. <i>set max CPC to 75</i>."
        ),
    },
    # — 3A: кнопка меню во время визарда (мягкое сворачивание без потери работы) —
    "cc_wizard_suspended": {
        "ru": (
            "⏸ Черновик кампании сохранён (шаг {step}/7) — вернуться: "
            "«➕ Создание кампании» → «▶️ Продолжить»."
        ),
        "en": (
            "⏸ Campaign draft saved (step {step}/7) — to resume: "
            "“➕ Create campaign” → “▶️ Continue”."
        ),
    },
    "cli_buf_flushed": {
        "ru": "💾 Накопленный текст профиля оформлен черновиком — подтверди его выше (✅/❌).",
        "en": "💾 The accumulated profile text became a draft — confirm it above (✅/❌).",
    },
    # — 2C: гранты аккаунтов (/grant /revoke /accounts /whoami) —
    "admin_only": {
        "ru": "⛔ Команда доступна только администратору бота (ADMIN_CHAT_IDS).",
        "en": "⛔ This command is for the bot administrator only (ADMIN_CHAT_IDS).",
    },
    "grant_bad_args": {
        "ru": "Формат: <code>/grant &lt;chat_id&gt; &lt;customer_id&gt;</code> — выдать доступ к аккаунту.",
        "en": "Usage: <code>/grant &lt;chat_id&gt; &lt;customer_id&gt;</code> — grant account access.",
    },
    "grant_unknown_account": {
        "ru": (
            "Аккаунт <code>{cid}</code> не входит в читаемые ботом (read-замок). Проверь id или "
            "выполни /refresh (пере-обход MCC)."
        ),
        "en": (
            "Account <code>{cid}</code> is not readable by the bot (read lock). Check the id or "
            "run /refresh (re-discover MCC)."
        ),
    },
    "grant_ok": {
        "ru": "✅ Грант выдан: chat <code>{chat}</code> → аккаунт <code>{cid}</code> (чтение).",
        "en": "✅ Granted: chat <code>{chat}</code> → account <code>{cid}</code> (read).",
    },
    "grant_enforcement_note": {
        "ru": (
            "⚠️ Это ПЕРВЫЙ грант — включён режим пер-пользовательской изоляции: не-Draft аккаунты "
            "теперь видны операторам только по грантам (/whoami покажет режим)."
        ),
        "en": (
            "⚠️ This is the FIRST grant — per-user isolation is now enforced: non-Draft accounts "
            "are visible to operators only via grants (/whoami shows the mode)."
        ),
    },
    "revoke_ok": {
        "ru": "✅ Грант снят: chat <code>{chat}</code> ✕ аккаунт <code>{cid}</code>.",
        "en": "✅ Revoked: chat <code>{chat}</code> ✕ account <code>{cid}</code>.",
    },
    # — P0-A: рантайм-управление операторами (/adduser /removeuser /users) —
    "adduser_bad_args": {
        "ru": (
            "Формат: <code>/adduser &lt;chat_id&gt; [заметка]</code> — открыть оператору доступ к боту.\n"
            "chat_id оператор узнаёт командой /whoami."
        ),
        "en": (
            "Usage: <code>/adduser &lt;chat_id&gt; [note]</code> — let an operator use the bot.\n"
            "The operator learns their chat_id via /whoami."
        ),
    },
    "adduser_added": {
        "ru": (
            "✅ Оператор <code>{chat}</code> добавлен в whitelist — может пользоваться ботом (без "
            "рестарта).\nКакие аккаунты открыть ему на <b>чтение</b>? Любая мутация — только через "
            "подтверждение «да» (грант чтения её не открывает)."
        ),
        "en": (
            "✅ Operator <code>{chat}</code> added to the whitelist — can use the bot now (no "
            "restart).\nWhich accounts to open for <b>reading</b>? Any mutation only runs after "
            "explicit confirmation (a read grant doesn't enable mutations)."
        ),
    },
    "adduser_exists": {
        "ru": (
            "Оператор <code>{chat}</code> уже в whitelist. Можно донастроить доступ к аккаунтам:"
        ),
        "en": "Operator <code>{chat}</code> is already whitelisted. You can adjust account access:",
    },
    "adduser_btn_all": {"ru": "✅ Все аккаунты", "en": "✅ All accounts"},
    "adduser_btn_pick": {"ru": "🎯 Выбрать аккаунты", "en": "🎯 Pick accounts"},
    "adduser_btn_none": {"ru": "🚫 Только доступ к боту", "en": "🚫 Bot access only"},
    "adduser_all_done": {
        "ru": "✅ Оператору <code>{chat}</code> открыто чтение всех аккаунтов ({n}). Мутации — только через подтверждение «да».",
        "en": "✅ Operator <code>{chat}</code> can now read all accounts ({n}). Mutations run only after explicit confirmation.",
    },
    "adduser_none_done": {
        "ru": (
            "Оператор <code>{chat}</code> может пользоваться ботом; аккаунты пока не открыты "
            "(доступен только Draft). Выдать позже: /grant."
        ),
        "en": (
            "Operator <code>{chat}</code> can use the bot; no accounts opened yet (Draft only). "
            "Grant later with /grant."
        ),
    },
    "adduser_pick_title": {
        "ru": (
            "🎯 Выбор аккаунтов для оператора <code>{chat}</code> (тап — открыть/закрыть чтение). "
            "✅ = доступен. По готовности — «Готово»."
        ),
        "en": (
            "🎯 Pick accounts for operator <code>{chat}</code> (tap to toggle read). "
            "✅ = allowed. Press “Done” when finished."
        ),
    },
    "adduser_pick_empty": {
        "ru": "Нет обнаруженных дочерних аккаунтов. Выполни /refresh (пере-обход MCC) и повтори.",
        "en": "No discovered child accounts. Run /refresh (re-discover MCC) and retry.",
    },
    "adduser_btn_done": {"ru": "✔️ Готово", "en": "✔️ Done"},
    "adduser_pick_done": {
        "ru": "✅ Готово. Оператору <code>{chat}</code> открыто аккаунтов: {n}.",
        "en": "✅ Done. Operator <code>{chat}</code> now has access to {n} account(s).",
    },
    "removeuser_bad_args": {
        "ru": "Формат: <code>/removeuser &lt;chat_id&gt;</code> — закрыть оператору доступ к боту.",
        "en": "Usage: <code>/removeuser &lt;chat_id&gt;</code> — revoke an operator's bot access.",
    },
    "removeuser_env": {
        "ru": (
            "⚠️ chat <code>{chat}</code> прописан в .env (TELEGRAM_WHITELIST_CHAT_IDS) — из БД убрать "
            "нечего. Убрать env-оператора можно только правкой .env + рестартом."
        ),
        "en": (
            "⚠️ chat <code>{chat}</code> is in .env (TELEGRAM_WHITELIST_CHAT_IDS) — nothing to remove "
            "from the DB. Env operators can only be removed by editing .env + restart."
        ),
    },
    "removeuser_ok": {
        "ru": "✅ Оператор <code>{chat}</code> удалён из whitelist (и снят с грантов аккаунтов).",
        "en": "✅ Operator <code>{chat}</code> removed from the whitelist (account grants revoked).",
    },
    "users_title": {
        "ru": "👥 <b>Операторы бота</b>\nEnv (.env): {env}\nБД (/adduser): {db}",
        "en": "👥 <b>Bot operators</b>\nEnv (.env): {env}\nDB (/adduser): {db}",
    },
    "users_empty_db": {"ru": "— нет —", "en": "— none —"},
    # P4 (живой тест 2026-07-06): рантайм-админка без рестарта VPS (env ∪ таблица admins).
    "addadmin_bad_args": {
        "ru": "Формат: <code>/addadmin &lt;chat_id&gt; [заметка]</code>. Chat_id виден в /whoami.",
        "en": "Usage: <code>/addadmin &lt;chat_id&gt; [note]</code>. Chat_id is shown by /whoami.",
    },
    "addadmin_added": {
        "ru": (
            "✅ <code>{chat}</code> теперь админ (без рестарта): /grant, /adduser, /addadmin, "
            "чтение всех доступных аккаунтов. Любая мутация — только через подтверждение «да»."
        ),
        "en": (
            "✅ <code>{chat}</code> is now an admin (no restart): /grant, /adduser, /addadmin, "
            "read access to all available accounts. Any mutation runs only after explicit confirmation."
        ),
    },
    "addadmin_exists": {
        "ru": "ℹ️ <code>{chat}</code> уже админ.",
        "en": "ℹ️ <code>{chat}</code> is already an admin.",
    },
    "removeadmin_bad_args": {
        "ru": "Формат: <code>/removeadmin &lt;chat_id&gt;</code>.",
        "en": "Usage: <code>/removeadmin &lt;chat_id&gt;</code>.",
    },
    "removeadmin_env": {
        "ru": (
            "⚠️ <code>{chat}</code> — env-админ (ADMIN_CHAT_IDS): в рантайме неснимаем, "
            "только правкой .env и рестартом."
        ),
        "en": (
            "⚠️ <code>{chat}</code> is an env admin (ADMIN_CHAT_IDS): can't be removed at "
            "runtime — edit .env and restart."
        ),
    },
    "removeadmin_self": {
        "ru": "⚠️ Нельзя снять админку с СЕБЯ (защита от самоблокировки).",
        "en": "⚠️ You can't remove YOUR OWN admin role (lockout protection).",
    },
    "removeadmin_last": {
        "ru": "⚠️ Это последний админ — снять нельзя (некому будет управлять доступом).",
        "en": "⚠️ That's the last admin — can't remove (no one would manage access).",
    },
    "removeadmin_ok": {
        "ru": "✅ Админка снята с <code>{chat}</code> (оператором в whitelist он остаётся).",
        "en": "✅ Admin role removed from <code>{chat}</code> (still a whitelisted operator).",
    },
    "admins_title": {
        "ru": "🛡 <b>Админы бота</b>\nEnv (.env, неснимаемые): {env}\nБД (/addadmin): {db}",
        "en": "🛡 <b>Bot admins</b>\nEnv (.env, irremovable): {env}\nDB (/addadmin): {db}",
    },
    "accounts_title": {
        "ru": "🏢 <b>Твои аккаунты (чтение)</b> · {n}:",
        "en": "🏢 <b>Your accounts (read)</b> · {n}:",
    },
    "whoami_text": {
        "ru": (
            "🪪 <b>Ты</b>\n"
            "chat_id: <code>{chat}</code>\n"
            "Активный аккаунт чтения: <code>{active}</code>\n"
            "Режим изоляции: <code>{mode}</code> · enforcement: {enforced}\n"
            "Админ: {admin}"
        ),
        "en": (
            "🪪 <b>You</b>\n"
            "chat_id: <code>{chat}</code>\n"
            "Active read account: <code>{active}</code>\n"
            "Isolation mode: <code>{mode}</code> · enforcement: {enforced}\n"
            "Admin: {admin}"
        ),
    },
    "kw_partial_rejected": {
        "ru": (
            "⚠️ Часть позиций Google Ads отклонил: применено <b>{ok}</b>, отклонено <b>{bad}</b>. "
            "Причины (первые):"
        ),
        "en": (
            "⚠️ Google Ads rejected some items: applied <b>{ok}</b>, rejected <b>{bad}</b>. "
            "Reasons (first):"
        ),
    },
    "external_context_money_warn": {
        "ru": (
            "⚠️ <b>Черновик создан при наличии внешнего контента (файл/ссылка)</b> — сумма могла "
            "быть предложена этим контентом, а не тобой. Проверь цифры перед подтверждением."
        ),
        "en": (
            "⚠️ <b>This draft was created with external content present (file/link)</b> — the "
            "amount may have been suggested by that content, not you. Verify the numbers before "
            "confirming."
        ),
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
        "en": "⚠️ Failed to execute: {err}",
    },
    # Денежный путь: исход мутации НЕИЗВЕСТЕН (таймаут/INTERNAL/DEADLINE во время SDK — могла
    # примениться на сервере). НЕ «failed» — иначе повтор задвоил бы. Просим сверить перед повтором.
    "needs_review": {
        "ru": texts.NEEDS_REVIEW,
        "en": (
            "⚠️ <b>Outcome unknown.</b> Google Ads didn't respond in time — the change "
            "<b>may have applied</b>. Check the account in Google Ads <b>before retrying</b> "
            "(otherwise you may duplicate it): {err}"
        ),
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
    # C3: тексты алертов аномалий (scheduler.anomaly отдаёт kind+params, рендер per-recipient).
    "anomaly_spend_spike": {
        "ru": "📈 Расход вырос на {pct}% ({prev} → {now}{cur}).",
        "en": "📈 Spend up {pct}% ({prev} → {now}{cur}).",
    },
    "anomaly_conv_drop": {
        "ru": "📉 Конверсии упали на {pct}% ({prev} → {now}).",
        "en": "📉 Conversions down {pct}% ({prev} → {now}).",
    },
    "anomaly_spend_no_conv": {
        "ru": "⚠️ Расход {now}{cur} при нуле конверсий (было {prev_conv}).",
        "en": "⚠️ Spend {now}{cur} with zero conversions (was {prev_conv}).",
    },
    "camp_network_title": {
        "ru": (
            "🌐 Сети кампании «{camp}»\n\n"
            "Поисковые партнёры Google — показы на сайтах-партнёрах поиска. Рекомендуется "
            "ВЫКЛ (дефолт проекта); включайте только осознанно. КМС для Search-кампаний "
            "всегда выключена. Изменение — после подтверждения."
        ),
        "en": (
            "🌐 Networks of campaign “{camp}”\n\n"
            "Google search partners — ads on search partner sites. Recommended OFF "
            "(project default); enable only deliberately. Display network is always off "
            "for Search campaigns. The change applies after confirmation."
        ),
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
    # B2: у кампании есть группы, но ни одна не Search-стандартная (DSA/Display/Video/PMax) —
    # адаптивное поисковое объявление туда добавить нельзя (Google отвергнет).
    "rsa_not_search": {
        "ru": (
            "⚠️ В этой кампании нет стандартной поисковой группы объявлений — адаптивное "
            "поисковое объявление (RSA) сюда добавить нельзя. Выбери Search-кампанию или создай "
            "поисковую кампанию через ➕ Создание кампании."
        ),
        "en": (
            "⚠️ This campaign has no standard Search ad group — a Responsive Search Ad can't be "
            "added here. Pick a Search campaign or create one via ➕ Create campaign."
        ),
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
            "• <b>Video</b> — охватная видеокампания (CPM). ⚠️ создание Video по API требует "
            "allowlist Google на аккаунте — без него шаг создания упрётся в ограничение."
        ),
        "en": (
            "▶️ Video: <code>{vid}</code>\n"
            "Which campaign type should I build?\n"
            "• <b>Demand Gen</b> — YouTube/Discover/Gmail, best default for leads and sales.\n"
            "• <b>Video</b> — reach video campaign (CPM). ⚠️ creating Video via API needs Google "
            "allowlist on the account — without it the create step hits a restriction."
        ),
    },
    "video_allowlist_warn": {
        "ru": (
            "⚠️ Video-кампании создаются по API только с allowlist Google. Соберу черновик, но "
            "создание может упереться в ограничение — тогда выбери Demand Gen."
        ),
        "en": (
            "⚠️ Video campaigns are API-creatable only with Google allowlist. I'll build the draft, "
            "but creation may hit a restriction — pick Demand Gen if so."
        ),
    },
    # B4: Video выключен конфигом (аккаунт не в allowlist Google) — не ведём в гарантированный тупик,
    # а честно объясняем и оставляем на Demand Gen (рабочий путь из видео). Включается
    # GOOGLE_ADS_VIDEO_ENABLED=true, когда аккаунт добавлен в allowlist Google.
    "video_disabled_use_dg": {
        "ru": (
            "▶️ Video через API недоступно: создание VIDEO-кампаний Google разрешает только по "
            "allowlist аккаунта (иначе запрос отклоняется). Использую 🎯 Demand Gen — рабочий путь "
            "из того же видео. Продолжаем с Demand Gen."
        ),
        "en": (
            "▶️ Video via API is unavailable: Google allows creating VIDEO campaigns only for "
            "allowlisted accounts (otherwise the request is rejected). Using 🎯 Demand Gen — the "
            "working path from the same video. Continuing with Demand Gen."
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
            "🖼 Логотип для Demand Gen — <b>обязателен</b> (требование Google; лучше квадратный "
            "1:1): пришли <b>фото</b> логотипа."
        ),
        "en": (
            "🖼 Logo for Demand Gen — <b>required</b> (Google requirement; square 1:1 works "
            "best): send the logo as a <b>photo</b>."
        ),
    },
    "video_logo_required": {
        "ru": (
            "Для Demand Gen логотип обязателен — без него Google отклоняет создание кампании. "
            "Пришли фото логотипа (или «✖ Отмена»)."
        ),
        "en": (
            "A logo is required for Demand Gen — Google rejects campaign creation without it. "
            "Send the logo photo (or “✖ Cancel”)."
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
    "search_edit_bid_btn": {"ru": "✏️ Изменить ставку", "en": "✏️ Edit bid"},
    "search_ask_new_bid": {
        "ru": "Текущая max CPC-ставка: {cur:g}{sfx}. Пришли новое значение одним числом "
        "(в валюте аккаунта), например «0.35».",
        "en": "Current max CPC bid: {cur:g}{sfx}. Send a new value as a single number "
        "(in the account currency), e.g. “0.35”.",
    },
    "search_bid_bad": {
        "ru": "Не понял ставку. Пришли положительное число, например «0.35».",
        "en": "Couldn't read the bid. Send a positive number, e.g. “0.35”.",
    },
    "search_bid_stale": {
        "ru": "Черновик кампании устарел. Запусти /newsearch заново.",
        "en": "The campaign draft is stale. Start /newsearch again.",
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
    # cb_error («Ошибка: {kind}» с именем класса) удалён — P1-аудит 2026-07-06: менеджеру имена
    # исключений не показываем; см. bot.main._friendly_error (err_validate / err_unexpected+код).
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
    "report_preparing_docx": {"ru": "Готовлю .docx-отчёт…", "en": "Preparing the .docx report…"},
    "report_preparing_sheets": {
        "ru": "Готовлю Google Sheets-отчёт…",
        "en": "Preparing the Google Sheets report…",
    },
    "sheets_ready": {
        "ru": "✅ Google Sheets готов: {url}",
        "en": "✅ Google Sheets is ready: {url}",
    },
    # B3: таблица открыта anyone-with-link (settings.sheets_public_link) — финансовые данные видны
    # всем, у кого есть ссылка. Владелец может выключить SHEETS_PUBLIC_LINK (тогда таблица приватна).
    "sheets_public_warn": {
        "ru": "⚠️ Таблица доступна ВСЕМ по ссылке. Не пересылайте её посторонним.",
        "en": "⚠️ The sheet is accessible to ANYONE with the link. Don't share it with outsiders.",
    },
    # Владелец САМ выключил публичные ссылки (SHEETS_PUBLIC_LINK=false) — это не сбой, и писать
    # «не удалось открыть доступ» было бы враньём: доступ выдаётся вручную владельцем таблицы.
    "sheets_share_off_note": {
        "ru": (
            "🔒 Публичные ссылки выключены — таблица приватна. Доступ выдаёт владелец "
            "Google-аккаунта бота вручную (или включите SHEETS_PUBLIC_LINK)."
        ),
        "en": (
            "🔒 Public links are disabled — the sheet is private. The owner of the bot's Google "
            "account grants access manually (or enable SHEETS_PUBLIC_LINK)."
        ),
    },
    # P1 (живой тест 2026-07-06): не удалось открыть anyone-with-link — таблица осталась приватной.
    "sheets_share_failed_note": {
        "ru": (
            "⚠️ Не удалось открыть доступ по ссылке — откройте таблицу и нажмите "
            "«Request access», либо напишите администратору."
        ),
        "en": (
            "⚠️ Couldn't enable link sharing — open the sheet and press "
            "“Request access”, or contact the administrator."
        ),
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
    "diag_detail_not_found": {
        "ru": "🔍 Инцидент не найден (мог быть очищен ретеншном).",
        "en": "🔍 Incident not found (may have been purged by retention).",
    },
    # §6 «Сообщить об ошибке» (/reportbug)
    "bug_ask": {
        "ru": (
            "🐞 Опишите проблему одним сообщением: что делали, что пошло не так, что ожидали.\n"
            "Я передам это администратору. Секреты можно не вычищать — я их редактирую автоматически."
        ),
        "en": (
            "🐞 Describe the problem in one message: what you did, what went wrong, what you expected.\n"
            "I'll pass it to the admin. No need to strip secrets — I redact them automatically."
        ),
    },
    "bug_empty": {
        "ru": "Пустое сообщение — опишите проблему текстом (или «✖ Отмена»).",
        "en": "Empty message — describe the problem in text (or “✖ Cancel”).",
    },
    "bug_thanks": {
        "ru": "✅ Спасибо! Баг-репорт принят. Тикет: <code>{ticket}</code> — админ уведомлён.",
        "en": "✅ Thanks! Bug report received. Ticket: <code>{ticket}</code> — the admin was notified.",
    },
    "bug_save_failed": {
        "ru": "⚠️ Не удалось сохранить баг-репорт (проблема с БД). Попробуйте позже.",
        "en": "⚠️ Couldn't save the bug report (DB issue). Please try again later.",
    },
    "llm_budget_exceeded": {
        "ru": (
            "🚦 Достигнут дневной лимит запросов к ИИ ({used}/{limit}). "
            "Попробуй завтра или попроси админа поднять лимит."
        ),
        "en": (
            "🚦 Daily AI request limit reached ({used}/{limit}). "
            "Try again tomorrow or ask an admin to raise the limit."
        ),
    },
    "err_campaigns": {
        "ru": "⚠️ Не удалось получить кампании: {err}",
        "en": "⚠️ Couldn't fetch campaigns: {err}",
    },
    "err_report": {
        "ru": "⚠️ Не удалось построить отчёт: {err}",
        "en": "⚠️ Couldn't build the report: {err}",
    },
    # §6 /account: выбор АКТИВНОГО аккаунта — и для отчётов, и как цель изменений по умолчанию
    # (AD.3: мут-мяты минтят на активном аккаунте; каждое изменение всё равно за подтверждением «да»).
    "account_current": {
        "ru": (
            "👤 Активный аккаунт: <code>{cid}</code>{draft}\n"
            "Сменить: <code>/account 123-456-7890</code> · Сброс: <code>/account reset</code>\n"
            "📊 Отчёты — по нему. ✏️ Изменения — тоже по нему (по умолчанию), но КАЖДОЕ через "
            "подтверждение «да»."
        ),
        "en": (
            "👤 Active account: <code>{cid}</code>{draft}\n"
            "Switch: <code>/account 123-456-7890</code> · Reset: <code>/account reset</code>\n"
            "📊 Reports use it. ✏️ Changes target it too (by default), but EACH one requires "
            "explicit confirmation."
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
    # 3.5: действия под сводкой /mcc
    "mcc_actions": {
        "ru": (
            "⚙️ Тап по аккаунту — сделать его активным (все команды пойдут по нему). "
            "«▶️ Аудит по всем» пересчитает скоры 🩺 — займёт несколько минут."
        ),
        "en": (
            "⚙️ Tap an account to make it active (commands will target it). "
            "“▶️ Audit all” recomputes the 🩺 scores — takes a few minutes."
        ),
    },
    "mcc_audit_all_btn": {"ru": "▶️ Аудит по всем", "en": "▶️ Audit all"},
    "mcc_audit_running": {
        "ru": "⏳ Прогон аудита уже идёт — дождись завершения.",
        "en": "⏳ An audit run is already in progress — wait for it to finish.",
    },
    "mcc_audit_progress": {
        "ru": "⏳ Аудит по аккаунтам: {done}/{total}{last}",
        "en": "⏳ Auditing accounts: {done}/{total}{last}",
    },
    "mcc_audit_done": {
        "ru": "✅ Аудит прогнан: {ok}/{total} (сбоев: {fail}). Свежие скоры — в /mcc.",
        "en": "✅ Audit finished: {ok}/{total} (failures: {fail}). Fresh scores — in /mcc.",
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
        "ru": "⚠️ Параметры не прошли валидацию:\n{err}",
        "en": "⚠️ Parameters failed validation:\n{err}",
    },
    # Короткие локализованные фразы правил валидации (bot.ux.humanize_validation) — вместо
    # сырого многострочного дампа Pydantic. {n} — числовой лимит из ctx ошибки.
    "val_le": {"ru": "должно быть ≤ {n}", "en": "must be ≤ {n}"},
    "val_ge": {"ru": "должно быть ≥ {n}", "en": "must be ≥ {n}"},
    "val_lt": {"ru": "должно быть < {n}", "en": "must be < {n}"},
    "val_gt": {"ru": "должно быть > {n}", "en": "must be > {n}"},
    "val_too_long": {"ru": "не длиннее {n} символов", "en": "at most {n} characters"},
    "val_too_short": {"ru": "не короче {n} символов", "en": "at least {n} characters"},
    "val_missing": {"ru": "обязательное поле", "en": "required field"},
    "val_more": {"ru": "…и ещё {n}", "en": "…and {n} more"},
    # Отказ не-whitelisted пользователю (bot.main.WhitelistMiddleware) — с его chat_id для админа.
    "access_denied": {
        "ru": "⛔ Доступ к боту не выдан.\nПередайте администратору ваш ID: {chat_id}",
        "en": "⛔ Bot access is not granted.\nSend your ID to the administrator: {chat_id}",
    },
    "err_photo": {
        "ru": "⚠️ Не удалось обработать фото: {err}",
        "en": "⚠️ Couldn't process the photo: {err}",
    },
    "err_period": {
        "ru": (
            "⚠️ Не удалось разобрать период. Используй пресет 7/14/30/90/MTD/LM, даты "
            "ГГГГ-ММ-ДД [ГГГГ-ММ-ДД] или фразу («вчера», «прошлый месяц», «с 1 по 15 июня»)."
        ),
        "en": (
            "⚠️ Couldn't parse the period. Use a preset 7/14/30/90/MTD/LM, dates "
            "YYYY-MM-DD [YYYY-MM-DD] or a phrase (“yesterday”, “last month”, “june 1-15”)."
        ),
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
    # A4: честная причина, когда аккаунт деактивирован/нет прав (ошибка от Google Ads, НЕ Sheets-
    # scope). Раньше по такой ошибке показывалась подсказка про drive.file — вводило в заблуждение.
    "err_account_inactive": {
        "ru": (
            "⚠️ Аккаунт недоступен: он отключён, деактивирован или у бота нет прав на его чтение.\n"
            "{err}\n"
            "Это не проблема настройки Sheets — выбери другой аккаунт или проверь его статус в "
            "Google Ads (активен ли, привязан ли к нашему MCC)."
        ),
        "en": (
            "⚠️ Account unavailable: it is disabled, deactivated, or the bot has no read access.\n"
            "{err}\n"
            "This is not a Sheets setup issue — pick another account or check its status in "
            "Google Ads (is it active and linked to our MCC)."
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
    "loop_multi_actions": {
        "ru": "⚠️ Распознано несколько действий — обработал только первое ({name}). "
        "Остальные пришли, пожалуйста, отдельными сообщениями по одному.",
        "en": "⚠️ Recognized several actions — handled only the first ({name}). "
        "Please send the others one per message.",
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
    "loop_account_denied": {
        "ru": (
            "⛔ Аккаунт «{account}» не разрешён тебе на чтение. Доступные — /accounts; "
            "доступ выдаёт админ (/grant)."
        ),
        "en": (
            "⛔ You don't have read access to account “{account}”. See /accounts; "
            "access is granted by the admin (/grant)."
        ),
    },
    "loop_account_not_found": {
        "ru": "❓ Не понял, какой аккаунт: {detail}. Список — /accounts.",
        "en": "❓ Couldn't resolve the account: {detail}. See /accounts.",
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
            "Пришлите информацию о клиенте обычным текстом, своими словами (можно несколькими "
            "сообщениями): бизнес, сайт, соцсети, услуги, цены, телефоны. Когда закончите — "
            "«💾 Сохранить».\n\n"
            "Пример (можно скопировать и поправить):\n"
            "<code>Студия «Флора», доставка цветов и букетов по Киеву. Сайт floraprima.ua, "
            "инстаграм @floraprima. Услуги: букеты от 500 грн, оформление свадеб. Тел "
            "+380 44 123 45 67.</code>\n"
            "<i>Или нажмите «🔎 Подтянуть из аккаунта» — заполню фактами из Google Ads.</i>"
        ),
        "en": (
            "Send the client info as free text, in your own words (several messages are fine): "
            "business, site, socials, services, prices, phones. When done — “💾 Save”.\n\n"
            "Example (tap to copy, then tweak):\n"
            "<code>Studio “Flora”, flower & bouquet delivery in Kyiv. Site floraprima.ua, "
            "instagram @floraprima. Services: bouquets from 500 UAH, wedding decor. Tel "
            "+380 44 123 45 67.</code>\n"
            "<i>Or tap “🔎 Fill from account” to prefill facts from Google Ads.</i>"
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
    "cli_autofill_reading": {
        "ru": "🔎 Читаю аккаунт (валюта, таймзона, гео/языки, домен)… только реальные данные.",
        "en": "🔎 Reading the account (currency, timezone, geo/languages, domain)… facts only.",
    },
    "cli_autofill_empty": {
        "ru": (
            "В аккаунте не нашлось фактов для профиля (нет активных кампаний/таргетинга). "
            "Добавьте информацию текстом через «➕ Добавить информацию»."
        ),
        "en": (
            "No account facts found for the profile (no active campaigns/targeting). "
            "Add info as text via “➕ Add info”."
        ),
    },
    "cli_autofill_failed": {
        "ru": "⚠️ Не удалось прочитать аккаунт: {err}",
        "en": "⚠️ Couldn't read the account: {err}",
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
        "ru": (
            "💾 Авто-сохранение по таймауту: показал черновик профиля — подтвердите «да» выше. "
            "Режим накопления закрыт; добавить ещё — «✏️ Обновить инфу»."
        ),
        "en": (
            "💾 Auto-save on idle: here's the profile draft — confirm “yes” above. "
            "Accumulation closed; to add more — “✏️ Update info”."
        ),
    },
    "cli_crawl_unchanged": {
        "ru": "✅ Сайт {domain} не изменился ({pages} стр.). Профиль актуален, обновление не требуется.",
        "en": "✅ Site {domain} is unchanged ({pages} pages). Profile is up to date, no update needed.",
    },
    "cli_crawl_diff": {
        "ru": "🆕 Новых страниц: {new} · ✏️ изменённых: {changed}",
        "en": "🆕 New pages: {new} · ✏️ changed: {changed}",
    },
    "cli_crawl_partial": {
        "ru": (
            "⏳ Обход прерван по времени: успел {pages} стр. Профиль собран по ЧАСТИ сайта — "
            "запусти обход ещё раз, чтобы добрать остальное."
        ),
        "en": (
            "⏳ Crawl hit the time budget: {pages} pages done. The profile covers PART of the site — "
            "run the crawl again to pick up the rest."
        ),
    },
    "cli_dossier_none": {
        "ru": "Досье ещё нет: запусти обход сайта («🕷 Сохранить и краулить» / «🔄 Перекраулить»).",
        "en": "No dossier yet: run a site crawl («🕷 Save and crawl» / «🔄 Re-crawl»).",
    },
    "cli_crawl_dossier_line": {
        "ru": (
            "📄 Досье собрано: услуг {services} · людей {people} · фактов {facts} "
            "(файл ниже — он же контекст для текстов объявлений)"
        ),
        "en": (
            "📄 Dossier built: {services} services · {people} people · {facts} facts "
            "(file below — same context feeds ad copy)"
        ),
    },
    "cli_crawl_dossier_budget": {
        "ru": (
            "📄 Досье не собрано: дневной лимит ИИ исчерпан ({used}/{limit}). "
            "Страницы сайта сохранены — запусти обход завтра, досье соберётся."
        ),
        "en": (
            "📄 Dossier not built: daily AI limit reached ({used}/{limit}). "
            "Site pages are saved — run the crawl tomorrow to build the dossier."
        ),
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
    # Причины отказа краула — человеческим языком. Класс исключения наружу не идёт (решение
    # P1-аудита), сырой str(e) — тем более (правило 5), и у сетевых ошибок он вдобавок ПУСТОЙ:
    # именно так пользователю приезжало «не удался: ?».
    "crawl_err_timeout": {
        "ru": "сайт не ответил за отведённое время — попробуй ещё раз чуть позже",
        "en": "the site didn't respond in time — try again a bit later",
    },
    "crawl_err_unreachable": {
        "ru": "сайт недоступен: домен не отвечает или не существует — проверь адрес",
        "en": "site unreachable: the domain doesn't respond or doesn't exist — check the address",
    },
    "crawl_err_tls": {
        "ru": "проблема с сертификатом сайта (HTTPS) — обход остановлен",
        "en": "the site's TLS certificate is invalid — crawl stopped",
    },
    "crawl_err_down": {
        "ru": "сайт перестал отвечать (много ошибок подряд) — обход остановлен",
        "en": "the site kept failing (too many errors in a row) — crawl stopped",
    },
    "crawl_err_redirects": {
        "ru": "слишком много переадресаций — сайт зациклил редиректы",
        "en": "too many redirects — the site is looping",
    },
    "crawl_err_robots": {
        "ru": "robots.txt сайта запрещает обход — данные собрать нельзя",
        "en": "the site's robots.txt forbids crawling — no data can be collected",
    },
    "crawl_err_forbidden": {
        "ru": "сайт закрыл доступ боту (403) — обход невозможен",
        "en": "the site denied access to the bot (403) — crawl not possible",
    },
    "crawl_err_notfound": {
        "ru": "страница не найдена (404) — проверь адрес сайта",
        "en": "page not found (404) — check the site address",
    },
    "crawl_err_http": {
        "ru": "сайт вернул ошибку {code} — попробуй позже",
        "en": "the site returned error {code} — try later",
    },
    "crawl_err_blocked": {
        "ru": "адрес заблокирован политикой безопасности (внутренний/небезопасный)",
        "en": "the address is blocked by the security policy (internal/unsafe)",
    },
    "crawl_err_generic": {
        "ru": "не удалось прочитать сайт — подробности в логе",
        "en": "couldn't read the site — details are in the log",
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
    # D1: поиск кампании по названию в пикерах (/campaigns, отчёт, /rsa)
    "picker_search_prompt": {
        "ru": "🔎 Пришлите часть названия кампании (или id) — покажу совпадения.",
        "en": "🔎 Send part of the campaign name (or id) — I’ll show matches.",
    },
    "picker_search_empty": {
        "ru": "🔎 По «{q}» кампаний не нашёл. Вот весь список:",
        "en": "🔎 No campaigns match “{q}”. Here’s the full list:",
    },
    "picker_search_results": {
        "ru": "🔎 Найдено: {n}. Выберите кампанию (показаны первые {shown}):",
        "en": "🔎 Found: {n}. Choose a campaign (showing first {shown}):",
    },
    "picker_pick_campaign": {
        "ru": "Выберите кампанию:",
        "en": "Choose a campaign:",
    },
    # D2: откат применённой обратимой операции (мятие ОБРАТНОГО черновика за confirm-гейтом)
    "rollback_offer": {
        "ru": "↩️ Не то? Могу откатить это изменение — верну прежнее значение (тоже через ✅).",
        "en": "↩️ Not right? I can undo this change — restore the previous value (also via ✅).",
    },
    "rollback_stale": {
        "ru": "↩️ Откат уже недоступен (появилось новое действие или бот перезапускался).",
        "en": "↩️ Undo is no longer available (a newer action appeared or the bot restarted).",
    },
    "rollback_failed": {
        "ru": "↩️ Не удалось собрать откат: {err}",
        "en": "↩️ Couldn’t build the undo: {err}",
    },
    # Доп.2A: окно пост-проверки — применённое значение разошлось с подтверждённым «станет».
    # Значения код-генерированы (микро/enum), не сырой SDK. Строка помечена needs_review.
    "verify_mismatch": {
        "ru": (
            "⚠️ Проверка после применения не сошлась: в аккаунте <b>{actual}</b>, "
            "ожидалось <b>{expected}</b>. Операция помечена на ревью (см. /journal) — "
            "проверь аккаунт вручную; при необходимости откати кнопкой ниже."
        ),
        "en": (
            "⚠️ Post-apply check didn’t match: account shows <b>{actual}</b>, "
            "expected <b>{expected}</b>. Flagged for review (see /journal) — "
            "verify the account manually; undo below if needed."
        ),
    },
    # Доп.2B: персистентный откат из /journal — реверс не собрать (нет снимка/смешанные ставки/
    # не та операция). Кнопка была, но на клике честно отказываем (fail-closed).
    "rollback_not_reversible": {
        "ru": "↩️ Эту операцию откатить нельзя (нет снимка «было» или откат неоднозначен).",
        "en": "↩️ This operation can’t be undone (no “before” snapshot or ambiguous reverse).",
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
            "📝 Опишите кампанию <b>одним сообщением, своими словами</b> — спецсимволы не нужны, "
            "я сам разложу на настройки.\n\n"
            "Полезно указать: <i>что рекламируем · страна/город · дневной бюджет · цель "
            "(звонки/заявки/продажи/трафик) · язык · даты · расписание</i>.\n\n"
            "Пример (нажмите, чтобы скопировать, и поправьте под себя):\n"
            "<code>Доставка цветов по Киеву и Одессе, бюджет 300 грн в день, цель — заявки, "
            "украинский язык, с сегодня до конца месяца, показ пн-пт 9-21</code>"
        ),
        "en": (
            "📝 Describe the campaign <b>in one message, in your own words</b> — no special "
            "characters needed, I'll turn it into settings.\n\n"
            "Helpful to mention: <i>what you promote · country/city · daily budget · goal "
            "(calls/leads/sales/traffic) · language · dates · schedule</i>.\n\n"
            "Example (tap to copy, then tweak):\n"
            "<code>Flower delivery in Kyiv and Odesa, budget 300 UAH per day, goal — leads, "
            "Ukrainian language, from today till end of month, show Mon-Fri 9-21</code>"
        ),
    },
    "cc_extracting": {
        "ru": "⏳ Разбираю описание на настройки…",
        "en": "⏳ Parsing the description into settings…",
    },
    "cc_empty_description": {
        "ru": (
            "Нужно текстовое описание. Пример (можно скопировать и поправить):\n"
            "<code>Ремонт квартир под ключ в Варшаве, бюджет 200 zł в день, цель — заявки, "
            "польский язык</code>"
        ),
        "en": (
            "I need a text description. Example (tap to copy, then tweak):\n"
            "<code>Turnkey apartment renovation in Warsaw, budget 200 PLN per day, goal — leads, "
            "Polish language</code>"
        ),
    },
    "cc_draft_stale": {
        "ru": "Черновик кампании не найден или устарел — начните заново: «➕ Создание кампании».",
        "en": "The campaign draft was not found or expired — start over: “➕ Create campaign”.",
    },
    # §19.2: аккаунт выбран, но мутации на нём запрещены замком — честно предупреждаем СРАЗУ,
    # а не на финале (черновик собрать можно, создание будет отклонено).
    "cc_account_readonly": {
        "ru": (
            "⚠️ На этом аккаунте изменения запрещены (только чтение). Черновик собрать можно, "
            "но «Создать» будет отклонён — включите аккаунт в список мутаций или выберите другой."
        ),
        "en": (
            "⚠️ Changes are not allowed on this account (read-only). You can still build the draft, "
            "but “Create” will be rejected — enable the account for mutations or pick another one."
        ),
    },
    "cc_resume_prompt": {
        "ru": "У вас есть незавершённый черновик кампании (этап {step}/7). Продолжить или начать заново?",
        "en": "You have an unfinished campaign draft (step {step}/7). Resume or start over?",
    },
    "start_resume_hint": {
        "ru": "▶️ У вас есть незавершённая кампания (этап {step}/7). Продолжить?",
        "en": "▶️ You have an unfinished campaign (step {step}/7). Resume?",
    },
    # Хлебная крошка этапа визарда §19 — префикс к промптам этапов (пользователь видит прогресс).
    "cc_step_crumb": {
        "ru": "🆕 Кампания · шаг {step}/7\n\n",
        "en": "🆕 Campaign · step {step}/7\n\n",
    },
    "report_repeat_last": {
        "ru": "↻ Повторить прошлый отчёт или выбрать заново?",
        "en": "↻ Repeat the last report or pick again?",
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
    "cc_kw_busy": {
        "ru": "⏳ Подбор ключей уже идёт — дождись таблицы (20–40 с), второй запуск не нужен.",
        "en": "⏳ Keyword generation is already running — wait for the sheet (20–40 s).",
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
    "delete_confirm_alert": {
        "ru": "⚠️ Это НЕОБРАТИМО. Нажмите «Да, удалить безвозвратно» для подтверждения.",
        "en": "⚠️ This is IRREVERSIBLE. Tap “Yes, delete permanently” to confirm.",
    },
    "dg_budget_below_min": {
        "ru": (
            "⚠️ Дневной бюджет {have} {cur} может быть НИЖЕ минимума для Demand Gen/Video "
            "(обычно ≥ {minv} {cur}). Google может отклонить создание — если так, увеличьте бюджет "
            "и повторите."
        ),
        "en": (
            "⚠️ Daily budget {have} {cur} may be BELOW the Demand Gen/Video minimum "
            "(usually ≥ {minv} {cur}). Google may reject creation — if so, raise the budget and retry."
        ),
    },
    "cc_kw_verify_prompt_v2": {
        "ru": (
            "✅ Я уже сохранил <b>{n}</b> ключей в черновик.\n"
            "• Хотите оставить как есть — нажмите «✅ Использовать эти ключи».\n"
            "• Хотите отредактировать — поправьте таблицу и пришлите ссылку на неё сообщением "
            "(тогда возьму ваш выверенный список)."
        ),
        "en": (
            "✅ I've already saved <b>{n}</b> keywords to the draft.\n"
            "• Keep them — tap «✅ Use these keywords».\n"
            "• Refine — edit the sheet and send its link back (I'll take your curated list)."
        ),
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
    # N5: активен визард-экран без своего текст-хендлера (пикер/параметры) — не уводим ввод в агента.
    "wizard_use_screen": {
        "ru": "Заверши текущий шаг на экране (нажми кнопку выбора или пришли нужные данные). Выйти — /cancel.",
        "en": "Finish the current step on screen (tap a choice or send the needed data). Exit — /cancel.",
    },
    # N5/N3b: на экране параметров /keywords приняли присланные сид-слова/URL — пере-показываем параметры.
    "kw_params_seeds_added": {
        "ru": "Добавил в подбор: {n} сид-слов(а){url}. Проверь параметры и жми «🚀 Подобрать».",
        "en": "Added to the search: {n} seed(s){url}. Check the parameters and tap “🚀 Search ideas”.",
    },
    # C1: Google принимает не больше MAX_SEEDS=20 сид-ключей за запрос (лишние отбрасываем ЧЕСТНО —
    # молчаливое усечение выглядело бы как «подобрал по всему списку»). Раньше 21-й сид ронял весь
    # запрос в InvalidArgument.
    "kw_seeds_capped": {
        "ru": "Взял первые {n} сид-слов из {total} — Google принимает максимум {n} за один подбор.",
        "en": "Kept the first {n} seeds out of {total} — Google accepts at most {n} per search.",
    },
    "kw_params_need_seeds": {
        "ru": "Сначала пришли сид-слова или ссылку (текстом на этом экране), затем «🚀 Подобрать».",
        "en": "First send seed keywords or a URL (as text on this screen), then “🚀 Search ideas”.",
    },
    # K: RU/BY не обслуживаются Keyword Planner — подобрали без этих стран (иначе «invalid value»).
    "kw_geo_dropped": {
        "ru": "⚠️ Google Ads не обслуживает Россию/Беларусь как гео — подобрал ключи без этих стран.",
        "en": "⚠️ Google Ads doesn't serve Russia/Belarus as a geo — searched keywords without them.",
    },
    # G2: баннер аккаунта мутации (не-Draft) в карточке подтверждения — на ЧЬИ деньги идёт изменение.
    # AD.2: баннер аккаунта мутации — на КАЖДОЙ карточке. Боевой (не-Draft) — с ⚠️ (реальные деньги);
    "mutation_account_banner": {
        "ru": "⚠️ Аккаунт изменения: {acct}",
        "en": "⚠️ Change account: {acct}",
    },
    # Draft (песочница) — спокойный маркер, чтобы «реальные деньги» ⚠️ оставались отличимым сигналом.
    "mutation_account_banner_draft": {
        "ru": "🧪 Аккаунт изменения: {acct}",
        "en": "🧪 Change account: {acct}",
    },
    # AD.1/AD.4: активный аккаунт только для чтения — мутации на нём не включены. В проде мутации
    # включены на всех видимых по умолчанию; этот отказ теперь редкий (аккаунт вне видимости бота или
    # набор сужен явным списком без него).
    "mutation_account_read_only": {
        "ru": (
            "⛔ Аккаунт {acct} доступен только для ЧТЕНИЯ — изменения на нём не включены.\n"
            "Включить может администратор: убедиться, что аккаунт виден боту (обход MCC), при другом "
            "MCC — зарегистрировать OAuth; набор мутаций — GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all (все "
            "видимые) или явный список id. Проверка готовности — /mutready."
        ),
        "en": (
            "⛔ Account {acct} is READ-ONLY — changes are not enabled for it.\n"
            "An admin can enable it: make sure the account is visible to the bot (MCC crawl), register "
            "OAuth if it's under another MCC; mutation set — GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all (all "
            "visible) or an explicit id list. Check readiness with /mutready."
        ),
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
        "ru": "✅ Переиспользую {n} ассет(ов) аккаунта: {types}.",
        "en": "✅ Reusing {n} account asset(s): {types}.",
    },
    # §19.7: выбор ПОДМНОЖЕСТВА (раньше линковались все найденные ассеты аккаунта без спроса).
    "cc_assets_reuse_pick": {
        "ru": (
            "♻️ Какие ассеты аккаунта переиспользовать? Нажми на тип, чтобы включить/выключить.\n"
            "Сейчас выбрано: {n}."
        ),
        "en": (
            "♻️ Which account assets to reuse? Tap a type to toggle it.\nCurrently selected: {n}."
        ),
    },
    "cc_assets_pick_type": {
        "ru": "Какой ассет добавить? Сгенерирую наполнение по теме и сайту, вы сможете подтвердить.",
        "en": "Which asset to add? I'll generate the content from the topic and site for you to confirm.",
    },
    # §19.7.1: типы ассетов, требующие внешней настройки аккаунта (не автогенерируются) — честное
    # объяснение вместо тихого отсутствия в перечне.
    "cc_asset_needs_location": {
        "ru": (
            "📍 <b>Адрес (Location)</b> требует привязки <b>Google Business Profile</b> к аккаунту "
            "(адрес из текста нельзя превратить в объявление без верифицированной точки на картах). "
            "Свяжите Business Profile в Google Ads и попробуйте снова."
        ),
        "en": (
            "📍 <b>Location</b> asset requires a linked <b>Google Business Profile</b> on the account "
            "(a text address can't become an ad without a verified Maps location). "
            "Link the Business Profile in Google Ads and try again."
        ),
    },
    "cc_asset_needs_affiliate": {
        "ru": (
            "🏬 <b>Адрес аффилиата</b> удалён из Google Ads API v24 (Google депрекировал этот тип "
            "ассета) — создать его нельзя. Используйте адрес (Location) через Business Profile."
        ),
        "en": (
            "🏬 <b>Affiliate location</b> was removed from Google Ads API v24 (Google deprecated this "
            "asset type) — it can't be created. Use a Location asset via Business Profile instead."
        ),
    },
    # §19.7.1: лид-форма РЕАЛИЗОВАНА — просим URL политики конфиденциальности (единственный внешний
    # обязательный ввод; остальное собираем из профиля §20). Ассет строится за confirm-гейтом.
    "cc_asset_lead_form_prompt": {
        "ru": (
            "📝 <b>Лид-форма.</b> Пришлите ссылку на <b>политику конфиденциальности</b> (обязательна "
            "для лид-форм Google). Остальное (бренд, заголовок, поля имя/e-mail/телефон) соберу из "
            "профиля клиента.\n<i>Аккаунт должен принять условия Lead Form в Google Ads — иначе ассет "
            "будет пропущен при создании (кампания не пострадает).</i>"
        ),
        "en": (
            "📝 <b>Lead form.</b> Send the <b>privacy policy</b> URL (required by Google for lead "
            "forms). I'll fill the rest (business name, headline, name/email/phone fields) from the "
            "client profile.\n<i>The account must accept Lead Form terms in Google Ads — otherwise the "
            "asset is skipped on creation (the campaign is unaffected).</i>"
        ),
    },
    "cc_asset_lead_form_bad_url": {
        "ru": "Нужна ссылка на политику конфиденциальности (http:// или https://). Пришлите URL.",
        "en": "A privacy policy link is required (http:// or https://). Please send the URL.",
    },
    "cc_asset_app_out_of_scope": {
        "ru": (
            "📱 <b>Приложение (App)</b> — вне объёма проекта: App/UAC-кампании исключены (у клиента "
            "нет приложения). Используйте другие типы ассетов."
        ),
        "en": (
            "📱 <b>App</b> asset is out of scope: App/UAC campaigns are excluded (the client has no "
            "app). Use the other asset types."
        ),
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
