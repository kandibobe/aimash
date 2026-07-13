"""Тексты и шаблоны сообщений бота (RU). Вынесены отдельно — упрощает правки и будущую
EN-локализацию (ТЗ §4). Формат — HTML (parse_mode='HTML' на стороне отправки в bot.main);
ВСЕ динамические данные (имена кампаний, текст ошибок) обязательно через esc().
"""

from __future__ import annotations

import html
import re

from core.limits import ZERO_DECIMAL_CURRENCIES


def esc(s: object) -> str:
    """Экранирование для HTML parse_mode (имена кампаний/ошибки могут содержать < & >)."""
    return html.escape(str(s), quote=False)


def _lang(lang: str | None) -> str:
    """Разрешить язык форматтера: явный lang → он; None → язык текущего запроса (contextvar,
    ставит LangMiddleware). i18n импортируем ЛЕНИВО внутри функции — bot.i18n импортирует bot.texts
    на уровне модуля, поэтому верхнеуровневый импорт здесь дал бы циклический import."""
    if lang is None:
        from bot import i18n

        return i18n.current_lang()
    return lang if lang in ("ru", "en") else "ru"


def _thou(n: float, dec: int = 0) -> str:
    """Число с пробелом-разделителем тысяч: 12480 -> '12 480', 4512.3 -> '4 512.30'."""
    return f"{n:,.{dec}f}".replace(",", " ")


def status_human(status: str, lang: str | None = None) -> str:
    if _lang(lang) == "en":
        return {"ENABLED": "enabled ▶️", "PAUSED": "paused ⏸", "REMOVED": "removed 🗑"}.get(
            status, status
        )
    return {"ENABLED": "включена ▶️", "PAUSED": "на паузе ⏸", "REMOVED": "удалена 🗑"}.get(
        status, status
    )


# ── Профиль бота (@BotFather) ────────────────────────────────────────────────────
# Плейн-текст (Telegram НЕ парсит HTML в описаниях). Длина считается КОДОМ (golden rule #4):
# short ≤120, description ≤512 символов-кодпойнтов. Ставятся при старте (bot.main.main →
# set_my_short_description / set_my_description). Голос продукта: контроль/прозрачность/«твоё да».
BOT_SHORT_DESCRIPTION = (
    "Управляй Google Ads прямо в Telegram — обычным текстом. "
    "Любое изменение только после твоего «да». 🙂"
)

BOT_DESCRIPTION = (
    "Aimash — твой ассистент по Google Ads прямо в Telegram.\n\n"
    "Я читаю кампании и предлагаю изменения, но решаешь ты. Перед любой правкой бюджета, "
    "ставки или ключей покажу «было → станет» и попрошу подтверждение. Без твоего «да» "
    "ничего не меняется — это про твои деньги.\n\n"
    "Что умею:\n"
    "• статистика и отчёты за период (.xlsx и Google Sheets)\n"
    "• бюджет, ставки, ключевые и минус-слова, пауза кампаний\n"
    "• генерация RSA-текстов, подбор ключевых слов\n\n"
    "Я исполнитель, не автопилот. Каждое действие пишется в журнал. /start"
)

BOT_SHORT_DESCRIPTION_EN = (
    "Manage Google Ads right in Telegram — in plain text. "
    "Any change happens only after your “yes”. 🙂"
)

BOT_DESCRIPTION_EN = (
    "Aimash — your Google Ads assistant right in Telegram.\n\n"
    "I read your campaigns and suggest changes, but you decide. Before any budget, bid or "
    "keyword edit I show “before → after” and ask for confirmation. Nothing changes without "
    "your “yes” — it's your money.\n\n"
    "What I can do:\n"
    "• stats and period reports (.xlsx and Google Sheets)\n"
    "• budget, bids, keywords and negatives, pausing campaigns\n"
    "• RSA copy generation, keyword research\n\n"
    "I'm an executor, not an autopilot. Every action is logged. /start"
)


# ── Статичные тексты ─────────────────────────────────────────────────────────────
# START — подпись к приветственному баннеру bot/assets/welcome.png (HTML; лимит подписи 1024).
START = (
    "👋 <b>Aimash на связи.</b>\n\n"
    "Я читаю твой Google Ads и предлагаю изменения, но я <b>исполнитель, не автопилот</b> — "
    "последнее слово всегда за тобой.\n\n"
    "Любая правка (бюджет, ставка, ключи, пауза) — <b>только после твоего «да»</b>. "
    "Сначала покажу <i>«было → станет»</i> и кнопки ✅/❌, потом выполню. Без подтверждения "
    "не трогаю ничего — это твои деньги, и они под контролем.\n\n"
    "🔒 Каждое действие пишется в журнал: всегда видно, что и когда изменилось. "
    "Бюджет меняю только по твоей прямой команде.\n\n"
    "Попробуй обычным текстом:\n"
    "• <i>покажи статистику за 7 дней</i>\n"
    "• <i>повысь бюджет Search Spring на 20%</i>\n"
    "• <i>поставь на паузу Brand</i>\n\n"
    "Или жми кнопки меню. /help — подробнее."
)

HELP = (
    "<b>Что я умею сейчас</b>\n"
    "Перед любым изменением показываю «было → станет» и жду подтверждения «да».\n\n"
    "<b>Изменения</b> (по тексту, с подтверждением):\n"
    "• бюджет, ставка CPC, ключевые слова, минус-слова, пауза/возобновление\n\n"
    "<b>📣 Кампании</b>\n"
    "/newcampaign — пошаговый визард создания: аккаунт → настройки → ключи → объявление → ассеты\n"
    "/newsearch — быстрая поисковая кампания по брифу (RSA + ключи), на паузе\n"
    "🖼 пришли фото — соберу медийную кампанию (GDN), создам после «да» (на паузе)\n"
    "🎬 пришли видео или /newvideo — кампания из видео: Demand Gen / Video (YouTube, на паузе)\n"
    "/campaigns — кампании + быстрые действия (пауза/возобновление, 🎯 аудитории, 🧩 расширения)\n"
    "/pause Название · /resume Название — пауза/возобновление (с подтверждением)\n"
    "/templates — шаблоны кампаний · /savetemplate имя [from Кампания] — сохранить как шаблон\n"
    "/recent — недавние действия: повторить в один тап (с подтверждением)\n\n"
    "<b>🔑 Ключевые слова и тексты</b>\n"
    "/keywords — подбор ключевых слов (объём, конкуренция, кластеры) + .xlsx\n"
    "/addkeys — добавить ключи в кампанию (свой файл/ссылка/текст, через подтверждение)\n"
    "/searchterms — мусорные поисковые запросы (клики без конверсий) → минус-слова (через подтверждение)\n"
    "/rsa — сгенерировать тексты объявления (RSA), поэлементный confirm (создаётся на паузе)\n\n"
    "<b>📊 Отчёты</b>\n"
    "/status — быстрая статистика (30 дн.)\n"
    "/report [7|30|90|MTD | ГГГГ-ММ-ДД [ГГГГ-ММ-ДД]] — сводка за период (по умолч. 30 дн.)\n"
    "/export [период] — глубокий отчёт .xlsx · /sheets [период] — в Google Sheets (ссылка)\n"
    "/mcc [период] — сводка по всем дочерним аккаунтам MCC (подытоги по валютам)\n"
    "/quota — дневная квота операций Google Ads API\n"
    "/advise [optimize|keywords|rsa|structure] — 💡 рекомендации по ЖИВЫМ метрикам "
    "(расход/клики/конверсии). При нескольких аккаунтах — пикер выбора. На пустом/тест-"
    "аккаунте советов нет (нужен рабочий: /account). Только подсказки — ничего не меняю сам\n"
    "/audit [период] — 🩺 аудит здоровья аккаунта: оценка 0-100 + где утекают деньги + "
    "что чинить первым (безопасно, одним «да»). Рядом — родная оценка Google\n"
    "/target &lt;CPA&gt; — целевой CPA аккаунта (разблокирует в /audit паузу дорогих: CPA ≥ 3× цели)\n"
    "/alerts — пороги алертов аномалий (всплеск расхода / падение конверсий)\n\n"
    "<b>ℹ️ Клиенты</b>\n"
    "/clients — база знаний: профиль клиента текстом + краулинг сайта → релевантная генерация\n"
    "/client &lt;id&gt; — карточка клиента по номеру аккаунта\n\n"
    "<b>⚙️ Настройки и сервис</b>\n"
    "/account &lt;id&gt; | reset — аккаунт чтения для отчётов/ключей (по умолч. Draft)\n"
    "/accounts — мои доступные аккаунты · /whoami — мой chat_id и режим доступа\n"
    "/refresh — обновить список аккаунтов и кэши без рестарта\n"
    "/model — выбрать модель ИИ (OpenRouter) · /balance — бюджет ИИ: баланс и траты\n"
    "/lang — язык интерфейса (RU/EN)\n"
    "/journal — журнал изменений · /diag — журнал ошибок\n"
    "/reportbug — 🐞 сообщить об ошибке (передам админу)\n"
    "/cancel — отменить текущий черновик\n\n"
    "<b>👤 Для админа (ADMIN_CHAT_IDS)</b>\n"
    "/adduser &lt;chat_id&gt; — открыть оператору доступ к боту (без рестарта) + выбрать аккаунты\n"
    "/removeuser &lt;chat_id&gt; — закрыть доступ · /users — список операторов\n"
    "/grant &lt;chat_id&gt; &lt;id&gt; · /revoke &lt;chat_id&gt; &lt;id&gt; — точечный доступ к аккаунту (чтение)\n"
    "/addadmin &lt;chat_id&gt; · /removeadmin &lt;chat_id&gt; — админка без рестарта · /admins — список\n"
    "/bugs — очередь баг-репортов (триаж) · /mutready &lt;id&gt; — готовность аккаунта к мутациям\n\n"
    "<b>Как в другой кампании / по брифу</b>\n"
    "• «сделай кампанию N с настройками как в кампании X» — клонирую настройки.\n"
    "• 🧩 в /campaigns → «Расширения»: быстрые ссылки, уточнения, структурные описания, картинка.\n"
    "• Текстом: «добавь телефон +380… в кампанию X», «добавь промо −20% на лето», «добавь цены: "
    "Basic 9.99/мес…» — соберу расширение (телефон/промо/прайс) и спрошу подтверждение.\n"
    "• 📎 пришли ссылку или файл (.txt/.csv/.docx/.xlsx) + задачу — прочитаю и выполню "
    "(например: «подбери ключи по этому лендингу» или «кампанию по этому брифу»).\n\n"
    "<i>Новые объявления и кампании создаются на паузе — запуск отдельно, чтобы ничего не "
    "ушло в показ без твоего решения. Отчёты по расписанию и алерты аномалий работают в фоне.</i>"
)


# ── Переключатель модели ИИ (/model) ─────────────────────────────────────────────
def fmt_model_menu(active: str | None, parsing: str, copy: str, lang: str | None = None) -> str:
    """Экран /model: что активно сейчас + что реально пойдёт в запросы (parsing/copy)."""
    same = parsing == copy
    if _lang(lang) == "en":
        head = (
            f"🧠 <b>Active model:</b> <code>{esc(active)}</code>"
            if active
            else "🧠 <b>Model:</b> default (from settings)"
        )
        used = (
            f"<code>{esc(parsing)}</code>"
            if same
            else f"parsing — <code>{esc(parsing)}</code>, copy — <code>{esc(copy)}</code>"
        )
        return (
            f"{head}\n"
            f"In use now: {used}\n\n"
            "💡 <b>What's for what:</b>\n"
            "• 🐬 <b>DeepSeek V3</b> — cheap, for everyday; <b>V4 Pro</b> — stronger, also "
            "affordable.\n"
            "• 🧠 <b>Claude Sonnet 4.6</b> — best ad copy (RSA).\n"
            "• 👑 <b>Claude Opus 4.8</b> — top quality for hard tasks (pricier).\n"
            "• 🤖 <b>GPT-4o</b> / ⚡ <b>4o-mini</b> — a reliable alternative for parsing.\n\n"
            "Pick a preset, set your own, or reset to default.\n"
            "<i>⚠️ The model must support function calling — otherwise command parsing won't "
            "work.</i>"
        )
    head = (
        f"🧠 <b>Активная модель:</b> <code>{esc(active)}</code>"
        if active
        else "🧠 <b>Модель:</b> по умолчанию (из настроек)"
    )
    used = (
        f"<code>{esc(parsing)}</code>"
        if same
        else f"разбор — <code>{esc(parsing)}</code>, тексты — <code>{esc(copy)}</code>"
    )
    return (
        f"{head}\n"
        f"Сейчас в работе: {used}\n\n"
        "💡 <b>Что для чего:</b>\n"
        "• 🐬 <b>DeepSeek V3</b> — дёшево, на каждый день; <b>V4 Pro</b> — мощнее, тоже недорого.\n"
        "• 🧠 <b>Claude Sonnet 4.6</b> — лучшие тексты объявлений (RSA).\n"
        "• 👑 <b>Claude Opus 4.8</b> — максимум качества для сложного (дороже).\n"
        "• 🤖 <b>GPT-4o</b> / ⚡ <b>4o-mini</b> — надёжная альтернатива для разбора.\n\n"
        "Выбери пресет, задай свою или сбрось на дефолт.\n"
        "<i>⚠️ Модель должна поддерживать function calling — иначе разбор команд не сработает.</i>"
    )


MODEL_SET = "🧠 Модель переключена на <code>{model}</code>."
MODEL_RESET = "↩️ Сброшено на модель по умолчанию: <code>{model}</code>."
MODEL_ASK_CUSTOM = (
    "✏️ Пришли slug модели OpenRouter одним сообщением.\n"
    "Например: <code>anthropic/claude-sonnet-4.6</code> или <code>openai/gpt-4o-mini</code>\n"
    "Список — на openrouter.ai/models. Нужна поддержка function calling."
)
MODEL_BAD = (
    "Не похоже на slug модели OpenRouter (нужен вид <code>vendor/model</code>, до 128 символов). "
    "Пришли ещё раз или /model для меню."
)

# ── Keyword research (Фаза 3, БЛОК E) ────────────────────────────────────────────
# 2.7: единый регистр «ты» внутри фичи (KW_EMPTY/KW_BAD_INPUT уже на «ты» — брендовый голос).
KW_ASK = (
    "🔍 <b>Подбор ключевых слов</b>\n"
    "Пришли сид-слова через запятую и/или ссылку одним сообщением — можно вставить <b>свои "
    "ключи</b> или просто <b>описать нишу словами</b>, я подберу похожие и оценю объёмы.\n"
    "Например (нажми, чтобы скопировать): <code>доставка цветов, букеты, 101 роза</code>\n"
    "или ссылку <code>https://example.com</code>\n"
    "<i>Спецсимволы не нужны. Для точных метрик выбери живой аккаунт: /account</i>"
)
KW_SEARCHING = "⏳ Подбираю ключевые слова и группирую по интенту…"
KW_EMPTY = "Ничего не нашлось по этим сидам. Попробуй другие слова или ссылку: /keywords"
KW_BAD_INPUT = "Нужны сид-слова или ссылка. Пришли, например: <code>купить телефон, смартфон</code>"

PROPOSAL_PENDING = "📝 <b>Черновик изменения</b>\n\n{summary}\n\nПодтвердить? <i>(черновик действует {ttl_h} ч)</i>"
EXECUTING = "⏳ Выполняю…"
APPLIED = "✅ <b>Готово.</b>\n{result}"
# 3C: без технического {kind} (имя класса исключения) — {err} уже человекочитаем (humanize).
FAILED = "⚠️ Не удалось выполнить: {err}"
REJECTED = "❌ Отменено"
STALE = "Черновик не найден или устарел"
NO_PROPOSAL = "Нет активного черновика для отмены."
NO_CAMPAIGNS = "Кампаний нет."
CAMP_LIST_STALE = "Список кампаний устарел — вызови /campaigns заново."
NO_AUDIENCES = "👥 Доступных аудиторий (списков ремаркетинга) в аккаунте не найдено."
AUD_LIST_STALE = "Список аудиторий устарел — открой меню кампании заново."


def audiences_title(campaign: str, lang: str | None = None) -> str:
    if _lang(lang) == "en":
        return (
            f"👥 <b>Audiences</b> for campaign “{esc(campaign)}”\n"
            "Choose which one to attach to targeting (creates a draft — you'll confirm):"
        )
    return (
        f"👥 <b>Аудитории</b> для кампании «{esc(campaign)}»\n"
        "Выбери, какую прикрепить к таргетингу (создаст черновик — подтвердишь):"
    )


# ── RSA-генерация (фаза 2.C) ─────────────────────────────────────────────────────
RSA_PICK_CAMPAIGN = "✍️ <b>Генерация текстов объявления</b>\nВыбери кампанию:"
RSA_PICK_ADGROUP = "Выбери группу объявлений:"
RSA_NO_ADGROUPS = "В кампании нет групп объявлений — сначала создай группу."
RSA_ASK_BRIEF = (
    "Пришли <b>тематику</b> и <b>ссылку</b> объявления одним сообщением.\n"
    "Например: <code>доставка цветов | https://example.com</code>"
)
RSA_BAD_URL = "Не вижу корректной ссылки (http/https). Пришли тематику и URL ещё раз."
RSA_GENERATING = "⏳ Генерирую варианты…"
RSA_GEN_EMPTY = "Не удалось сгенерировать достаточно валидных вариантов. Попробуй ещё раз: /rsa"
RSA_SESSION_STALE = "Сессия генерации устарела — начни заново: /rsa"
RSA_REFINE_PROMPT = "✏️ Что поправить в этом элементе? Пришли короткую правку текстом."
RSA_REFINE_TOO_LONG = (
    "Доработанный вариант не уложился в лимит ({n}/{limit}). Пришли правку ещё раз."
)
RSA_BELOW_MIN = "Нужно ≥3 одобренных заголовка и ≥2 описания. Сейчас: {h} загол. / {d} опис."
RSA_CREATED = "✅ <b>Объявление создано (на паузе).</b>\n{result}"

# ── GDN из фото (§11) ─────────────────────────────────────────────────────────────
GDN_ASK_BRIEF = (
    "🖼 <b>Фото принято.</b> Соберу медийную кампанию (GDN).\n"
    "Пришли одним сообщением: <b>название | ссылка | дневной бюджет [| гео]</b>.\n"
    "Гео — опционально (локации через запятую).\n"
    "Например: <code>Весна 2026 | https://shop.example | 50 | Кения, Найроби</code>\n\n"
    "Тексты сгенерирую сам — покажу черновик «было → станет» перед созданием."
)
GDN_BAD_BRIEF = (
    "Не разобрал. Нужно <b>название | ссылка | бюджет [| гео]</b> (бюджет — число; гео опц.).\n"
    "Например: <code>Летняя распродажа | https://shop.example | 30 | Кения</code>"
)
GDN_GENERATING = "⏳ Генерирую тексты объявления…"
GDN_GEN_EMPTY = "Не удалось сгенерировать валидные тексты. Пришли фото и бриф ещё раз."
GDN_SESSION_STALE = "Сессия создания кампании устарела — пришли фото заново."
GDN_CREATED = "✅ <b>Кампания создана (на паузе).</b>\n{result}"


def fmt_rsa_element(
    kind: str, idx: int, total: int, e: dict, campaign: str, ad_group: str, lang: str | None = None
) -> str:
    """Карточка одного элемента курации: тип, текст, длина/лимит, кампания/группа."""
    from adcopy.validate import LIMITS

    limit = LIMITS["headline" if kind == "h" else "description"]
    if _lang(lang) == "en":
        name = "Headline" if kind == "h" else "Description"
        state = {
            "pending": "🟡 under review",
            "approved": "✅ approved",
            "rejected": "❌ rejected",
        }.get(e.get("state", "pending"), e.get("state", ""))
        return (
            f"<b>{name} {idx + 1}/{total}</b> · {e.get('len', 0)}/{limit} · {state}\n"
            f"“{esc(e.get('text', ''))}”\n\n"
            f"📋 {esc(campaign)} / {esc(ad_group)}"
        )
    name = "Заголовок" if kind == "h" else "Описание"
    state = {
        "pending": "🟡 на рассмотрении",
        "approved": "✅ одобрен",
        "rejected": "❌ отклонён",
    }.get(e.get("state", "pending"), e.get("state", ""))
    return (
        f"<b>{name} {idx + 1}/{total}</b> · {e.get('len', 0)}/{limit} · {state}\n"
        f"«{esc(e.get('text', ''))}»\n\n"
        f"📋 {esc(campaign)} / {esc(ad_group)}"
    )


def fmt_rsa_overview(
    h_appr: int, d_appr: int, h_total: int, d_total: int, lang: str | None = None
) -> str:
    """Итоговый экран курации: сколько одобрено из скольких, готовность к созданию."""
    if _lang(lang) == "en":
        ready = (
            "✅ ready to create"
            if (h_appr >= 3 and d_appr >= 2)
            else "need ≥3 headlines and ≥2 descriptions"
        )
        return (
            "📋 <b>RSA curation summary</b>\n"
            f"Headlines approved: <b>{h_appr}</b>/{h_total}\n"
            f"Descriptions approved: <b>{d_appr}</b>/{d_total}\n\n"
            f"{ready}"
        )
    ready = "✅ можно создавать" if (h_appr >= 3 and d_appr >= 2) else "нужно ≥3 загол. и ≥2 опис."
    return (
        "📋 <b>Итог курации RSA</b>\n"
        f"Заголовки одобрены: <b>{h_appr}</b>/{h_total}\n"
        f"Описания одобрены: <b>{d_appr}</b>/{d_total}\n\n"
        f"{ready}"
    )


def fmt_rsa_proposal_summary(
    ad_group: str,
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
    lang: str | None = None,
) -> str:
    """Плейн-текст сводка create_rsa для confirm-гейта (esc применяется при показе)."""
    h_lines = "\n".join(f"  • {h}" for h in headlines)
    d_lines = "\n".join(f"  • {d}" for d in descriptions)
    if _lang(lang) == "en":
        return (
            f"Create an ad (RSA) in group “{ad_group}” — paused.\n"
            f"Link: {final_url}\n\n"
            f"Headlines ({len(headlines)}):\n{h_lines}\n\n"
            f"Descriptions ({len(descriptions)}):\n{d_lines}"
        )
    return (
        f"Создать объявление (RSA) в группе «{ad_group}» — на паузе.\n"
        f"Ссылка: {final_url}\n\n"
        f"Заголовки ({len(headlines)}):\n{h_lines}\n\n"
        f"Описания ({len(descriptions)}):\n{d_lines}"
    )


def fmt_rsa_list_block(session, lang: str | None = None) -> str:
    """List-UX (§10): ВЕСЬ набор заголовков+описаний одним РЕДАКТИРУЕМЫМ текстом (ПЛЕЙН, без HTML —
    менеджер копирует ровно то, что видит), с нумерацией и подсказкой длины [n/лимит]. Правит по
    строке и присылает обратно — разбирает parse_rsa_paste. Секции-заголовки помогают парсеру."""
    from adcopy.validate import LIMITS, rsa_len

    en = _lang(lang) == "en"
    hl, dl = LIMITS["headline"], LIMITS["description"]
    h_hdr = f"HEADLINES (≤{hl}):" if en else f"ЗАГОЛОВКИ (≤{hl}):"
    d_hdr = f"DESCRIPTIONS (≤{dl}):" if en else f"ОПИСАНИЯ (≤{dl}):"
    lines = [h_hdr]
    for i, e in enumerate(session.headlines):
        t = e.get("text", "")
        lines.append(f"{i + 1}. {t}  [{rsa_len(t)}/{hl}]")
    lines.append("")
    lines.append(d_hdr)
    for i, e in enumerate(session.descriptions):
        t = e.get("text", "")
        lines.append(f"{i + 1}. {t}  [{rsa_len(t)}/{dl}]")
    return "\n".join(lines)


def fmt_kw_candidates(keywords: list[str]) -> str:
    """List-UX §7: кандидаты-ключи одним РЕДАКТИРУЕМЫМ текстом — по одному в строке (плейн, без HTML:
    менеджер копирует, удаляет лишние/добавляет свои и присылает обратно)."""
    return "\n".join(keywords)


def _rsa_section(line: str) -> str | None:
    """Строка-заголовок секции ('ЗАГОЛОВКИ (≤30):' → 'h'; 'ОПИСАНИЯ (≤90):' → 'd'). Требуем И начало
    с маркера, И признак ИМЕННО заголовка ('≤' или хвостовое ':') — иначе контент-строка, начинающаяся
    со слова «Заголовок…»/«Описание…» (реальный текст объявления), ложно съедалась бы как секция."""
    u = (line or "").strip().upper()
    if "≤" not in u and not u.endswith(":"):  # не похоже на заголовок секции — это контент
        return None
    if u.startswith("ЗАГОЛОВК") or u.startswith("HEADLINE"):
        return "h"
    if u.startswith("ОПИСАН") or u.startswith("DESCRIPTION"):
        return "d"
    return None


def _rsa_clean_line(line: str) -> str:
    """Снять с присланной строки нумерацию '1.'/'1)', хвост-аннотацию '[n/m]' и обрамляющие кавычки."""
    s = re.sub(r"^\s*\d+[.)]\s*", "", line or "")  # нумерация в начале
    s = re.sub(r"\s*\[\s*\d+\s*/\s*\d+\s*\]\s*$", "", s)  # хвост [n/m]
    return s.strip().strip('«»"“”').strip()


def parse_rsa_paste(text: str) -> tuple[list[str], list[str]]:
    """Разобрать присланный менеджером список обратно в (headlines, descriptions). Толерантно:
    • если есть строки-заголовки секций — делим по ним; • иначе делим по ПЕРВОЙ пустой строке
    (до неё заголовки, после — описания). В строках снимаем нумерацию/аннотацию/кавычки, пустые и
    заголовочные строки пропускаем. Валидацию количества/длины делает вызывающий (bot.main)."""
    raw_lines = (text or "").split("\n")
    if any(_rsa_section(ln) for ln in raw_lines):
        headlines: list[str] = []
        descriptions: list[str] = []
        bucket: str | None = None
        for ln in raw_lines:
            sec = _rsa_section(ln)
            if sec:
                bucket = sec
                continue
            clean = _rsa_clean_line(ln)
            if clean and bucket is not None:
                (headlines if bucket == "h" else descriptions).append(clean)
        return headlines, descriptions
    # Без заголовков: две группы, разделённые ПЕРВОЙ пустой строкой (заголовки → описания).
    groups: list[list[str]] = [[], []]
    gi = 0
    saw_content = False
    for ln in raw_lines:
        if ln.strip() == "":
            if saw_content and gi == 0:
                gi = 1  # первая пустая строка после контента → переключаемся на описания
            continue
        clean = _rsa_clean_line(ln)
        if not clean:
            continue
        saw_content = True
        groups[gi].append(clean)
    return groups[0], groups[1]


SEARCH_ASK_BRIEF = (
    "🆕 <b>Новая поисковая кампания</b>\n"
    "Пришли одним сообщением через <code>|</code>:\n"
    "<code>Название | https://сайт | дневной_бюджет [| тематика [| ключ1, ключ2]]</code>\n\n"
    "Например:\n"
    "<code>Доставка цветов | https://flowers.ua | 300 | доставка букетов Киев | "
    "доставка цветов, букет роз</code>\n\n"
    "Я сгенерирую заголовки/описания (RSA) и покажу черновик. Кампания создаётся "
    "<b>на паузе</b> — запуск отдельным действием."
)
SEARCH_GENERATING = "⏳ Генерирую тексты объявления (RSA) для кампании…"
SEARCH_GEN_EMPTY = (
    "Не удалось сгенерировать достаточно текстов (нужно ≥3 заголовков и ≥2 описаний). "
    "Попробуй другую тематику: /newsearch"
)
SEARCH_BAD_BRIEF = (
    "Неверный формат. Нужно: <code>Название | https://сайт | бюджет "
    "[| тематика [| ключи через запятую]]</code>\n"
    "Бюджет — число в валюте аккаунта (0 &lt; бюджет ≤ 1 000 000). Пришли ещё раз."
)


def _targeting_block(
    lng: str,
    *,
    geo_locations: list[str] | None,
    languages: list[str] | None,
    networks: str | None,
    ad_schedule: str | None,
    start_date: str | None,
    end_date: str | None,
) -> str:
    """P1-A: блок таргетинга (гео/язык/сети/расписание/даты) для карточки подтверждения §5/§19.8 —
    менеджер видит, на КОГО/КОГДА пойдёт кампания, ДО нажатия ✅ (пустое гео = глобальный показ
    отлавливается глазами). Пустой блок, если ничего не передано (обратная совместимость /clone)."""
    if not any([geo_locations, languages, networks, ad_schedule, start_date, end_date]):
        return ""
    en = lng == "en"
    geo = ", ".join(geo_locations or []) or ("all (global!)" if en else "все (глобально!)")
    langs = ", ".join(languages or []) or "—"
    if en:
        nets = "Search + partners" if networks == "search_partners" else "Search"
        sched = ad_schedule or "24/7"
        dates = f"{start_date or 'today'} — {end_date or 'no end date'}"
        return (
            f"\n\nGeo: {geo} · Language: {langs}\n"
            f"Networks: {nets} · Ad schedule: {sched}\n"
            f"Dates: {dates}"
        )
    nets = "Search + партнёры" if networks == "search_partners" else "Search"
    sched = ad_schedule or "24/7"
    dates = f"{start_date or 'сегодня'} — {end_date or 'без даты конца'}"
    return f"\n\nГЕО: {geo} · Язык: {langs}\nСети: {nets} · Расписание: {sched}\nДаты: {dates}"


def fmt_search_proposal_summary(
    name: str,
    url: str,
    budget_units: float,
    headlines: list[str],
    descriptions: list[str],
    keywords: list[str],
    match_type: str,
    lang: str | None = None,
    *,
    cpc_units: float | None = None,
    currency: str = "",
    geo_locations: list[str] | None = None,
    languages: list[str] | None = None,
    networks: str | None = None,
    ad_schedule: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Плейн-текст сводка create_search_campaign для confirm-гейта (esc применяется при показе).
    P1-A: опц. таргетинг-поля (гео/язык/сети/расписание/даты) выводятся блоком, если переданы —
    полное «было→станет» в точке ✅ (§5/§19.8). /clone их НЕ передаёт (там гео не переносится).
    A7: cpc_units — max CPC-ставка (в валюте аккаунта), печатается строкой на карточке; раньше
    ставка была скрыта (валютно-слепой хардкод 0.5) и менеджер жал ✅ на невидимом значении."""
    lng = _lang(lang)
    cur_sfx = f" {currency}" if currency else ""
    cpc_line_ru = f"\nMax CPC-ставка: {cpc_units:g}{cur_sfx}" if cpc_units is not None else ""
    cpc_line_en = f"\nMax CPC bid: {cpc_units:g}{cur_sfx}" if cpc_units is not None else ""
    from adcopy.validate import LIMITS, rsa_len  # 2.10 (§19.8): «…заголовки и описания (с длинами)»

    hl, dl = LIMITS["headline"], LIMITS["description"]
    h_lines = "\n".join(f"  • {h} [{rsa_len(h)}/{hl}]" for h in headlines)
    d_lines = "\n".join(f"  • {d} [{rsa_len(d)}/{dl}]" for d in descriptions)
    tgt_block = _targeting_block(
        lng,
        geo_locations=geo_locations,
        languages=languages,
        networks=networks,
        ad_schedule=ad_schedule,
        start_date=start_date,
        end_date=end_date,
    )
    if lng == "en":
        kw_block = ""
        if keywords:
            shown = list(keywords)[:KW_INLINE_MAX]
            kw_lines = "\n".join(f"  • {k}" for k in shown)
            more = (
                f"\n  …{len(keywords) - KW_INLINE_MAX} more"
                if len(keywords) > KW_INLINE_MAX
                else ""
            )
            kw_block = (
                f"\n\nKeywords ({len(keywords)}, {match_type_human(match_type, lng)}):\n"
                f"{kw_lines}{more}"
            )
        return (
            f"Create a search campaign “{name}” — paused.\n"
            f"Link: {url}\n"
            f"Daily budget: {budget_units:g}{cur_sfx}"
            f"{cpc_line_en}"
            f"{tgt_block}\n\n"
            f"Headlines ({len(headlines)}):\n{h_lines}\n\n"
            f"Descriptions ({len(descriptions)}):\n{d_lines}"
            f"{kw_block}"
        )
    kw_block = ""
    if keywords:
        shown = list(keywords)[:KW_INLINE_MAX]
        kw_lines = "\n".join(f"  • {k}" for k in shown)
        more = f"\n  …ещё {len(keywords) - KW_INLINE_MAX}" if len(keywords) > KW_INLINE_MAX else ""
        kw_block = (
            f"\n\nКлючевые слова ({len(keywords)}, {match_type_human(match_type, lng)}):\n"
            f"{kw_lines}{more}"
        )
    return (
        f"Создать поисковую кампанию «{name}» — на паузе.\n"
        f"Ссылка: {url}\n"
        f"Дневной бюджет: {budget_units:g}{cur_sfx}"
        f"{cpc_line_ru}"
        f"{tgt_block}\n\n"
        f"Заголовки ({len(headlines)}):\n{h_lines}\n\n"
        f"Описания ({len(descriptions)}):\n{d_lines}"
        f"{kw_block}"
    )


def fmt_clone_proposal_summary(
    new_name: str,
    source: str,
    budget_units: float,
    params: dict,
    dropped_texts: int = 0,
    regenerated: bool = False,
    lang: str | None = None,
) -> str:
    """§2A: сводка клона — заголовок «клон из X» + тело create_search_campaign + честная сноска
    о том, что НЕ переносится (гео/минус-слова/стратегия/аудитории — применять отдельно).
    esc применяется при показе."""
    lng = _lang(lang)
    body = fmt_search_proposal_summary(
        new_name,
        params.get("final_url", ""),
        budget_units,
        params.get("headlines", []),
        params.get("descriptions", []),
        params.get("keywords", []),
        params.get("match_type", "phrase"),
        lang=lng,
    )
    if lng == "en":
        head = f"Clone of “{source}” → new campaign “{new_name}”.\n\n"
        notes = []
        if dropped_texts:
            notes.append(f"{dropped_texts} ad text(s) dropped (over length limit)")
        if regenerated:
            notes.append("ad copy regenerated (too few valid texts to clone)")
        note_block = ("\n\n" + "; ".join(notes)) if notes else ""
        tail = (
            "\n\nNot copied automatically: geo, negative keywords, bidding strategy, audiences — "
            "apply them separately after creation."
        )
        return head + body + note_block + tail
    head = f"Клон из «{source}» → новая кампания «{new_name}».\n\n"
    notes = []
    if dropped_texts:
        notes.append(f"{dropped_texts} текст(ов) объявления отброшено (превышали лимит длины)")
    if regenerated:
        notes.append("тексты сгенерированы заново (валидных для клона было мало)")
    note_block = ("\n\n" + "; ".join(notes)) if notes else ""
    tail = (
        "\n\nНе переносится автоматически: гео, минус-слова, стратегия ставок, аудитории — "
        "применить отдельно после создания."
    )
    return head + body + note_block + tail


def fmt_gdn_proposal_summary(
    name: str,
    url: str,
    budget_units: float,
    headlines: list[str],
    descriptions: list[str],
    business_name: str,
    geo_locations: list[str] | None = None,
    lang: str | None = None,
) -> str:
    """Плейн-текст сводка create_gdn_campaign для confirm-гейта (esc применяется при показе)."""
    h_lines = "\n".join(f"  • {h}" for h in headlines)
    d_lines = "\n".join(f"  • {d}" for d in descriptions)
    geo = ", ".join(geo_locations) if geo_locations else ""
    if _lang(lang) == "en":
        geo_line = f"Geo: {geo}\n" if geo else "Geo: all locations (not set)\n"
        return (
            f"Create a display campaign (GDN) “{name}” — paused.\n"
            f"Business: {business_name}\n"
            f"Link: {url}\n"
            f"Daily budget: {budget_units:g}\n"
            f"{geo_line}"
            "Image: 1 (cropped to 1.91:1 and 1:1)\n\n"
            f"Headlines ({len(headlines)}):\n{h_lines}\n\n"
            f"Descriptions ({len(descriptions)}):\n{d_lines}"
        )
    geo_line = f"ГЕО: {geo}\n" if geo else "ГЕО: все локации (не задано)\n"
    return (
        f"Создать медийную кампанию (GDN) «{name}» — на паузе.\n"
        f"Бизнес: {business_name}\n"
        f"Ссылка: {url}\n"
        f"Дневной бюджет: {budget_units:g}\n"
        f"{geo_line}"
        "Изображение: 1 (обрезано в 1.91:1 и 1:1)\n\n"
        f"Заголовки ({len(headlines)}):\n{h_lines}\n\n"
        f"Описания ({len(descriptions)}):\n{d_lines}"
    )


def fmt_video_proposal_summary(
    kind: str,
    name: str,
    url: str,
    youtube_id: str,
    budget_units: float,
    headlines: list[str],
    descriptions: list[str],
    business_name: str,
    geo_locations: list[str] | None = None,
    goal: str = "",
    with_logo: bool = False,
    lang: str | None = None,
) -> str:
    """§11: плейн-текст сводка create_demand_gen_campaign / create_video_campaign для confirm-гейта
    (esc применяется при показе). kind: 'dg' | 'video'."""
    h_lines = "\n".join(f"  • {h}" for h in headlines)
    d_lines = "\n".join(f"  • {d}" for d in descriptions)
    geo = ", ".join(geo_locations) if geo_locations else ""
    en = _lang(lang) == "en"
    if en:
        kind_h = "Demand Gen campaign" if kind == "dg" else "Video campaign (reach, CPM)"
        goal_line = (
            f"Goal: {'conversions (Maximize Conversions)' if goal == 'conversions' else 'clicks (Maximize Clicks)'}\n"
            if kind == "dg"
            else ""
        )
        logo_line = ("Logo: yes (1:1)\n" if with_logo else "Logo: no\n") if kind == "dg" else ""
        geo_line = f"Geo: {geo}\n" if geo else "Geo: all locations (not set)\n"
        return (
            f"Create a {kind_h} “{name}” — paused.\n"
            f"Business: {business_name}\n"
            f"Site: {url}\n"
            f"YouTube video: {youtube_id}\n"
            f"Daily budget: {budget_units:g}\n"
            f"{goal_line}{logo_line}{geo_line}\n"
            f"Headlines ({len(headlines)}):\n{h_lines}\n\n"
            f"Descriptions ({len(descriptions)}):\n{d_lines}"
        )
    kind_h = "Demand Gen кампанию" if kind == "dg" else "видеокампанию (охват, CPM)"
    goal_line = (
        f"Цель: {'конверсии (Maximize Conversions)' if goal == 'conversions' else 'клики (Maximize Clicks)'}\n"
        if kind == "dg"
        else ""
    )
    logo_line = ("Логотип: есть (1:1)\n" if with_logo else "Логотип: нет\n") if kind == "dg" else ""
    geo_line = f"ГЕО: {geo}\n" if geo else "ГЕО: все локации (не задано)\n"
    return (
        f"Создать {kind_h} «{name}» — на паузе.\n"
        f"Бизнес: {business_name}\n"
        f"Сайт: {url}\n"
        f"YouTube-видео: {youtube_id}\n"
        f"Дневной бюджет: {budget_units:g}\n"
        f"{goal_line}{logo_line}{geo_line}\n"
        f"Заголовки ({len(headlines)}):\n{h_lines}\n\n"
        f"Описания ({len(descriptions)}):\n{d_lines}"
    )


# ── Человекочитаемая сводка черновика мутации (ТЗ §5) ────────────────────────────
KW_INLINE_MAX = 20  # ключей показываем в сводке черновика; больше — во вложении .xlsx
_CURRENCY_HUMAN = {
    "USD": "USD",
    "UAH": "грн",
    "EUR": "EUR",
    "AUD": "AUD",
    "CZK": "Kč",
    "PLN": "zł",
    "GBP": "GBP",
    "percent": "%",
}


def match_type_human(mt: str, lang: str | None = None) -> str:
    """broad/phrase/exact → человекочитаемый тип соответствия."""
    if _lang(lang) == "en":
        return {"broad": "broad", "phrase": "phrase", "exact": "exact"}.get(
            str(mt).lower(), str(mt)
        )
    return {"broad": "широкое", "phrase": "фразовое", "exact": "точное"}.get(
        str(mt).lower(), str(mt)
    )


def keyword_action_label(operation: str, lang: str | None = None) -> str:
    if _lang(lang) == "en":
        return {
            "add_keywords": "Add keywords",
            "remove_keywords": "Remove keywords",
            "add_negative_keywords": "Add negative keywords",
            "remove_negative_keywords": "Remove negative keywords",
            "create_search_campaign": "Keywords of the new campaign",
        }.get(operation, operation)
    return {
        "add_keywords": "Добавить ключевые слова",
        "remove_keywords": "Удалить ключевые слова",
        "add_negative_keywords": "Добавить минус-слова",
        "remove_negative_keywords": "Удалить минус-слова",
        "create_search_campaign": "Ключи новой кампании",
    }.get(operation, operation)


def _fmt_micros(micros: int, currency: str | None = None) -> str:
    """micros (1e6 = единица валюты аккаунта) → «12 480.00» (разделитель тысяч, 2 знака).

    Zero-decimal валюта (JPY/UGX/KRW…) — БЕЗ дробной части: «1 500 JPY», а не «1 500.00 JPY»
    (копеек у неё не бывает, и «0.50» на карточке «было→станет» читалось бы как пол-йены)."""
    try:
        dec = 0 if (currency or "").strip().upper() in ZERO_DECIMAL_CURRENCIES else 2
        return _thou(int(micros) / 1_000_000, dec)
    except (TypeError, ValueError):
        return str(micros)


def _micros_range(values: list, currency: str | None = None) -> str:
    """Диапазон ставок групп: одинаковые → одно число, иначе «min–max»."""
    nums = [int(x) for x in values] if values else []
    if not nums:
        return "—"
    lo, hi = min(nums), max(nums)
    return (
        _fmt_micros(lo, currency)
        if lo == hi
        else f"{_fmt_micros(lo, currency)}–{_fmt_micros(hi, currency)}"
    )


def _before(params: dict) -> dict | None:
    """Снимок текущего значения из proposal.params['_before'] (см. ads.service.read_before)."""
    b = params.get("_before")
    return b if isinstance(b, dict) else None


def _geo_before_str(b: dict, en: bool) -> str:
    """D6: текущее ГЕО из снимка _before (kind='geo') одной строкой: локации + радиусы, либо
    «все регионы» (пустой таргетинг = показ везде — это НЕ ошибка)."""
    parts = list(b.get("before_locations") or []) + list(b.get("before_proximity") or [])
    if not parts:
        return "all regions" if en else "все регионы"
    return ", ".join(str(x) for x in parts)


# ENUM-имя стратегии (MAXIMIZE_CONVERSIONS) ИЛИ схемное значение (maximize_conversions) → человек.
# Отдельно от _BIDDING_HUMAN (ниже, RU/EN-nested, только схемные ключи) — этот покрывает и ENUM
# Google (для «было» из read_before), и расширенные типы (tCPA/tROAS/eCPC/tIS).
_BIDDING_STRAT_HUMAN = {
    "manual_cpc": ("Ручная CPC", "Manual CPC"),
    "maximize_conversions": ("Максимум конверсий", "Maximize conversions"),
    "maximize_conversion_value": ("Максимум ценности конверсий", "Maximize conversion value"),
    "target_spend": ("Максимум кликов", "Maximize clicks"),
    "target_cpa": ("Целевая цена конверсии (CPA)", "Target CPA"),
    "target_roas": ("Целевая рентабельность (ROAS)", "Target ROAS"),
    "enhanced_cpc": ("Улучшенная CPC", "Enhanced CPC"),
    "target_impression_share": ("Целевой процент показов", "Target impression share"),
    "": ("прежняя стратегия", "current strategy"),
}


def _bidding_human(value: str, en: bool) -> str:
    """D6: имя стратегии ставок для показа. Принимает и ENUM Google (UPPER), и схемное (lower)."""
    key = (value or "").strip().lower()
    ru, eng = _BIDDING_STRAT_HUMAN.get(key, (value or "", value or ""))
    return eng if en else ru


def _mode_sign(mode: str | None) -> str:
    """Знак изменения на карточке: направление несёт mode (value всегда >0). «−» (U+2212), не дефис:
    в «100 → 80 (-20%)» дефис визуально теряется, а перепутать направление на денежной карточке —
    ровно тот класс ошибки, из-за которого decrease_* и появился."""
    return "−" if str(mode or "").startswith("decrease") else "+"


def _money_summary(label: str, params: dict, lang: str | None = None) -> str:
    c = params.get("campaign", "")
    mode = params.get("mode")
    sign = _mode_sign(mode)
    pct = str(mode or "").endswith("_by_percent")  # increase/decrease_by_percent
    amt = str(mode or "").endswith("_by_amount")  # increase/decrease_by_amount
    # currency=None (не указана → валюта аккаунта) → код валюты не печатаем (числа уже в валюте
    # аккаунта; сверка валюты — на предпросмотре). 'percent' → «%». Неизвестное → без кода.
    cur = _CURRENCY_HUMAN.get(params.get("currency") or "", "")
    try:
        v = f"{float(params.get('value')):g}"
    except (TypeError, ValueError):
        v = str(params.get("value"))
    b = _before(params)
    if _lang(lang) == "en":
        # §5: реальное «было → станет», если снимок прочитан (числа — в валюте аккаунта).
        if b and b.get("kind") == "budget" and b.get("before_micros") is not None:
            acc = b.get("currency")  # валюта АККАУНТА из снимка (не params: там валюта запроса)
            before_s = _fmt_micros(b["before_micros"], acc)
            after_s = _fmt_micros(b.get("after_micros"), acc)
            if pct:
                tail = f" ({sign}{v}%)"
            elif amt:
                tail = f" ({sign}{f'{v} {cur}'.strip()})"
            else:
                tail = ""
            return f"Campaign “{c}” — {label}: {before_s} → {after_s}{tail}".rstrip()
        if pct:
            return f"Campaign “{c}” — {label}: {sign}{v}%"
        if amt:
            return f"Campaign “{c}” — {label}: {sign}{v} {cur}".rstrip()
        if mode == "set_to":
            return f"Campaign “{c}” — {label} → {v} {cur}".rstrip()
        return f"Campaign “{c}” — {label}: {v} {cur}".rstrip()
    # §5: реальное «было → станет», если снимок прочитан (числа — в валюте аккаунта).
    if b and b.get("kind") == "budget" and b.get("before_micros") is not None:
        acc = b.get("currency")
        before_s = _fmt_micros(b["before_micros"], acc)
        after_s = _fmt_micros(b.get("after_micros"), acc)
        if pct:
            tail = f" ({sign}{v}%)"
        elif amt:
            tail = f" ({sign}{f'{v} {cur}'.strip()})"  # cur='' (валюта аккаунта) → «(+10)», без лишнего пробела
        else:
            tail = ""
        return f"Кампания «{c}» — {label}: {before_s} → {after_s}{tail}".rstrip()
    # fallback без «было» (чтение не удалось / старый черновик)
    if pct:
        return f"Кампания «{c}» — {label}: {sign}{v}%"
    if amt:
        return f"Кампания «{c}» — {label}: {sign}{v} {cur}".rstrip()
    if mode == "set_to":
        return f"Кампания «{c}» — {label} → {v} {cur}".rstrip()
    return f"Кампания «{c}» — {label}: {v} {cur}".rstrip()


def _bid_summary(params: dict, lang: str | None = None) -> str:
    c = params.get("campaign", "")
    mode = params.get("mode")
    sign = _mode_sign(mode)
    pct = str(mode or "").endswith("_by_percent")
    try:
        v = f"{float(params.get('value')):g}"
    except (TypeError, ValueError):
        v = str(params.get("value"))
    b = _before(params)
    if _lang(lang) == "en":
        if b and b.get("kind") == "bid" and b.get("before_micros"):
            n = b.get("n_groups") or len(b["before_micros"])
            acc = b.get("currency")
            rng_b = _micros_range(b["before_micros"], acc)
            rng_a = _micros_range(b.get("after_micros") or [], acc)
            if pct:
                return (
                    f"Campaign “{c}” — CPC bid: {sign}{v}% (current {rng_b} → {rng_a}; groups: {n})"
                )
            return f"Campaign “{c}” — CPC bid: {rng_b} → {rng_a} (groups: {n})"
        return _money_summary("CPC bid", params, lang)
    if b and b.get("kind") == "bid" and b.get("before_micros"):
        n = b.get("n_groups") or len(b["before_micros"])
        acc = b.get("currency")
        rng_b = _micros_range(b["before_micros"], acc)
        rng_a = _micros_range(b.get("after_micros") or [], acc)
        if pct:
            return (
                f"Кампания «{c}» — ставка CPC: {sign}{v}% (текущие {rng_b} → {rng_a}; групп: {n})"
            )
        return f"Кампания «{c}» — ставка CPC: {rng_b} → {rng_a} (групп: {n})"
    return _money_summary("ставка CPC", params, lang)


def fmt_mutation_summary(operation: str, params: dict, lang: str | None = None) -> str:
    """Человекочитаемая сводка черновика «было → станет»/действия (plain text; esc — при показе).

    Для ключей — тип соответствия словами + список (усечён до KW_INLINE_MAX; полный — во вложении).
    Возвращает '' для операций со своим богатым форматтером (create_rsa/create_gdn_campaign) — тогда
    вызывающий оставляет собственный summary. Заменяет «сырой dict» из confirm.gate.build_summary."""
    if not isinstance(params, dict):
        return ""
    lng = _lang(lang)
    c = params.get("campaign", "")
    if lng == "en":
        return _mutation_summary_en(operation, params, c)
    if operation == "update_budget":
        return _money_summary("бюджет", params, lng)
    if operation == "update_bid":
        return _bid_summary(params, lng)
    if operation in ("pause_campaign", "resume_campaign"):
        # §5: показываем текущий статус → новый, если снимок прочитан.
        b = _before(params)
        new = "на паузе ⏸" if operation == "pause_campaign" else "включена ▶️"
        if b and b.get("kind") == "status" and b.get("before_status"):
            return f"Кампания «{c}»: {status_human(b['before_status'], lng)} → {new}"
        verb = "поставить на паузу" if operation == "pause_campaign" else "возобновить"
        return f"Кампания «{c}» — {verb}."
    if operation == "launch_campaign":
        # §19.8/§11: запуск включает кампанию ПОЛНОСТЬЮ (кампания + группы + объявления) — говорим
        # об этом явно, чтобы менеджер понимал: показы пойдут (не только статус кампании сменится).
        b = _before(params)
        base = (
            f"Кампания «{c}» — 🚀 ЗАПУСТИТЬ (включить полностью: кампания + группы + объявления)."
        )
        if b and b.get("kind") == "status" and b.get("before_status"):
            return f"Кампания «{c}»: {status_human(b['before_status'], lng)} → включена полностью ▶️.\n{base}"
        return base
    if operation == "update_campaign":
        # §3 «изменение» кампании: переименование. Показываем старое → новое имя.
        new = params.get("new_name", "")
        b = _before(params)
        old = b.get("before_name") if (b and b.get("kind") == "name") else c
        return f"Кампания «{old}» → переименовать в «{new}»."
    if operation == "set_campaign_network":
        # §19.3: тумблер поисковых партнёров. «Было→станет», если снимок есть.
        after = "ВКЛ" if params.get("search_partners") else "ВЫКЛ"
        b = _before(params)
        if b and b.get("kind") == "network":
            before = "ВКЛ" if b.get("before_search_partners") else "ВЫКЛ"
            return f"Кампания «{c}»: поисковые партнёры {before} → {after}."
        return f"Кампания «{c}»: поисковые партнёры → {after}."
    if operation == "remove_campaign":
        return f"🗑 УДАЛИТЬ кампанию «{c}» целиком.\n⚠️ Действие необратимо (статус станет REMOVED)."
    if operation == "remove_ad_group":
        ag = params.get("ad_group", "")
        return f"🗑 УДАЛИТЬ группу «{ag}» (кампания «{c}»).\n⚠️ Действие необратимо."
    if operation in ("pause_ad", "resume_ad"):
        ag = params.get("ad_group", "")
        ad = params.get("ad", "")
        new = "на паузе ⏸" if operation == "pause_ad" else "включено ▶️"
        b = _before(params)
        if b and b.get("kind") == "status" and b.get("before_status"):
            return (
                f"Объявление «{ad}» (группа «{ag}», кампания «{c}»): "
                f"{status_human(b['before_status'], lng)} → {new}"
            )
        verb = "поставить на паузу" if operation == "pause_ad" else "возобновить"
        return f"Объявление «{ad}» (группа «{ag}», кампания «{c}») — {verb}."
    if operation == "remove_ad":
        ag = params.get("ad_group", "")
        ad = params.get("ad", "")
        return (
            f"🗑 УДАЛИТЬ объявление «{ad}» (группа «{ag}», кампания «{c}»).\n"
            "⚠️ Действие необратимо (статус станет REMOVED)."
        )
    if operation in ("pause_ad_group", "resume_ad_group"):
        ag = params.get("ad_group", "")
        b = _before(params)
        new = "на паузе ⏸" if operation == "pause_ad_group" else "включена ▶️"
        if b and b.get("kind") == "status" and b.get("before_status"):
            return (
                f"Группа «{ag}» (кампания «{c}»): {status_human(b['before_status'], lng)} → {new}"
            )
        verb = "поставить на паузу" if operation == "pause_ad_group" else "возобновить"
        return f"Группа «{ag}» (кампания «{c}») — {verb}."
    if operation == "set_geo_proximity":
        try:
            rs = f"{float(params.get('radius_km')):g}"
        except (TypeError, ValueError):
            rs = str(params.get("radius_km"))
        city = params.get("city_name", "")
        cc = params.get("country_code", "")
        after = f"радиус {rs} км вокруг «{city}» ({cc})"
        b = _before(params)
        if b and b.get("kind") == "geo":  # D6: реальное «было → станет»
            return f"Кампания «{c}» — гео: {_geo_before_str(b, False)} → {after}."
        return f"Кампания «{c}» — {after}. Заменит прежний гео-радиус."
    if operation == "set_geo_location":
        locs = ", ".join(str(x) for x in (params.get("locations") or []))
        cc = params.get("country_code", "")
        after = f"{locs} ({cc})"
        b = _before(params)
        if b and b.get("kind") == "geo":  # D6: реальное «было → станет»
            return f"Кампания «{c}» — гео-таргетинг: {_geo_before_str(b, False)} → {after}."
        return (
            f"Кампания «{c}» — гео-таргетинг: {after}. "
            "Заменит прежний географический таргетинг кампании."
        )
    if operation == "set_bidding_strategy":
        strat = _bidding_human(params.get("strategy", ""), False)
        extra = ""
        if params.get("target_cpa"):
            extra = f", target CPA {float(params['target_cpa']):g}"
        elif params.get("target_roas"):
            extra = f", target ROAS {float(params['target_roas']):g}"
        elif params.get("strategy") == "manual_cpc" and params.get("enhanced_cpc"):
            extra = ", enhanced CPC"
        b = _before(params)
        if b and b.get("kind") == "bidding":  # D6: реальное «было → станет»
            return (
                f"Кампания «{c}» — стратегия ставок: "
                f"{_bidding_human(b.get('before_strategy', ''), False)} → {strat}{extra}."
            )
        return f"Кампания «{c}» — стратегия ставок → {strat}{extra}."
    if operation in (
        "add_keywords",
        "remove_keywords",
        "add_negative_keywords",
        "remove_negative_keywords",
    ):
        kws = params.get("keywords") or []
        mt = match_type_human(params.get("match_type", ""), lng)
        negatives = operation in ("add_negative_keywords", "remove_negative_keywords")
        removals = operation in ("remove_keywords", "remove_negative_keywords")
        what = "минус-слов" if negatives else "ключевых слов"
        verb = "удалить" if removals else "добавить"
        head = f"Кампания «{c}» — {verb} {len(kws)} {what} (тип соответствия: {mt}):"
        shown = list(kws)[:KW_INLINE_MAX]
        lines = "\n".join(f"  • {k}" for k in shown)
        if len(kws) > KW_INLINE_MAX:
            lines += f"\n  …ещё {len(kws) - KW_INLINE_MAX} — полный список во вложении .xlsx"
        return f"{head}\n{lines}"
    if operation in ("attach_audience", "detach_audience"):
        names = params.get("_audience_names") or []  # дружелюбные имена (инертны для исполнения)
        rns = params.get("audience_resource_names") or []
        label = ", ".join(str(n) for n in names) if names else f"{len(rns)} шт."
        if operation == "detach_audience":
            return f"Кампания «{c}» — открепить аудиторию от таргетинга: {label}."
        return (
            f"Кампания «{c}» — прикрепить аудиторию к таргетингу: {label}. "
            "Показы пойдут выбранной аудитории."
        )
    if operation == "add_sitelinks":
        sls = params.get("sitelinks") or []
        lines = "\n".join(f"  • {s.get('link_text', '')} → {s.get('final_url', '')}" for s in sls)
        return f"Кампания «{c}» — добавить быстрые ссылки ({len(sls)}):\n{lines}"
    if operation == "add_callouts":
        cs = params.get("callouts") or []
        return f"Кампания «{c}» — добавить уточнения ({len(cs)}): " + ", ".join(str(x) for x in cs)
    if operation == "add_structured_snippets":
        vals = params.get("values") or []
        return f"Кампания «{c}» — структурное описание «{params.get('header', '')}»: " + ", ".join(
            str(x) for x in vals
        )
    if operation == "attach_image_asset":
        return f"Кампания «{c}» — добавить изображение-ассет (обрезано до 1.91:1)."
    if operation == "add_call_asset":
        return (
            f"Кампания «{c}» — телефон-расширение: {params.get('phone_number', '')} "
            f"({params.get('country_code', 'UA')})."
        )
    if operation == "add_promotion":
        if params.get("percent_off") is not None:
            disc = f"-{params['percent_off']:g}%"
        else:
            disc = f"-{params.get('money_off_units', '')} {params.get('currency', '')}"
        code = f", промокод {params['promo_code']}" if params.get("promo_code") else ""
        return f"Кампания «{c}» — промо: «{params.get('promotion_target', '')}» {disc}{code}."
    if operation == "add_price_asset":
        offs = params.get("offerings") or []
        lines = "\n".join(
            f"  • {o.get('header', '')}: {o.get('price_units', '')} {params.get('currency', '')}"
            for o in offs
        )
        return (
            f"Кампания «{c}» — прайс ({len(offs)} оферов, {params.get('currency', '')}):\n{lines}"
        )
    if operation == "remove_asset_link":
        n = len(params.get("link_resource_names") or [])
        return f"Открепить расширения от кампании: {n} шт. (ассеты не удаляются)."
    return ""  # create_rsa / create_gdn_campaign / неизвестное — оставить summary вызывающего


def _mutation_summary_en(operation: str, params: dict, c: str) -> str:
    """EN-ветка fmt_mutation_summary: те же operation/params/плейсхолдеры, английская формулировка.
    RU-ветка остаётся источником истины по структуре — здесь зеркально по-английски."""
    if operation == "update_budget":
        return _money_summary("budget", params, "en")
    if operation == "update_bid":
        return _bid_summary(params, "en")
    if operation in ("pause_campaign", "resume_campaign"):
        b = _before(params)
        new = "paused ⏸" if operation == "pause_campaign" else "enabled ▶️"
        if b and b.get("kind") == "status" and b.get("before_status"):
            return f"Campaign “{c}”: {status_human(b['before_status'], 'en')} → {new}"
        verb = "pause" if operation == "pause_campaign" else "resume"
        return f"Campaign “{c}” — {verb}."
    if operation == "launch_campaign":
        b = _before(params)
        base = f"Campaign “{c}” — 🚀 LAUNCH (enable fully: campaign + ad groups + ads)."
        if b and b.get("kind") == "status" and b.get("before_status"):
            return f"Campaign “{c}”: {status_human(b['before_status'], 'en')} → fully enabled ▶️.\n{base}"
        return base
    if operation == "update_campaign":
        new = params.get("new_name", "")
        b = _before(params)
        old = b.get("before_name") if (b and b.get("kind") == "name") else c
        return f"Campaign “{old}” → rename to “{new}”."
    if operation == "set_campaign_network":
        after = "ON" if params.get("search_partners") else "OFF"
        b = _before(params)
        if b and b.get("kind") == "network":
            before = "ON" if b.get("before_search_partners") else "OFF"
            return f"Campaign “{c}”: search partners {before} → {after}."
        return f"Campaign “{c}”: search partners → {after}."
    if operation == "remove_campaign":
        return f"🗑 DELETE the whole campaign “{c}”.\n⚠️ Irreversible (status becomes REMOVED)."
    if operation == "remove_ad_group":
        ag = params.get("ad_group", "")
        return f"🗑 DELETE ad group “{ag}” (campaign “{c}”).\n⚠️ Irreversible."
    if operation in ("pause_ad", "resume_ad"):
        ag = params.get("ad_group", "")
        ad = params.get("ad", "")
        new = "paused ⏸" if operation == "pause_ad" else "enabled ▶️"
        b = _before(params)
        if b and b.get("kind") == "status" and b.get("before_status"):
            return (
                f"Ad “{ad}” (ad group “{ag}”, campaign “{c}”): "
                f"{status_human(b['before_status'], 'en')} → {new}"
            )
        verb = "pause" if operation == "pause_ad" else "resume"
        return f"Ad “{ad}” (ad group “{ag}”, campaign “{c}”) — {verb}."
    if operation == "remove_ad":
        ag = params.get("ad_group", "")
        ad = params.get("ad", "")
        return (
            f"🗑 DELETE ad “{ad}” (ad group “{ag}”, campaign “{c}”).\n"
            "⚠️ Irreversible (status becomes REMOVED)."
        )
    if operation in ("pause_ad_group", "resume_ad_group"):
        ag = params.get("ad_group", "")
        b = _before(params)
        new = "paused ⏸" if operation == "pause_ad_group" else "enabled ▶️"
        if b and b.get("kind") == "status" and b.get("before_status"):
            return f"Ad group “{ag}” (campaign “{c}”): {status_human(b['before_status'], 'en')} → {new}"
        verb = "pause" if operation == "pause_ad_group" else "resume"
        return f"Ad group “{ag}” (campaign “{c}”) — {verb}."
    if operation == "set_geo_proximity":
        try:
            rs = f"{float(params.get('radius_km')):g}"
        except (TypeError, ValueError):
            rs = str(params.get("radius_km"))
        city = params.get("city_name", "")
        cc = params.get("country_code", "")
        after = f"{rs} km radius around “{city}” ({cc})"
        b = _before(params)
        if b and b.get("kind") == "geo":  # D6: real before → after
            return f"Campaign “{c}” — geo: {_geo_before_str(b, True)} → {after}."
        return f"Campaign “{c}” — {after}. Replaces the prior geo-radius."
    if operation == "set_geo_location":
        locs = ", ".join(str(x) for x in (params.get("locations") or []))
        cc = params.get("country_code", "")
        after = f"{locs} ({cc})"
        b = _before(params)
        if b and b.get("kind") == "geo":  # D6: real before → after
            return f"Campaign “{c}” — geo-targeting: {_geo_before_str(b, True)} → {after}."
        return (
            f"Campaign “{c}” — geo-targeting: {after}. "
            "Replaces the campaign's prior geographic targeting."
        )
    if operation == "set_bidding_strategy":
        strat = _bidding_human(params.get("strategy", ""), True)
        extra = ""
        if params.get("target_cpa"):
            extra = f", target CPA {float(params['target_cpa']):g}"
        elif params.get("target_roas"):
            extra = f", target ROAS {float(params['target_roas']):g}"
        elif params.get("strategy") == "manual_cpc" and params.get("enhanced_cpc"):
            extra = ", enhanced CPC"
        b = _before(params)
        if b and b.get("kind") == "bidding":  # D6: real before → after
            return (
                f"Campaign “{c}” — bidding strategy: "
                f"{_bidding_human(b.get('before_strategy', ''), True)} → {strat}{extra}."
            )
        return f"Campaign “{c}” — bidding strategy → {strat}{extra}."
    if operation in (
        "add_keywords",
        "remove_keywords",
        "add_negative_keywords",
        "remove_negative_keywords",
    ):
        kws = params.get("keywords") or []
        mt = match_type_human(params.get("match_type", ""), "en")
        negatives = operation in ("add_negative_keywords", "remove_negative_keywords")
        removals = operation in ("remove_keywords", "remove_negative_keywords")
        what = "negative keywords" if negatives else "keywords"
        verb = "remove" if removals else "add"
        head = f"Campaign “{c}” — {verb} {len(kws)} {what} (match type: {mt}):"
        shown = list(kws)[:KW_INLINE_MAX]
        lines = "\n".join(f"  • {k}" for k in shown)
        if len(kws) > KW_INLINE_MAX:
            lines += f"\n  …{len(kws) - KW_INLINE_MAX} more — full list in the .xlsx attachment"
        return f"{head}\n{lines}"
    if operation in ("attach_audience", "detach_audience"):
        names = params.get("_audience_names") or []
        rns = params.get("audience_resource_names") or []
        label = ", ".join(str(n) for n in names) if names else f"{len(rns)} item(s)"
        if operation == "detach_audience":
            return f"Campaign “{c}” — detach audience from targeting: {label}."
        return (
            f"Campaign “{c}” — attach audience to targeting: {label}. "
            "Impressions will go to the chosen audience."
        )
    if operation == "add_sitelinks":
        sls = params.get("sitelinks") or []
        lines = "\n".join(f"  • {s.get('link_text', '')} → {s.get('final_url', '')}" for s in sls)
        return f"Campaign “{c}” — add sitelinks ({len(sls)}):\n{lines}"
    if operation == "add_callouts":
        cs = params.get("callouts") or []
        return f"Campaign “{c}” — add callouts ({len(cs)}): " + ", ".join(str(x) for x in cs)
    if operation == "add_structured_snippets":
        vals = params.get("values") or []
        return f"Campaign “{c}” — structured snippet “{params.get('header', '')}”: " + ", ".join(
            str(x) for x in vals
        )
    if operation == "attach_image_asset":
        return f"Campaign “{c}” — add an image asset (cropped to 1.91:1)."
    if operation == "add_call_asset":
        return (
            f"Campaign “{c}” — call extension: {params.get('phone_number', '')} "
            f"({params.get('country_code', 'UA')})."
        )
    if operation == "add_promotion":
        if params.get("percent_off") is not None:
            disc = f"-{params['percent_off']:g}%"
        else:
            disc = f"-{params.get('money_off_units', '')} {params.get('currency', '')}"
        code = f", code {params['promo_code']}" if params.get("promo_code") else ""
        return f"Campaign “{c}” — promotion: “{params.get('promotion_target', '')}” {disc}{code}."
    if operation == "add_price_asset":
        offs = params.get("offerings") or []
        lines = "\n".join(
            f"  • {o.get('header', '')}: {o.get('price_units', '')} {params.get('currency', '')}"
            for o in offs
        )
        return f"Campaign “{c}” — price ({len(offs)} offerings, {params.get('currency', '')}):\n{lines}"
    if operation == "remove_asset_link":
        n = len(params.get("link_resource_names") or [])
        return f"Detach {n} extension(s) from the campaign (assets are not deleted)."
    return ""  # create_rsa / create_gdn_campaign / unknown — keep caller's summary


# §7: короткая метка уровня конкуренции для чат-таблицы (полная — в .xlsx). UNSPECIFIED не показываем.
_COMP_RU = {"LOW": "конк. низк.", "MEDIUM": "конк. сред.", "HIGH": "конк. выс."}
_COMP_EN = {"LOW": "comp low", "MEDIUM": "comp med", "HIGH": "comp high"}


def _kw_metrics_suffix(idea, currency: str, en: bool) -> str:
    """§7: компактный хвост строки ключа — конкуренция · ставка top-of-page · пик сезона.
    Показываем ТОЛЬКО части, по которым есть данные (на тест-аккаунте ставки/сезон часто нулевые →
    не засоряем строку). Метрики уже посчитаны КОДОМ (ads.keyword_plan), здесь только формат."""
    if idea is None:
        return ""
    parts: list[str] = []
    comp = (_COMP_EN if en else _COMP_RU).get(getattr(idea, "competition", "") or "")
    if comp:
        # D8: индекс конкуренции 0..100 (точнее, чем low/med/high) — если Google его вернул.
        idx = int(getattr(idea, "competition_index", 0) or 0)
        parts.append(f"{comp} ({idx})" if idx > 0 else comp)
    low, high = getattr(idea, "low_bid", 0.0) or 0.0, getattr(idea, "high_bid", 0.0) or 0.0
    if high > 0:
        cur = f" {esc(currency)}" if currency else ""
        parts.append(f"{low:.2f}–{high:.2f}{cur}")
    peak = getattr(idea, "peak_month", "") or ""
    if peak:
        parts.append((f"peak {esc(peak)}") if en else (f"пик {esc(peak)}"))
    return (" · " + " · ".join(parts)) if parts else ""


def fmt_keywords_summary(
    clusters,
    by_text: dict,
    total: int,
    src: str,
    lang: str | None = None,
    *,
    by_idea: dict | None = None,
    currency: str = "",
    irrelevant: int = 0,
    off_topic=frozenset(),
) -> str:
    """Сводка keyword research: топ-кластеры с топ-ключами и метриками. Полная таблица — в .xlsx.

    clusters — объекты с .name/.intent/.keywords (duck-typed); by_text — {ключ: объём/мес}.
    by_idea (§7) — {ключ: KeywordIdea}; если задан, к строке ключа добавляем конкуренцию/ставку/
    сезон (то, что раньше жило ТОЛЬКО в .xlsx). irrelevant — сколько идей помечено нерелевантными
    (§19.4.2 AI-релевантность). off_topic — сами эти ключи: помечаем их 🚫 в топе и перечисляем
    (не просто счётчик — менеджеру важно ВИДЕТЬ, что сочтено нецелевым). Ключи из таблицы НЕ
    удаляются (контракт «менеджер видит всё»). Усечение помечаем явно, без «тихого» обрезания."""
    # Плоский топ (15) — главная витрина «best→worst»; кластеры компактнее, полное — в .xlsx.
    # Границы держим так, чтобы сводка влезала в лимит одного сообщения Telegram (~4096 символов).
    max_clusters, max_kw, flat_top_n = 5, 4, 15
    by_idea = by_idea or {}
    # P1-7: плоский топ best→worst по объёму — единый отсортированный список поверх кластеров
    # (пользователю нужно «сразу видно, что лучше»). Полный список — во вложении .xlsx.
    all_kw = {k for cl in clusters for k in cl.keywords}
    flat_top = sorted(all_kw, key=lambda k: by_text.get(k, 0), reverse=True)[:flat_top_n]
    if _lang(lang) == "en":
        lines = [
            f"🔍 <b>Keywords</b> — {esc(src)}",
            f"Ideas: <b>{total}</b>, clusters: {len(clusters)}\n",
        ]
        if flat_top:
            lines.append("🏆 <b>Top by volume</b> (best → worst):")
            for kw in flat_top:
                suffix = _kw_metrics_suffix(by_idea.get(kw), currency, True)
                mark = "🚫 " if kw in off_topic else ""
                lines.append(f"  • {mark}{esc(kw)} — {_thou(by_text.get(kw, 0))}/mo{suffix}")
            lines.append("")
        for cl in clusters[:max_clusters]:
            intent = f" · <i>{esc(cl.intent)}</i>" if cl.intent else ""
            lines.append(f"<b>{esc(cl.name)}</b>{intent} ({len(cl.keywords)})")
            ordered = sorted(cl.keywords, key=lambda k: by_text.get(k, 0), reverse=True)
            for kw in ordered[:max_kw]:
                suffix = _kw_metrics_suffix(by_idea.get(kw), currency, True)
                lines.append(f"  • {esc(kw)} — {_thou(by_text.get(kw, 0))}/mo{suffix}")
            if len(cl.keywords) > max_kw:
                lines.append(f"  …{len(cl.keywords) - max_kw} more — see .xlsx")
        if len(clusters) > max_clusters:
            lines.append(f"\n…{len(clusters) - max_clusters} more clusters — see .xlsx")
        off = list(off_topic)[:8]
        if off:
            more = f" +{irrelevant - len(off)}" if irrelevant > len(off) else ""
            lines.append(
                "\n🚫 <b>Likely off-topic vs the client's business</b>"
                f"{more}: {', '.join(esc(t) for t in off)}. Review before adding."
            )
        elif irrelevant > 0:
            lines.append(f"\n🚫 {irrelevant} idea(s) flagged likely off-topic.")
        lines.append(
            "\n<i>This is a suggestion, not an action. Full table is in the attachment.</i>"
        )
        return "\n".join(lines)
    lines = [
        f"🔍 <b>Ключевые слова</b> — {esc(src)}",
        f"Идей: <b>{total}</b>, кластеров: {len(clusters)}\n",
    ]
    if flat_top:
        lines.append("🏆 <b>Топ по объёму</b> (лучшие → слабее):")
        for kw in flat_top:
            suffix = _kw_metrics_suffix(by_idea.get(kw), currency, False)
            mark = "🚫 " if kw in off_topic else ""
            lines.append(f"  • {mark}{esc(kw)} — {_thou(by_text.get(kw, 0))}/мес{suffix}")
        lines.append("")
    for cl in clusters[:max_clusters]:
        intent = f" · <i>{esc(cl.intent)}</i>" if cl.intent else ""
        lines.append(f"<b>{esc(cl.name)}</b>{intent} ({len(cl.keywords)})")
        ordered = sorted(cl.keywords, key=lambda k: by_text.get(k, 0), reverse=True)
        for kw in ordered[:max_kw]:
            suffix = _kw_metrics_suffix(by_idea.get(kw), currency, False)
            lines.append(f"  • {esc(kw)} — {_thou(by_text.get(kw, 0))}/мес{suffix}")
        if len(cl.keywords) > max_kw:
            lines.append(f"  …ещё {len(cl.keywords) - max_kw} — см. .xlsx")
    if len(clusters) > max_clusters:
        lines.append(f"\n…ещё {len(clusters) - max_clusters} кластеров — см. .xlsx")
    off = list(off_topic)[:8]
    if off:
        more = f" +{irrelevant - len(off)}" if irrelevant > len(off) else ""
        lines.append(
            "\n🚫 <b>Низкая релевантность бизнесу клиента</b>"
            f"{more}: {', '.join(esc(t) for t in off)}. Проверьте перед добавлением."
        )
    elif irrelevant > 0:
        lines.append(f"\n🚫 {irrelevant} идей помечены нецелевыми.")
    lines.append("\n<i>Это подсказка, не действие. Полная таблица — во вложении.</i>")
    return "\n".join(lines)


def fmt_searchterms(items: list[dict], *, currency: str = "", lang: str | None = None) -> str:
    """§7: список «мусорных» поисковых запросов (клики без конверсий) с расходом и кампанией.
    Кнопки под сообщением предлагают добавить запрос в минус-слова (за confirm-гейтом). off_topic
    (§19.4.2, advisory) — тег «похоже не по теме бизнеса» по AI-релевантности к профилю клиента."""
    cur = f" {currency}" if currency else ""
    if _lang(lang) == "en":
        lines = ["🔎 <b>Wasteful search terms</b> (clicks, no conversions):", ""]
        for it in items:
            tag = " 🚫off-topic" if it.get("off_topic") else ""
            lines.append(
                f"• <b>{esc(it['term'])}</b>{tag} — {it['clicks']} clicks, "
                f"{round(float(it['cost']), 2)}{cur} · {esc(it['campaign'])}"
            )
        lines.append("\n<i>Tap 🚫 to add a term to negative keywords (confirmation required).</i>")
        return "\n".join(lines)
    lines = ["🔎 <b>«Мусорные» поисковые запросы</b> (клики без конверсий):", ""]
    for it in items:
        tag = " 🚫не по теме" if it.get("off_topic") else ""
        lines.append(
            f"• <b>{esc(it['term'])}</b>{tag} — {it['clicks']} кл., "
            f"{round(float(it['cost']), 2)}{cur} · {esc(it['campaign'])}"
        )
    lines.append("\n<i>Нажми 🚫, чтобы добавить запрос в минус-слова (нужно подтверждение).</i>")
    return "\n".join(lines)


# ── Рендер с данными ─────────────────────────────────────────────────────────────
def fmt_stats(
    account: str,
    days: int,
    st: dict,
    currency: str = "",
    lang: str | None = None,
    *,
    name: str = "",
    period_label: str = "",
) -> str:
    """Статистика аккаунта с вычисленными в КОДЕ CTR/CPC (контракт read не трогаем).
    currency (§9) — код валюты аккаунта для денежных строк; пустой → без явной валюты.
    name (2.1) — имя аккаунта из meta обхода MCC: заголовок «Башня · …2039» как в пикере;
    пустое → прежняя маска «…{last4}» (kw-only, старые вызовы не ломаются)."""
    imp = int(st.get("impressions") or 0)
    clk = int(st.get("clicks") or 0)
    cost = float(st.get("cost") or 0)
    conv = float(st.get("conversions") or 0)
    cval = float(st.get("conv_value") or 0)
    ctr = (clk / imp * 100) if imp else 0.0
    cpc = (cost / clk) if clk else 0.0
    cur = f" {esc(currency)}" if currency else ""
    label = f"{esc(name)} · …{esc(str(account)[-4:])}" if name else f"…{esc(str(account)[-4:])}"
    # C5: явный диапазон дат («за вчера»/«с 1 по 15 июня») — подпись фактическим периодом.
    if _lang(lang) == "en":
        period = esc(period_label) if period_label else f"{days} d."
        return (
            f"📊 <b>Account {label}</b> · {period}\n\n"
            f"Impressions: <b>{_thou(imp)}</b>\n"
            f"Clicks:      <b>{_thou(clk)}</b>  (CTR {ctr:.2f}%)\n"
            f"Cost:        <b>{_thou(cost, 2)}{cur}</b>\n"
            f"Avg. CPC:    <b>{_thou(cpc, 2)}{cur}</b>\n"
            f"Conversions: <b>{conv:g}</b>\n"
            f"Value:       <b>{_thou(cval, 2)}{cur}</b>"
        )
    period = esc(period_label) if period_label else f"{days} дн."
    return (
        f"📊 <b>Аккаунт {label}</b> · {period}\n\n"
        f"Показы:      <b>{_thou(imp)}</b>\n"
        f"Клики:       <b>{_thou(clk)}</b>  (CTR {ctr:.2f}%)\n"
        f"Расход:      <b>{_thou(cost, 2)}{cur}</b>\n"
        f"Ср. CPC:     <b>{_thou(cpc, 2)}{cur}</b>\n"
        f"Конверсии:   <b>{conv:g}</b>\n"
        f"Ценность:    <b>{_thou(cval, 2)}{cur}</b>"
    )


def fmt_mutready(r: dict, lang: str | None = None) -> str:
    """2.5: чек-лист готовности аккаунта к ВКЛЮЧЕНИЮ МУТАЦИЙ (/mutready, только админ). Диагностика,
    НИЧЕГО не меняет: последний шаг (добавить в GOOGLE_ADS_ALLOWED_CUSTOMER_IDS) делает ВЛАДЕЛЕЦ
    руками в конфиге (golden rule 9 — бот конфиг не трогает). r — dict из _mutready_check."""
    en = _lang(lang) == "en"

    def _mark(ok: bool | None) -> str:
        return "✅" if ok else ("⚠️" if ok is None else "❌")

    cid = esc(str(r.get("cid", "")))
    name = esc(str(r.get("name", "") or ""))
    title = f"{name} · {cid}" if name else cid
    ops = r.get("operators") or []
    ops_s = ", ".join(str(o) for o in ops[:10]) or ("none" if en else "нет")
    probe_err = esc(str(r.get("probe_error", "") or ""))
    if en:
        lines = [
            f"🧰 <b>Mutation readiness: {title}</b>",
            f"{_mark(r.get('visible'))} visible to the bot (allowed ceiling)",
            f"{_mark(r.get('enabled'))} status: {esc(str(r.get('status', '') or '?'))}",
            f"{_mark(r.get('oauth'))} OAuth: "
            + (
                "per-account token loaded"
                if r.get("oauth_runtime")
                else ("covered by env MCC token" if r.get("oauth") else "no credentials")
            ),
            f"{_mark(r.get('probe'))} live read probe"
            + (f" — {probe_err}" if probe_err and not r.get("probe") else ""),
            f"{_mark(bool(ops) or None)} operator grants: {ops_s}",
            f"{_mark(r.get('twofa'))} 2FA: "
            + ("ready" if r.get("twofa") else "off/not ready (recommended before enabling)"),
        ]
        if r.get("mutations_enabled"):
            how = "all visible" if r.get("all_visible") else "explicit list"
            lines.append(
                f"✅ mutations enabled ({how}) — still gated by confirmation on every change"
            )
        else:
            lines.append(
                "⏭ final step (OWNER, by hand): set GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all (all "
                "visible; prod default) or an explicit id list (docs/DEPLOYMENT.md §2.1). "
                "The bot never changes this config itself."
            )
        return "\n".join(lines)
    lines = [
        f"🧰 <b>Готовность к мутациям: {title}</b>",
        f"{_mark(r.get('visible'))} видим боту (потолок allowed_ceiling)",
        f"{_mark(r.get('enabled'))} статус: {esc(str(r.get('status', '') or '?'))}",
        f"{_mark(r.get('oauth'))} OAuth: "
        + (
            "per-account токен загружен"
            if r.get("oauth_runtime")
            else ("покрыт env-токеном MCC" if r.get("oauth") else "кредов нет")
        ),
        f"{_mark(r.get('probe'))} живое чтение (probe)"
        + (f" — {probe_err}" if probe_err and not r.get("probe") else ""),
        f"{_mark(bool(ops) or None)} гранты операторам: {ops_s}",
        f"{_mark(r.get('twofa'))} 2FA: "
        + ("готова" if r.get("twofa") else "выкл/не готова (рекомендуется включить до мутаций)"),
    ]
    if r.get("mutations_enabled"):
        how = "все видимые" if r.get("all_visible") else "явный список"
        lines.append(
            f"✅ мутации включены ({how}) — каждое изменение всё равно за подтверждением «да»"
        )
    else:
        lines.append(
            "⏭ финальный шаг (ВЛАДЕЛЕЦ, руками): GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all (все видимые; "
            "прод-дефолт) или явный список id (docs/DEPLOYMENT.md §2.1). Бот этот конфиг сам НЕ меняет."
        )
    return "\n".join(lines)


def fmt_mutready_all(results: list[dict], lang: str | None = None) -> str:
    """AD.5: компактная сводка готовности по ВСЕМ видимым аккаунтам (/mutready all). Одна строка на
    аккаунт: маркер общей готовности + флаги visible/oauth/probe/2FA + вкл/выкл мутаций. Диагностика,
    ничего не меняет (см. fmt_mutready)."""
    en = _lang(lang) == "en"

    def _f(ok: bool) -> str:
        return "✅" if ok else "❌"

    header = (
        "🧰 <b>Mutation readiness — all visible accounts</b>"
        if en
        else ("🧰 <b>Готовность к мутациям — все видимые аккаунты</b>")
    )
    lines = [header]
    for r in results:
        cid = esc(str(r.get("cid", "")))
        name = esc(str(r.get("name", "") or ""))
        title = f"{name} · {cid}" if name else cid
        ready = bool(r.get("visible") and r.get("oauth") and r.get("probe"))
        mut = r.get("mutations_enabled")
        if en:
            flags = f"vis{_f(bool(r.get('visible')))} oauth{_f(bool(r.get('oauth')))} probe{_f(bool(r.get('probe')))} 2FA{_f(bool(r.get('twofa')))}"
            mut_s = "mut: ON" if mut else "mut: off"
        else:
            flags = f"вид{_f(bool(r.get('visible')))} oauth{_f(bool(r.get('oauth')))} probe{_f(bool(r.get('probe')))} 2FA{_f(bool(r.get('twofa')))}"
            mut_s = "мут: ВКЛ" if mut else "мут: выкл"
        lines.append(f"{'✅' if ready else '⚠️'} <b>{title}</b> — {flags} · {mut_s}")
    tail = (
        "Enable all: GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all (prod default). The bot never changes this "
        "config. Every mutation still needs confirmation."
        if en
        else "Включить все: GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all (прод-дефолт). Бот конфиг НЕ меняет. "
        "Любая мутация — только через подтверждение «да»."
    )
    lines.append("")
    lines.append(tail)
    return "\n".join(lines)


def _usd_live(n: float | None) -> str:
    """Деньги для /balance с АДАПТИВНОЙ точностью. Зачем: запрос к LLM стоит доли цента
    ($~0.000003 за парс), и при фиксированных 2 знаках трата $0.0805 «зависает» как $0.08 на
    сотни вызовов — выглядит, будто данные не обновляются. Тут знаков ровно столько, чтобы
    микро-движение было видно: ≥$1 → 2 знака; $0.01–$1 → 4; меньше (но >0) → 6; ноль → $0.00."""
    if n is None:
        return "—"
    a = abs(n)
    if a == 0:
        dec = 2
    elif a >= 1:
        dec = 2
    elif a >= 0.01:
        dec = 4
    else:
        dec = 6
    return f"${_thou(n, dec)}"


def fmt_balance(acct, snap: dict, lang: str | None = None) -> str:
    """Бюджет LLM: баланс/траты OpenRouter (источник истины, переживает рестарты) + живая
    разбивка ТЕКУЩЕГО процесса по ролям «с запуска». acct — openrouter_account.AccountStatus;
    snap — core.usage.snapshot(). Кредит OpenRouter = USD, поэтому показываем в $."""
    if _lang(lang) == "en":
        return _fmt_balance_en(acct, snap)
    L = ["💳 <b>Бюджет ИИ · OpenRouter</b> <i>(актуально сейчас)</i>", ""]

    bal = acct.balance
    if bal is not None:
        L.append(f"💰 Остаток на счёте: <b>{_usd_live(bal)}</b>")
    if acct.total_usage is not None:
        L.append(f"Потрачено всего: <b>{_usd_live(acct.total_usage)}</b> (по аккаунту)")
    if bal is None and acct.key_usage is not None:
        # /credits недоступен (нужен management-ключ) — показываем траты по самому ключу.
        L.append(f"Потрачено ключом: <b>{_usd_live(acct.key_usage)}</b>")
    # Живые срезы по периодам (/key): двигаются с каждым запросом — «актуально», как просили.
    periods = [
        (acct.usage_daily, "сегодня"),
        (acct.usage_weekly, "неделя"),
        (acct.usage_monthly, "месяц"),
    ]
    chips = [f"{label} {_usd_live(v)}" for v, label in periods if v is not None]
    if chips:
        L.append("Траты: " + " · ".join(chips))
    if acct.limit_remaining is not None:
        L.append(f"Лимит ключа: <b>{_usd_live(acct.limit_remaining)}</b> остаток")
    if acct.is_free_tier:
        L.append("Тариф: <b>free</b> (есть лимиты по числу запросов)")
    if len(L) == 2:  # ни одного поля не пришло
        L.append("<i>Данные счёта недоступны (проверь OPENROUTER_API_KEY).</i>")

    total = snap.get("total")
    roles = snap.get("roles", {})
    if total is not None and total.calls:
        L += ["", f"<b>С запуска бота</b> · {total.calls} запрос(ов) к модели"]
        L.append(
            f"Токены: {_thou(total.prompt_tokens)} вход / {_thou(total.completion_tokens)} выход"
        )
        if total.cached_tokens and total.prompt_tokens:
            pct = total.cached_tokens / total.prompt_tokens * 100
            L.append(
                f"Из кэша: {_thou(total.cached_tokens)} вход. токенов ({pct:.0f}%) — дешевле ✅"
            )
        L.append(f"Стоимость: <b>{_usd_live(total.cost)}</b>")
        names = {"parsing": "парсинг", "copy": "копирайт", "fallback": "резерв"}
        for role, u in roles.items():
            if u.calls:
                L.append(f"  • {esc(names.get(role, role))}: {u.calls}× · {_usd_live(u.cost)}")
    else:
        L += ["", "<i>С запуска вызовов модели ещё не было.</i>"]

    return "\n".join(L)


def _fmt_balance_en(acct, snap: dict) -> str:
    """EN-ветка fmt_balance — зеркало RU по структуре/полям, английская формулировка."""
    L = ["💳 <b>AI budget · OpenRouter</b> <i>(live now)</i>", ""]

    bal = acct.balance
    if bal is not None:
        L.append(f"💰 Account balance: <b>{_usd_live(bal)}</b>")
    if acct.total_usage is not None:
        L.append(f"Spent total: <b>{_usd_live(acct.total_usage)}</b> (account-wide)")
    if bal is None and acct.key_usage is not None:
        L.append(f"Spent by key: <b>{_usd_live(acct.key_usage)}</b>")
    periods = [
        (acct.usage_daily, "today"),
        (acct.usage_weekly, "week"),
        (acct.usage_monthly, "month"),
    ]
    chips = [f"{label} {_usd_live(v)}" for v, label in periods if v is not None]
    if chips:
        L.append("Spend: " + " · ".join(chips))
    if acct.limit_remaining is not None:
        L.append(f"Key limit: <b>{_usd_live(acct.limit_remaining)}</b> remaining")
    if acct.is_free_tier:
        L.append("Tier: <b>free</b> (request-count limits apply)")
    if len(L) == 2:  # ни одного поля не пришло
        L.append("<i>Account data unavailable (check OPENROUTER_API_KEY).</i>")

    total = snap.get("total")
    roles = snap.get("roles", {})
    if total is not None and total.calls:
        L += ["", f"<b>Since bot start</b> · {total.calls} model request(s)"]
        L.append(f"Tokens: {_thou(total.prompt_tokens)} in / {_thou(total.completion_tokens)} out")
        if total.cached_tokens and total.prompt_tokens:
            pct = total.cached_tokens / total.prompt_tokens * 100
            L.append(
                f"From cache: {_thou(total.cached_tokens)} input tokens ({pct:.0f}%) — cheaper ✅"
            )
        L.append(f"Cost: <b>{_usd_live(total.cost)}</b>")
        names = {"parsing": "parsing", "copy": "copywriting", "fallback": "fallback"}
        for role, u in roles.items():
            if u.calls:
                L.append(f"  • {esc(names.get(role, role))}: {u.calls}× · {_usd_live(u.cost)}")
    else:
        L += ["", "<i>No model calls since start.</i>"]

    return "\n".join(L)


# ── Журнал изменений (ТЗ §12/§18: audit-лог всех операций — кто/когда/что/результат) ─
_AUDIT_STATUS = {
    "applied": ("✅", "применено"),
    "failed": ("⚠️", "ошибка"),
    "rejected": ("❌", "отклонено"),
    "needs_review": ("⚠️", "требует проверки"),
}
# Человекочитаемые имена операций для журнала (как в keyboards/loop, без технических slug'ов).
_OP_HUMAN = {
    "update_budget": "бюджет",
    "update_bid": "ставка CPC",
    "add_keywords": "добавить ключи",
    "remove_keywords": "удалить ключи",
    "add_negative_keywords": "минус-слова",
    "pause_campaign": "пауза кампании",
    "resume_campaign": "возобновить кампанию",
    "launch_campaign": "запустить кампанию",
    "set_geo_proximity": "гео-радиус",
    "set_geo_location": "гео-локации",
    "set_bidding_strategy": "стратегия ставок",
    "attach_audience": "аудитории",
    "create_rsa": "создать RSA",
    "create_gdn_campaign": "создать GDN-кампанию",
    "create_search_campaign": "создать Search-кампанию",
}
_AUDIT_STATUS_EN = {
    "applied": ("✅", "applied"),
    "failed": ("⚠️", "failed"),
    "rejected": ("❌", "rejected"),
    "needs_review": ("⚠️", "needs review"),
}
_OP_HUMAN_EN = {
    "update_budget": "budget",
    "update_bid": "CPC bid",
    "add_keywords": "add keywords",
    "remove_keywords": "remove keywords",
    "add_negative_keywords": "negative keywords",
    "pause_campaign": "pause campaign",
    "resume_campaign": "resume campaign",
    "launch_campaign": "launch campaign",
    "set_geo_proximity": "geo-radius",
    "set_geo_location": "geo-locations",
    "set_bidding_strategy": "bidding strategy",
    "attach_audience": "audiences",
    "create_rsa": "create RSA",
    "create_gdn_campaign": "create GDN campaign",
    "create_search_campaign": "create Search campaign",
}


def fmt_alerts(cur: dict, lang: str | None = None) -> str:
    """3H (M10): текущие пороги аномалий планировщика (эффективные: дефолты ∪ per-chat)."""
    en = _lang(lang) == "en"
    if en:
        return (
            "🔔 <b>Anomaly alert thresholds</b> (weekly windows, scheduler)\n"
            f"📈 Spend spike: <b>+{cur.get('spend_spike_pct', 0):.0f}%</b>\n"
            f"📉 Conversions drop: <b>−{cur.get('conv_drop_pct', 0):.0f}%</b>\n"
            f"💸 Min spend (noise floor): <b>{cur.get('min_spend', 0):g}</b>\n\n"
            "<i>Alerts only signal — the bot never changes anything by itself.</i>"
        )
    return (
        "🔔 <b>Пороги алертов аномалий</b> (недельные окна, планировщик)\n"
        f"📈 Всплеск расхода: <b>+{cur.get('spend_spike_pct', 0):.0f}%</b>\n"
        f"📉 Падение конверсий: <b>−{cur.get('conv_drop_pct', 0):.0f}%</b>\n"
        f"💸 Мин. расход (отсечка шума): <b>{cur.get('min_spend', 0):g}</b>\n\n"
        "<i>Алерты — только сигнал: бот сам ничего не меняет.</i>"
    )


# 3C: подписи частей composite-результата для warnings частичного успеха.
_RESULT_PART = {
    "keywords": ("ключи", "keywords"),
    "geo": ("гео-таргетинг", "geo targeting"),
    "languages": ("языки", "languages"),
    "ad_schedule": ("расписание показов", "ad schedule"),
}
# Служебные/технические ключи result — в человекочитаемом фолбэке не показываем.
_RESULT_HIDDEN_KEYS = frozenset(
    {"applied", "customer_id", "campaign", "budget", "ad_group", "ad", "resource_names", "created"}
)


def fmt_mutation_result(operation: str, result: object, lang: str | None = None) -> str:
    """3C: человекочитаемый итог применённой операции для _do_confirm — вместо сырого Python-dict
    (`{'campaign': 'customers/…', 'geo': 0}` менеджеру ни о чём). Возвращает ГОТОВЫЙ HTML
    (экранирование внутри). Warnings частичного успеха composite-create показываются явно."""
    en = _lang(lang) == "en"
    if result is None:
        return ""
    if isinstance(result, str):
        return esc(result)
    if not isinstance(result, dict):
        return esc(str(result))
    L: list[str] = []
    if operation in (
        "create_search_campaign",
        "create_gdn_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
    ):
        name = str(result.get("campaign_name") or "")
        head = (
            f"🆕 Campaign “{esc(name)}” created (PAUSED, $0 until launch)."
            if en
            else f"🆕 Кампания «{esc(name)}» создана (PAUSED, $0 до запуска)."
        )
        L.append(head)
        stats: list[str] = []
        for key, (ru_l, en_l) in (
            ("headlines", ("заголовков", "headlines")),
            ("descriptions", ("описаний", "descriptions")),
            ("keywords", ("ключей", "keywords")),
            ("geo", ("гео", "geo")),
            ("languages", ("языков", "languages")),
        ):
            v = result.get(key)
            if isinstance(v, int) and v > 0:
                stats.append(f"{en_l if en else ru_l}: {v}")
        if stats:
            L.append("• " + " · ".join(stats))
        # §19.6/§19.7: изображения и ассеты — раньше считались, но НИГДЕ не показывались (тихая
        # потеря на неподходящем аккаунте). Теперь видно: прикреплено/из запрошенных, добавлено,
        # переиспользовано, ПРОПУЩЕНО (с причиной «нужна доп. настройка»).
        img_req = result.get("images_requested")
        img_add = result.get("images_added")
        if isinstance(img_req, int) and img_req > 0 and isinstance(img_add, int):
            if img_add < img_req:
                L.append(
                    f"⚠️ Images: {img_add}/{img_req} attached — the account may not support image assets."
                    if en
                    else f"⚠️ Изображения: {img_add}/{img_req} прикреплено — аккаунт может не поддерживать image-ассеты."
                )
            else:
                L.append(
                    f"🖼 Images attached: {img_add}"
                    if en
                    else f"🖼 Изображений прикреплено: {img_add}"
                )
        added = result.get("assets_added")
        if isinstance(added, list) and added:
            fams = esc(", ".join(map(str, added)))
            L.append(f"➕ Assets added: {fams}" if en else f"➕ Ассеты добавлены: {fams}")
        reused = result.get("assets_reused")
        if isinstance(reused, int) and reused > 0:
            L.append(
                f"♻️ Existing assets linked: {reused}"
                if en
                else f"♻️ Переиспользовано ассетов: {reused}"
            )
        skipped = result.get("assets_skipped")
        if isinstance(skipped, list) and skipped:
            fams = esc(
                ", ".join(str(s.get("family") if isinstance(s, dict) else s) for s in skipped)
            )
            L.append(
                f"⚠️ Assets skipped: {fams} (require extra setup)."
                if en
                else f"⚠️ Ассеты пропущены: {fams} (нужна доп. настройка)."
            )
    elif operation in (
        "add_keywords",
        "remove_keywords",
        "add_negative_keywords",
        "remove_negative_keywords",
    ):
        n = int(result.get("count") or 0)
        mt = str(result.get("match_type") or "")
        verb = {
            "add_keywords": ("Добавлено ключей", "Keywords added"),
            "remove_keywords": ("Удалено ключей", "Keywords removed"),
            "add_negative_keywords": ("Добавлено минус-слов", "Negative keywords added"),
            "remove_negative_keywords": ("Снято минус-слов", "Negative keywords removed"),
        }[operation]
        L.append(f"{verb[1] if en else verb[0]}: <b>{n}</b>" + (f" ({esc(mt)})" if mt else ""))
        nf = result.get("not_found")
        if isinstance(nf, list) and nf:
            label = "not found" if en else "не найдено"
            L.append(f"• {label}: {esc(', '.join(map(str, nf[:5])))}")
    elif operation in ("profile_save", "profile_update", "profile_clear"):
        if result.get("cleared"):
            L.append("🗑 Profile cleared." if en else "🗑 Профиль очищен.")
        else:
            fields = result.get("changed_fields") or []
            verb = (
                ("Profile created" if result.get("created") else "Profile updated")
                if en
                else ("Профиль создан" if result.get("created") else "Профиль обновлён")
            )
            L.append(
                f"💾 {verb}" + (f": {esc(', '.join(map(str, fields[:8])))}" if fields else ".")
            )
    elif operation == "remove_campaign":
        L.append("🗑 Campaign deleted." if en else "🗑 Кампания удалена.")
    elif operation == "remove_ad_group":
        L.append("🗑 Ad group deleted." if en else "🗑 Группа объявлений удалена.")
    else:
        # фолбэк: построчно «ключ: значение» без служебных/сырых resource_name (не repr-дамп)
        for k, v in result.items():
            if k in _RESULT_HIDDEN_KEYS or k in ("warnings", "bidding_note", "rejected"):
                continue
            if isinstance(v, (str, int, float, bool)):
                L.append(f"• {esc(str(k))}: {esc(str(v))}")
        if not L:
            L.append("✅ OK")
    # warnings частичного успеха (composite-create): «⚠️ гео не применено (0 из 2)»
    for w in result.get("warnings") or []:
        part = str(w.get("part") or "")
        ru_l, en_l = _RESULT_PART.get(part, (part, part))
        req, app = int(w.get("requested") or 0), int(w.get("applied") or 0)
        if en:
            msg = (
                f"⚠️ {en_l}: applied {app} of {req}" if app else f"⚠️ {en_l} NOT applied (0 of {req})"
            )
        else:
            msg = (
                f"⚠️ {ru_l}: применено {app} из {req}"
                if app
                else f"⚠️ {ru_l} НЕ применено (0 из {req})"
            )
        L.append(msg)
    return "\n".join(L)


def _journal_actor(actor_user_id: int | None, actor_username: str | None) -> str:
    """«Кто» для строки журнала: @username, иначе id, иначе «—» (системное/неизвестно)."""
    if actor_username:
        return f"@{esc(actor_username)}"
    if actor_user_id:
        return f"id{actor_user_id}"
    return "—"


def fmt_errors(rows, lang: str | None = None) -> str:
    """§15: последние перехваченные ошибки (error_events) для /diag. rows — duck-typed
    (.request_id/.where/.exc_type/.message/.created_at), reverse-chron. message/traceback уже
    редактированы (секретов нет) — безопасно показывать. Полный traceback в чат НЕ выводим."""
    en = _lang(lang) == "en"
    if not rows:
        return "🩺 No errors logged." if en else "🩺 Журнал ошибок пуст."
    head = "🩺 <b>Recent errors</b>" if en else "🩺 <b>Последние ошибки</b>"
    L = [head, ""]
    for e in rows:
        when = e.created_at.strftime("%d.%m %H:%M") if getattr(e, "created_at", None) else "—"
        msg = (getattr(e, "message", "") or "").strip().replace("\n", " ")
        if len(msg) > 120:
            msg = msg[:120] + "…"
        L.append(
            f"<code>{esc(e.request_id)}</code> · {esc(e.where)} · "
            f"<b>{esc(e.exc_type)}</b> · {when} UTC"
        )
        if msg:
            L.append(f"    ↳ {esc(msg)}")
    return "\n".join(L)


def fmt_error_alert(rows, lang: str | None = None) -> str:
    """A1 (§15): проактивный дайджест НОВЫХ инцидентов админу (scheduler.jobs.run_error_alerts).
    rows — новые ErrorEvent (duck-typed, как fmt_errors), уже редактированы (секретов нет). Дедупим
    по (exc_type, where) с подсчётом ×N — один компактный месседж вместо спама по строке на ошибку.
    Полный traceback в чат НЕ шлём (он в /diag detail). Подробности — командой /diag."""
    en = _lang(lang) == "en"
    n = len(rows)
    head = (
        f"🚨 <b>New incidents: {n}</b> — details in /diag"
        if en
        else f"🚨 <b>Новых инцидентов: {n}</b> — подробнее в /diag"
    )
    L = [head, ""]
    # Дедуп по (exc_type, where): сохраняем первое вхождение для показа, считаем повторы.
    seen: dict[tuple[str, str], object] = {}
    order: list[tuple[str, str]] = []
    counts: dict[tuple[str, str], int] = {}
    for e in rows:
        key = (getattr(e, "exc_type", "") or "", getattr(e, "where", "") or "")
        counts[key] = counts.get(key, 0) + 1
        if key not in seen:
            seen[key] = e
            order.append(key)
    for key in order[:15]:
        e = seen[key]
        when = e.created_at.strftime("%d.%m %H:%M") if getattr(e, "created_at", None) else "—"
        c = counts[key]
        cnt = f" ×{c}" if c > 1 else ""
        L.append(
            f"• <code>{esc(e.request_id)}</code> · {esc(e.where)} · "
            f"<b>{esc(e.exc_type)}</b>{cnt} · {when} UTC"
        )
    if len(order) > 15:
        L.append("…")
    return "\n".join(L)


def fmt_error_detail(row, lang: str | None = None) -> str:
    """A3 (§15): полная карточка одного инцидента для detail-кнопки /diag (админ). traceback/message
    УЖЕ редактированы (golden rule #5) — секретов нет. Усекаем под лимит Telegram (4096) с запасом."""
    en = _lang(lang) == "en"
    when = row.created_at.strftime("%d.%m %H:%M") if getattr(row, "created_at", None) else "—"
    head = (
        "🔍 <b>Incident</b> " if en else "🔍 <b>Инцидент</b> "
    ) + f"<code>{esc(row.request_id)}</code>"
    meta = f"{esc(row.where)} · <b>{esc(row.exc_type)}</b> · {when} UTC"
    tb = (getattr(row, "traceback", "") or getattr(row, "message", "") or "").strip()
    if len(tb) > 3500:  # Telegram лимит 4096 — оставляем запас на разметку/эскейп
        tb = tb[:3500] + ("\n…(truncated)" if en else "\n…(усечено)")
    if tb:
        body = f"<pre>{esc(tb)}</pre>"
    else:
        body = "<i>no traceback</i>" if en else "<i>трейсбека нет</i>"
    return f"{head}\n{meta}\n\n{body}"


_BUG_STATUS_EMOJI = {"new": "🆕", "triaged": "🛠", "closed": "🗄"}


def fmt_bug_list(rows, lang: str | None = None) -> str:
    """§6: список баг-репортов для админ-триажа (/bugs). rows — BugReport (duck-typed), reverse-chron.
    text уже РЕДАКТИРОВАН на записи (секретов нет). Показываем id/статус/автора/время + начало текста."""
    en = _lang(lang) == "en"
    if not rows:
        return "🐞 No bug reports." if en else "🐞 Баг-репортов нет."
    head = "🐞 <b>Bug reports</b>" if en else "🐞 <b>Баг-репорты</b>"
    L = [head, ""]
    for r in rows:
        when = r.created_at.strftime("%d.%m %H:%M") if getattr(r, "created_at", None) else "—"
        st = _BUG_STATUS_EMOJI.get(getattr(r, "status", "") or "", "•")
        who = f"@{esc(r.username)}" if getattr(r, "username", None) else f"chat {r.chat_id}"
        msg = (getattr(r, "text", "") or "").strip().replace("\n", " ")
        if len(msg) > 140:
            msg = msg[:140] + "…"
        L.append(f"{st} <b>#{r.id}</b> · {who} · {when} UTC")
        if msg:
            L.append(f"    ↳ {esc(msg)}")
    return "\n".join(L)


def fmt_bug_forward(
    *, bug_id: int, chat_id: int, username, ticket: str, text: str, lang: str | None = None
) -> str:
    """§6: карточка баг-репорта для немедленного форварда админам. text уже РЕДАКТИРОВАН вызывающим."""
    en = _lang(lang) == "en"
    who = f"@{esc(username)}" if username else f"chat {chat_id}"
    head = f"🐞 <b>New bug report</b> #{bug_id}" if en else f"🐞 <b>Новый баг-репорт</b> #{bug_id}"
    meta = (f"from {who}" if en else f"от {who}") + f" · <code>{esc(ticket)}</code>"
    tip = "Triage: /bugs" if en else "Триаж: /bugs"
    return f"{head}\n{meta}\n\n{esc(text)}\n\n<i>{tip}</i>"


def fmt_weekly_digest(
    errors, bug_rows, activity: dict, *, days: int = 7, lang: str | None = None
) -> str:
    """§6/§15 (1.3): еженедельный дайджест админам. errors — ErrorEvent за неделю (дедуп по
    (exc_type, where) со счётчиками); bug_rows — BugReport за неделю; activity —
    confirm.store.audit_activity_since (статусы + created_campaigns). Всё уже редактировано/без
    секретов. Короткий текст (детали — во вложении файла)."""
    en = _lang(lang) == "en"
    head = (
        f"🗓 <b>Weekly digest</b> · last {days} days"
        if en
        else f"🗓 <b>Еженедельный дайджест</b> · за {days} дн."
    )
    L = [head, ""]

    # Активность
    st = (activity or {}).get("statuses", {}) or {}
    created = int((activity or {}).get("created_campaigns", 0) or 0)
    applied = int(st.get("applied", 0))
    failed = int(st.get("failed", 0))
    rejected = int(st.get("rejected", 0))
    if en:
        L.append(f"⚙️ <b>Activity</b>: {applied} applied · {failed} failed · {rejected} rejected")
        L.append(f"    campaigns created: {created}")
    else:
        L.append(
            f"⚙️ <b>Активность</b>: {applied} применено · {failed} сбоев · {rejected} отклонено"
        )
        L.append(f"    создано кампаний: {created}")

    # Ошибки (дедуп по (exc_type, where))
    errs = list(errors or [])
    L.append("")
    if not errs:
        L.append("🩺 <b>Errors</b>: none 🎉" if en else "🩺 <b>Ошибки</b>: нет 🎉")
    else:
        counts: dict[tuple[str, str], int] = {}
        order: list[tuple[str, str]] = []
        for e in errs:
            key = (getattr(e, "exc_type", "") or "", getattr(e, "where", "") or "")
            counts[key] = counts.get(key, 0) + 1
            if key not in order:
                order.append(key)
        L.append(
            (f"🩺 <b>Errors</b>: {len(errs)} total, {len(order)} kinds")
            if en
            else (f"🩺 <b>Ошибки</b>: всего {len(errs)}, видов {len(order)}")
        )
        for key in sorted(order, key=lambda k: counts[k], reverse=True)[:8]:
            exc_type, where = key
            L.append(f"    • <b>{esc(exc_type)}</b> · {esc(where)} ×{counts[key]}")

    # Баг-репорты
    bugs = list(bug_rows or [])
    L.append("")
    if not bugs:
        L.append("🐞 <b>Bug reports</b>: none" if en else "🐞 <b>Баг-репорты</b>: нет")
    else:
        new_n = sum(1 for b in bugs if getattr(b, "status", "") == "new")
        L.append(
            (f"🐞 <b>Bug reports</b>: {len(bugs)} ({new_n} new) — see /bugs")
            if en
            else (f"🐞 <b>Баг-репорты</b>: {len(bugs)} ({new_n} новых) — см. /bugs")
        )
    L.append("")
    L.append(
        "<i>Full details in the attached file.</i>"
        if en
        else "<i>Полные детали — в прикреплённом файле.</i>"
    )
    return "\n".join(L)


def fmt_journal(events, lang: str | None = None) -> str:
    """Журнал последних изменений (ТЗ §12): что/когда/кто/результат. events — список
    confirm.store.AuditEvent (applied/failed/rejected), reverse-chron. Время — UTC сервера."""
    en = _lang(lang) == "en"
    statuses = _AUDIT_STATUS_EN if en else _AUDIT_STATUS
    ops = _OP_HUMAN_EN if en else _OP_HUMAN
    if not events:
        if en:
            return (
                "📜 <b>Change journal</b>\n\n"
                "<i>Empty so far — no confirmed changes yet.</i>\n"
                "Every applied/rejected action lands here automatically."
            )
        return (
            "📜 <b>Журнал изменений</b>\n\n"
            "<i>Пока пусто — подтверждённых изменений ещё не было.</i>\n"
            "Каждое применённое/отклонённое действие попадёт сюда автоматически."
        )
    head = (
        f"📜 <b>Change journal</b> · last {len(events)}"
        if en
        else f"📜 <b>Журнал изменений</b> · последние {len(events)}"
    )
    L = [head, ""]
    for e in events:
        emoji, status = statuses.get(e.status, ("•", e.status))
        op = ops.get(e.operation, e.operation)
        when = e.created_at.strftime("%d.%m %H:%M") if e.created_at else "—"
        who = _journal_actor(e.actor_user_id, e.actor_username)
        L.append(f"{emoji} <b>{esc(op)}</b> — {status} · {when} UTC · {who}")
        if e.status in ("failed", "needs_review") and isinstance(e.result, dict):
            err = str(e.result.get("error") or "").strip()
            if err:
                L.append(f"    ↳ {esc(err[:120])}")
    L.append("")
    if en:
        L.append("<i>Full history and “before→after” are stored in the DB (audit_log).</i>")
    else:
        L.append("<i>Полная история и «было→станет» хранятся в БД (audit_log).</i>")
    return "\n".join(L)


def fmt_campaign_targeting(t, lang: str | None = None) -> str:
    """Текущий таргетинг кампании (§3 «чтение … ГЕО», 2E): локации/исключения/радиусы/языки.
    t — ads.read.CampaignTargeting. Пустые списки = «все регионы/языки» (честно показываем)."""
    en = _lang(lang) == "en"
    L: list[str] = ["📍 <b>Current targeting</b>" if en else "📍 <b>Текущий таргетинг</b>"]
    if t.locations:
        label = "Locations" if en else "Локации"
        L.append(f"• {label}: {esc(', '.join(t.locations[:10]))}")
    if t.negative_locations:
        label = "Excluded" if en else "Исключены"
        L.append(f"• {label}: {esc(', '.join(t.negative_locations[:10]))}")
    if t.proximity:
        label = "Radius" if en else "Радиус"
        L.append(f"• {label}: {esc('; '.join(t.proximity[:5]))}")
    if not (t.locations or t.negative_locations or t.proximity):
        L.append("• " + ("all regions (no geo criteria)" if en else "все регионы (гео не задано)"))
    if t.languages:
        label = "Languages" if en else "Языки"
        L.append(f"• {label}: {esc(', '.join(t.languages[:10]))}")
    else:
        L.append("• " + ("all languages" if en else "все языки"))
    return "\n".join(L)


def campaigns_title(account: str, lang: str | None = None, *, name: str = "") -> str:
    """2.1: name — имя аккаунта из meta («Башня»); пустое → прежняя маска «…{last4}»."""
    label = f"{esc(name)} · …{esc(str(account)[-4:])}" if name else f"…{esc(str(account)[-4:])}"
    if _lang(lang) == "en":
        return f"📋 <b>Campaigns of account {label}</b>\nChoose a campaign:"
    return f"📋 <b>Кампании аккаунта {label}</b>\nВыбери кампанию:"


def fmt_campaign_header(c: dict, lang: str | None = None) -> str:
    lng = _lang(lang)
    if lng == "en":
        return (
            f"📋 <b>{esc(c['name'])}</b>\n"
            f"Status: {status_human(c.get('status', ''), lng)}\n\n"
            "Choose an action:"
        )
    return (
        f"📋 <b>{esc(c['name'])}</b>\n"
        f"Статус: {status_human(c.get('status', ''), lng)}\n\n"
        "Выбери действие:"
    )


# ── §19: визард «Создание кампании» ───────────────────────────────────────────────
_BIDDING_HUMAN = {
    "ru": {
        "manual_cpc": "Ручная CPC",
        "maximize_conversions": "Максимум конверсий",
        "maximize_conversion_value": "Максимум ценности конверсий",
        "target_spend": "Максимум кликов (target spend)",
    },
    "en": {
        "manual_cpc": "Manual CPC",
        "maximize_conversions": "Maximize conversions",
        "maximize_conversion_value": "Maximize conversion value",
        "target_spend": "Maximize clicks (target spend)",
    },
}


def cc_account_header(name: str, customer_id: str, lang: str | None = None) -> str:
    """Этап 0: «Аккаунт выбран: …» + просьба описать кампанию (§19.2)."""
    cid = esc(str(customer_id))
    nm = esc(name or cid)
    if _lang(lang) == "en":
        return (
            f"🆕 <b>Account selected:</b> {nm} (<code>{cid}</code>).\n\n"
            "Describe the campaign in one message — what you promote, country/city, budget, goal. "
            "I'll turn it into settings myself."
        )
    return (
        f"🆕 <b>Аккаунт выбран:</b> {nm} (<code>{cid}</code>).\n\n"
        "Опишите рекламную кампанию одним сообщением — что продвигаем, на какую страну/город, "
        "бюджет, цель. Я сам разложу это на настройки."
    )


def fmt_cc_settings_summary(s: dict, lang: str | None = None) -> str:
    """Этап 1: сводка извлечённых настроек с честными пометками источника (§19.3):
    «(по аналогии)» — из истории аккаунта (GAQL-медианы), «(по умолчанию)» — статический дефолт
    кода. Старые черновики без by_default деградируют в прежний рендер. HTML, esc внутри."""
    lng = _lang(lang)
    by = set(s.get("by_analogy") or [])
    bd = set(s.get("by_default") or [])
    tag = " <i>(по аналогии)</i>" if lng != "en" else " <i>(by analogy)</i>"
    tag_def = " <i>(по умолчанию)</i>" if lng != "en" else " <i>(default)</i>"

    def mk(key: str) -> str:
        if key in by:
            return tag
        return tag_def if key in bd else ""

    geo = ", ".join(s.get("geo_locations") or []) or "—"
    langs = ", ".join(s.get("languages") or []) or "—"
    cur = (s.get("currency") or "").strip()
    cur_s = f" {esc(cur)}" if cur else ""
    # Честный показ денежных полей: 0 micros = значение неизвестно (пустой аккаунт / нет истории /
    # тест-аккаунт без метрик) → «нет данных», а НЕ ложный «0.00» (§B.3 — реальные данные).
    no_data = "no data" if lng == "en" else "нет данных"

    def money(key: str) -> str:
        micros = int(s.get(key, 0) or 0)
        if micros <= 0:
            return no_data
        # У JPY/KRW/… нет минорных единиц — «1 500 JPY», а не «1 500.00 JPY» (живой тест 2026-07-06).
        digits = 0 if cur.upper() in ZERO_DECIMAL_CURRENCIES else 2
        return f"{_thou(micros / 1_000_000, digits)}{cur_s}{mk(key)}"

    budget = money("budget_daily_micros")
    cpc_micros = int(s.get("cpc_bid_micros", 0) or 0)
    cpc = money("cpc_bid_micros")
    # CPC, заданный пользователем (нет тегов источника), — точное «Макс. CPC» без «≈»;
    # медиана/дефолт — прежнее «Ср. CPC: ≈ …».
    cpc_user_set = cpc_micros > 0 and "cpc_bid_micros" not in by and "cpc_bid_micros" not in bd
    cpc_prefix = "≈ " if (cpc_micros > 0 and not cpc_user_set) else ""
    cpc_label = (
        ("Max CPC" if cpc_user_set else "Avg. CPC")
        if lng == "en"
        else ("Макс. CPC" if cpc_user_set else "Ср. CPC")
    )
    strat = _BIDDING_HUMAN[lng].get(
        s.get("bidding_strategy") or "manual_cpc", s.get("bidding_strategy") or "—"
    )
    mt = match_type_human(s.get("match_type") or "phrase", lng)
    name = esc(s.get("campaign_name") or "—")
    # §19.3 (таблица Этапа 1): оплата, сети, расписание, даты — тоже видимы менеджеру.
    payment = (s.get("payment_model") or "cpc").upper()
    if lng == "en":
        nets = "Search + partners" if s.get("networks") == "search_partners" else "Search"
        sched = esc(s.get("ad_schedule") or "24/7")
        dates = f"{esc(s.get('start_date') or 'today')} — {esc(s.get('end_date') or 'no end date')}"
        return (
            "🆕 <b>Campaign (draft)</b>\n"
            f"Name: {name}\n"
            f"Geo: {esc(geo)} · Language: {esc(langs)}\n"
            f"Type: Search · Daily budget: {budget}\n"
            f"Bidding: {esc(strat)}{mk('bidding_strategy')} · Payment: {esc(payment)}\n"
            f"{cpc_label}: {cpc_prefix}{cpc}\n"
            f"Keyword match type: {esc(mt)}{mk('match_type')}\n"
            f"Networks: {nets}{mk('networks')}\n"
            f"Ad schedule: {sched}{mk('ad_schedule')}\n"
            f"Dates: {dates}\n\n"
            "Edit by text (e.g. <i>set budget 60</i>) or confirm the settings."
        )
    nets = "Search + партнёры" if s.get("networks") == "search_partners" else "Search"
    sched = esc(s.get("ad_schedule") or "24/7")
    dates = (
        f"{esc(s.get('start_date') or 'сегодня')} — {esc(s.get('end_date') or 'без даты конца')}"
    )
    return (
        "🆕 <b>Кампания (черновик)</b>\n"
        f"Название: {name}\n"
        f"ГЕО: {esc(geo)} · Язык: {esc(langs)}\n"
        f"Тип: Search · Бюджет/день: {budget}\n"
        f"Стратегия: {esc(strat)}{mk('bidding_strategy')} · Оплата: {esc(payment)}\n"
        f"{cpc_label}: {cpc_prefix}{cpc}\n"
        f"Тип соответствия ключей: {esc(mt)}{mk('match_type')}\n"
        f"Сети: {nets}{mk('networks')}\n"
        f"Расписание: {sched}{mk('ad_schedule')}\n"
        f"Даты: {dates}\n\n"
        "Можно поправить командой (напр. <i>поставь бюджет 60</i>) или подтвердить настройки."
    )


def fmt_asset_spec_label(spec: dict, lang: str | None = None) -> str:
    """Короткая подпись нового ассета (§19.7.2) для сообщения «добавлен ассет: …». Plain text, RU/EN."""
    en = _lang(lang) == "en"
    family = str(spec.get("family") or "")
    p = spec.get("params") or {}
    fam_h = (
        {
            "sitelinks": "Sitelinks",
            "callouts": "Callouts",
            "structured_snippets": "Structured snippet",
            "business_name": "Business name",
            "business_logo": "Logo",
            "call": "Phone",
            "price": "Prices",
            "promotion": "Promotion",
            "lead_form": "Lead form",
        }
        if en
        else {
            "sitelinks": "Доп. ссылки",
            "callouts": "Уточнения",
            "structured_snippets": "Структурное описание",
            "business_name": "Название бизнеса",
            "business_logo": "Логотип",
            "call": "Телефон",
            "price": "Цены",
            "promotion": "Акция",
            "lead_form": "Лид-форма",
        }
    ).get(family, family)
    if family == "sitelinks":
        n = len(p.get("sitelinks") or [])
        return f"{fam_h} ({n})"
    if family == "callouts":
        return f"{fam_h}: " + ", ".join(p.get("callouts") or [])[:80]
    if family == "structured_snippets":
        return f"{fam_h} «{p.get('header', '')}»: " + ", ".join((p.get("values") or [])[:5])
    if family == "business_name":
        return f"{fam_h}: {p.get('business_name', '')}"
    if family == "call":
        return f"{fam_h}: {p.get('phone_number', '')}"
    if family == "lead_form":
        return f"{fam_h}: {p.get('headline', '')}"
    if family == "price":
        off = "offers" if en else "оф."
        return f"{fam_h}: {len(p.get('offerings') or [])} {off} ({p.get('currency', '')})"
    if family == "promotion":
        pct = p.get("percent_off")
        return f"{fam_h}: -{int(pct)}% · {p.get('promotion_target', '')}" if pct else fam_h
    return fam_h


def fmt_cc_final_summary(state: dict, lang: str | None = None) -> str:
    """Этап 7: финальная сводка всего черновика кампании перед созданием (§19.8). HTML, esc внутри."""
    lng = _lang(lang)
    s = state.get("settings") or {}
    ad = state.get("ad") or {}
    kw = state.get("keywords") or {}
    imgs = state.get("images") or {}
    assets = state.get("assets") or {}
    url = state.get("url_options") or {}
    geo = ", ".join(s.get("geo_locations") or []) or "—"
    cur = (s.get("currency") or "").strip()
    # Как в fmt_cc_settings_summary: zero-decimal валюты без копеек («1 500 JPY», не «1 500.00 JPY»)
    digits = 0 if cur.upper() in ZERO_DECIMAL_CURRENCIES else 2
    budget = _thou(int(s.get("budget_daily_micros", 0)) / 1_000_000, digits)
    cur_s = f" {esc(cur)}" if cur else ""
    strat = _BIDDING_HUMAN[lng].get(s.get("bidding_strategy") or "manual_cpc", "—")
    path = "/".join(p for p in (ad.get("path1"), ad.get("path2")) if p)
    n_kw = len(kw.get("list") or [])
    n_h = len(ad.get("headlines") or [])
    n_d = len(ad.get("descriptions") or [])
    n_img = len(imgs.get("media_ids") or [])
    n_reuse = len(assets.get("reuse_links") or [])
    n_new = len(assets.get("new") or [])
    url_bits = [
        b
        for b in (
            "tracking" if url.get("tracking_url_template") else "",
            "suffix" if url.get("final_url_suffix") else "",
            f"{len(url.get('custom_parameters') or {})} params"
            if (url.get("custom_parameters") or {})
            else "",
        )
        if b
    ]
    # 2.10 (§19.8): «утверждённые заголовки и описания (с длинами)» — тексты, а не только счётчики.
    from adcopy.validate import LIMITS, rsa_len

    hl, dl = LIMITS["headline"], LIMITS["description"]
    h_list = "\n".join(f"  • {esc(h)} [{rsa_len(h)}/{hl}]" for h in (ad.get("headlines") or []))
    d_list = "\n".join(f"  • {esc(d)} [{rsa_len(d)}/{dl}]" for d in (ad.get("descriptions") or []))
    head = "🆕 <b>Final summary</b>" if lng == "en" else "🆕 <b>Финальная сводка</b>"
    if lng == "en":
        ad_block = f"\nAd: {n_h} headlines / {n_d} descriptions"
        if h_list:
            ad_block += f"\nHeadlines:\n{h_list}"
        if d_list:
            ad_block += f"\nDescriptions:\n{d_list}"
        body = (
            f"Campaign: {esc(s.get('campaign_name') or '—')}\n"
            f"Geo: {esc(geo)} · Budget/day: {budget}{cur_s} · {esc(strat)}\n"
            f"Final URL: {esc(ad.get('final_url') or '—')}"
            + (f" · path: {esc(path)}" if path else "")
            + ad_block
            + f"\nKeywords: {n_kw} "
            f"({match_type_human(kw.get('match_type') or 'phrase', lng)})\n"
            f"Images: {n_img} · assets: {n_reuse} reused, {n_new} new\n"
            f"URL options: {', '.join(url_bits) or '—'}\n\n"
            "Edit by command (e.g. <i>set budget 60</i>), or create the draft."
        )
    else:
        ad_block = f"\nОбъявление: {n_h} заголовков / {n_d} описаний"
        if h_list:
            ad_block += f"\nЗаголовки:\n{h_list}"
        if d_list:
            ad_block += f"\nОписания:\n{d_list}"
        body = (
            f"Кампания: {esc(s.get('campaign_name') or '—')}\n"
            f"ГЕО: {esc(geo)} · Бюджет/день: {budget}{cur_s} · {esc(strat)}\n"
            f"Final URL: {esc(ad.get('final_url') or '—')}"
            + (f" · path: {esc(path)}" if path else "")
            + ad_block
            + f"\nКлючей: {n_kw} "
            f"({match_type_human(kw.get('match_type') or 'phrase', lng)})\n"
            f"Изображения: {n_img} · ассеты: {n_reuse} переисп., {n_new} новых\n"
            f"URL-опции: {', '.join(url_bits) or '—'}\n\n"
            "Поправьте командой (напр. <i>поставь бюджет 60</i>) или создайте черновик."
        )
    return f"{head}\n{body}"


# ── §20: «Информация про клиентов» ────────────────────────────────────────────────
def fmt_crawl_summary(
    domain: str,
    *,
    pages: int,
    sections: list[str],
    services: list[str],
    prices: list[str],
    phones: list[str],
    socials: list[str],
    lang: str | None = None,
) -> str:
    """§20.4: сводка результата краулинга (что нашли на сайте). HTML, всё через esc(). Пустые
    блоки опускаются. Заголовок/суффикс («профиль обновлён»/confirm) добавляет вызывающий."""
    lng = _lang(lang)
    d = esc(domain)
    head = (
        f"✅ <b>Crawl of {d} finished.</b>" if lng == "en" else f"✅ <b>Краулинг {d} завершён.</b>"
    )
    lines = [head, ("• Pages: " if lng == "en" else "• Обойдено страниц: ") + str(pages)]

    def _row(label_ru: str, label_en: str, items: list[str], limit: int) -> None:
        vals = [esc(x) for x in items if x][:limit]
        if vals:
            lines.append(("• " + (label_en if lng == "en" else label_ru) + ": ") + ", ".join(vals))

    _row("Разделы", "Sections", sections, 8)
    _row("Услуги", "Services", services, 8)
    _row("Цены", "Prices", prices, 5)
    _row("Контакты", "Contacts", phones, 3)
    _row("Соцсети", "Socials", socials, 6)
    return "\n".join(lines)


def fmt_client_card(profile: dict | None, customer_id: str, lang: str | None = None) -> str:
    """§20.2 «Карточка клиента»: сохранённый профиль (бренд/бизнес/услуги/цены/контакты/сайт).
    profile=None → пустая карточка с приглашением добавить инфу. HTML, всё через esc()."""
    lng = _lang(lang)
    cid = esc(str(customer_id))
    if not profile:
        if lng == "en":
            return (
                f"ℹ️ <b>Client</b> · <code>{cid}</code>\n\n"
                "No profile yet. Add client info as free text — business, site, services, "
                "prices, phones."
            )
        return (
            f"ℹ️ <b>Клиент</b> · <code>{cid}</code>\n\n"
            "Профиль пуст. Пришлите информацию о клиенте обычным текстом — бизнес, сайт, услуги, "
            "цены, телефоны."
        )
    brand_fallback = "Client" if lng == "en" else "Клиент"
    lines: list[str] = [
        f"ℹ️ <b>{esc(profile.get('brand') or brand_fallback)}</b> · <code>{cid}</code>"
    ]
    if profile.get("business_desc"):
        lines.append(("Business: " if lng == "en" else "Бизнес: ") + esc(profile["business_desc"]))
    if profile.get("geo"):
        lines.append(("Geo: " if lng == "en" else "Гео: ") + esc(profile["geo"]))
    if profile.get("language"):
        lines.append(("Language: " if lng == "en" else "Язык: ") + esc(profile["language"]))
    services = profile.get("services") or []
    if services:
        svc = []
        for it in services[:10]:
            s = esc(it.get("name", ""))
            if it.get("price"):
                s += f" ({esc(it['price'])})"
            if it.get("category"):  # 2.10 (§20.6): категория видна менеджеру, а не «мёртвые данные»
                s += f" [{esc(it['category'])}]"
            svc.append(s)
        lines.append(("Services: " if lng == "en" else "Услуги: ") + "; ".join(x for x in svc if x))
        cats = sorted({str(it.get("category") or "").strip() for it in services} - {""})
        if cats:
            lines.append(
                ("Categories: " if lng == "en" else "Категории: ") + esc(", ".join(cats[:12]))
            )
    contacts = profile.get("contacts") or []
    if contacts:
        cs = "; ".join(esc(c.get("value", "")) for c in contacts[:6] if c.get("value"))
        if cs:
            lines.append(("Contacts: " if lng == "en" else "Контакты: ") + cs)
    if profile.get("website"):
        lines.append(("Site: " if lng == "en" else "Сайт: ") + esc(profile["website"]))
    socials = profile.get("socials") or {}
    if socials:
        ss = "; ".join(f"{esc(k)}: {esc(v)}" for k, v in list(socials.items())[:6])
        lines.append(("Socials: " if lng == "en" else "Соцсети: ") + ss)
    if profile.get("notes"):
        lines.append(("Notes: " if lng == "en" else "Заметки: ") + esc(profile["notes"]))
    pages = int(profile.get("site_pages_count") or 0)
    if pages:
        lines.append(("Crawled pages: " if lng == "en" else "Страниц с сайта: ") + str(pages))
    # §20.2: дата последнего краула (требование карточки; persist был, в UI не выводился — P1-D).
    lc = profile.get("last_crawled_at")
    if lc is not None:
        try:
            lc_s = lc.strftime("%Y-%m-%d %H:%M") if hasattr(lc, "strftime") else str(lc)
        except Exception:  # noqa: BLE001 — дата необязательна, не роняем карточку
            lc_s = str(lc)
        lines.append(("Last crawl: " if lng == "en" else "Последний краул: ") + esc(lc_s))
    return "\n".join(lines)


def _profile_summary_line(p: dict, lng: str) -> str:
    """Короткая сводка непустых полей профиля для «было→станет» (без длинных тел)."""
    bits: list[str] = []
    for key, ru, en in (
        ("brand", "бренд", "brand"),
        ("business_desc", "бизнес", "business"),
        ("geo", "гео", "geo"),
        ("language", "язык", "language"),
        ("website", "сайт", "site"),
    ):
        if p.get(key):
            bits.append(en if lng == "en" else ru)
    ns = len(p.get("services") or [])
    nc = len(p.get("contacts") or [])
    if ns:
        bits.append(f"{'services' if lng == 'en' else 'услуги'}×{ns}")
    if nc:
        bits.append(f"{'contacts' if lng == 'en' else 'контакты'}×{nc}")
    return ", ".join(bits) or ("—")


def fmt_client_diff(
    before: dict | None, after: dict, customer_id: str, *, operation: str, lang: str | None = None
) -> str:
    """§20.5 «было→станет» для confirm-гейта профиля. Показываем сводку полей до/после (без PII в
    длинных телах). operation: profile_save|profile_update|profile_clear."""
    lng = _lang(lang)
    cid = esc(str(customer_id))
    if operation == "profile_clear":
        if lng == "en":
            return (
                f"🗑 <b>Clear client profile</b> · <code>{cid}</code>\n"
                "The profile and all details will be removed (cannot be undone)."
            )
        return (
            f"🗑 <b>Очистить профиль клиента</b> · <code>{cid}</code>\n"
            "Профиль и все детали будут удалены (восстановить нельзя)."
        )
    head = (
        f"ℹ️ <b>Client profile</b> · <code>{cid}</code>"
        if lng == "en"
        else f"ℹ️ <b>Профиль клиента</b> · <code>{cid}</code>"
    )
    was = _profile_summary_line(before or {}, lng)
    now = _profile_summary_line(after, lng)
    label_was = "Was" if lng == "en" else "Было"
    label_now = "Becomes" if lng == "en" else "Станет"
    out = f"{head}\n{label_was}: {esc(was)}\n{label_now}: {esc(now)}"
    # §20.5: реальная дельта ПО ПОЛЯМ — менеджер должен видеть, ЧТО меняется («бренд: A → B»),
    # а не только имя поля (аудит: shallow-merge прятал изменение существующего значения).
    changes = _profile_field_changes(before or {}, after, lng)
    if changes:
        label = "Changes" if lng == "en" else "Изменения"
        out += f"\n{label}:\n" + "\n".join(f"  • {c}" for c in changes)
    return out


def _clip(v: object, n: int = 60) -> str:
    s = str(v or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


_DIFF_ITEMS_CAP = 6  # именованных элементов на категорию в diff (дальше «(+N ещё)»)


def _profile_field_changes(before: dict, after: dict, lng: str) -> list[str]:
    """§20.5: список per-field изменений «old → new» (esc-нутые строки). Пустой — ничего не меняется
    (или меняются только неотслеживаемые ключи). Длинные значения усечены (без простыни в чате).

    services/contacts — ПОИМЁННО (+«новое», −«удалённое» (только при явном replace), «X»: цена A→B),
    а не голые счётчики ×N→×M: менеджер должен видеть, ЧТО именно добавится/пропадёт, до «да».
    notes-append рендерится как «+хвост» (старое не перечитываем)."""
    from clients.store import contact_key, svc_key  # чистые ключи мерджа — та же семантика

    names = {
        "brand": ("бренд", "brand"),
        "business_desc": ("бизнес", "business"),
        "geo": ("гео", "geo"),
        "language": ("язык", "language"),
        "website": ("сайт", "site"),
    }
    out: list[str] = []
    for key, (ru, en) in names.items():
        old, new = before.get(key), after.get(key)
        if new is None or old == new:  # None в after = поле не трогается (shallow-merge)
            continue
        label = en if lng == "en" else ru
        old_s = f"«{_clip(old)}»" if old else "—"
        out.append(esc(f"{label}: {old_s} → «{_clip(new)}»"))
    # заметки: append-семантика (§20.3) → показываем только ДОБАВЛЕННЫЙ хвост
    old_n, new_n = str(before.get("notes") or ""), after.get("notes")
    if new_n is not None and old_n != new_n:
        label = "notes" if lng == "en" else "заметки"
        if old_n and str(new_n).startswith(old_n):  # чистый append
            tail = str(new_n)[len(old_n) :].strip().lstrip("— ").strip()
            out.append(esc(f"{label}: +«{_clip(tail)}»"))
        else:
            old_s = f"«{_clip(old_n)}»" if old_n else "—"
            out.append(esc(f"{label}: {old_s} → «{_clip(new_n)}»"))

    def _named(key: str, ru: str, en: str, item_key, item_label) -> None:
        if key not in after or after.get(key) is None:
            return
        old_items = {item_key(it): it for it in (before.get(key) or [])}
        new_items = {item_key(it): it for it in (after.get(key) or [])}
        added = [new_items[k] for k in new_items if k not in old_items]
        removed = [old_items[k] for k in old_items if k not in new_items]
        changed = [
            (old_items[k], new_items[k])
            for k in new_items
            if k in old_items and old_items[k] != new_items[k]
        ]
        if not (added or removed or changed):
            return
        label = en if lng == "en" else ru
        bits: list[str] = []
        for it in added[:_DIFF_ITEMS_CAP]:
            bits.append(f"+«{_clip(item_label(it), 40)}»")
        for it in removed[:_DIFF_ITEMS_CAP]:
            bits.append(f"−«{_clip(item_label(it), 40)}»")
        for old_it, new_it in changed[:_DIFF_ITEMS_CAP]:
            if key == "services" and old_it.get("price") != new_it.get("price"):
                bits.append(
                    f"«{_clip(new_it.get('name'), 30)}»: {_clip(old_it.get('price') or '—', 20)}"
                    f" → {_clip(new_it.get('price') or '—', 20)}"
                )
            else:
                bits.append(f"«{_clip(item_label(new_it), 40)}» ✎")
        overflow = max(0, len(added) + len(removed) + len(changed) - len(bits))
        if overflow:
            bits.append(f"(+{overflow} " + ("more)" if lng == "en" else "ещё)"))
        out.append(esc(f"{label}: " + ", ".join(bits)))

    _named(
        "services",
        "услуги",
        "services",
        lambda it: svc_key(it.get("name")),
        lambda it: it.get("name") or "",
    )
    _named("contacts", "контакты", "contacts", contact_key, lambda it: it.get("value") or "")
    return out
