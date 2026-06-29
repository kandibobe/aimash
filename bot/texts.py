"""Тексты и шаблоны сообщений бота (RU). Вынесены отдельно — упрощает правки и будущую
EN-локализацию (ТЗ §4). Формат — HTML (parse_mode='HTML' на стороне отправки в bot.main);
ВСЕ динамические данные (имена кампаний, текст ошибок) обязательно через esc().
"""

from __future__ import annotations

import html


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
    "<b>Команды</b>\n"
    "/status — быстрая статистика (30 дн.)\n"
    "/campaigns — кампании + быстрые действия (пауза/возобновление, 🎯 аудитории)\n"
    "/pause Название — поставить кампанию на паузу (с подтверждением)\n"
    "/resume Название — возобновить кампанию (с подтверждением)\n"
    "/report [7|30|90|MTD | ГГГГ-ММ-ДД [ГГГГ-ММ-ДД]] — сводка за период (по умолч. 30 дн.)\n"
    "/export [период] — глубокий отчёт .xlsx (пресет или диапазон дат)\n"
    "/sheets [период] — глубокий отчёт в Google Sheets (ссылка)\n"
    "/rsa — сгенерировать тексты объявления (RSA), поэлементный confirm (создаётся на паузе)\n"
    "/newsearch — создать поисковую кампанию (RSA + ключи), на паузе — запуск отдельно\n"
    "/keywords — подбор ключевых слов (объём, конкуренция, кластеры) + .xlsx\n"
    "🖼 пришли фото — соберу медийную кампанию (GDN), создам после «да» (на паузе)\n"
    "/templates — шаблоны кампаний: список и создание по шаблону\n"
    "/savetemplate имя [from Кампания] — сохранить настройки как шаблон\n"
    "/recent — недавние действия: повторить в один тап (с подтверждением)\n"
    "/model — выбрать модель ИИ (OpenRouter)\n"
    "/lang — язык интерфейса (RU/EN)\n"
    "/balance — бюджет ИИ: баланс OpenRouter и траты\n"
    "/journal — журнал изменений: что и когда менялось\n"
    "/cancel — отменить текущий черновик\n\n"
    "<b>Как в другой кампании / по брифу</b>\n"
    "• «сделай кампанию N с настройками как в кампании X» — клонирую настройки.\n"
    "• 🧩 в /campaigns → «Расширения»: быстрые ссылки, уточнения, структурные описания, картинка.\n"
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
KW_ASK = (
    "🔍 <b>Подбор ключевых слов</b>\n"
    "Пришли сид-слова через запятую и/или ссылку одним сообщением.\n"
    "Например: <code>доставка цветов, букеты, 101 роза</code>\n"
    "или ссылку <code>https://example.com</code>"
)
KW_SEARCHING = "⏳ Подбираю ключевые слова и группирую по интенту…"
KW_EMPTY = "Ничего не нашлось по этим сидам. Попробуй другие слова или ссылку: /keywords"
KW_BAD_INPUT = "Нужны сид-слова или ссылка. Пришли, например: <code>купить телефон, смартфон</code>"

PROPOSAL_PENDING = (
    "📝 <b>Черновик изменения</b>\n\n{summary}\n\nПодтвердить? <i>(черновик действует 24 ч)</i>"
)
EXECUTING = "⏳ Выполняю…"
APPLIED = "✅ <b>Готово.</b>\n{result}"
FAILED = "⚠️ Не удалось выполнить: {kind}: {err}"
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
    "Пришли одним сообщением: <b>название | ссылка | дневной бюджет</b>.\n"
    "Например: <code>Весна 2026 | https://shop.example | 50</code>\n\n"
    "Тексты сгенерирую сам — покажу черновик «было → станет» перед созданием."
)
GDN_BAD_BRIEF = (
    "Не разобрал. Нужно <b>название | ссылка | бюджет</b> (бюджет — число).\n"
    "Например: <code>Летняя распродажа | https://shop.example | 30</code>"
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


def fmt_search_proposal_summary(
    name: str,
    url: str,
    budget_units: float,
    headlines: list[str],
    descriptions: list[str],
    keywords: list[str],
    match_type: str,
    lang: str | None = None,
) -> str:
    """Плейн-текст сводка create_search_campaign для confirm-гейта (esc применяется при показе)."""
    lng = _lang(lang)
    h_lines = "\n".join(f"  • {h}" for h in headlines)
    d_lines = "\n".join(f"  • {d}" for d in descriptions)
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
            f"Daily budget: {budget_units:g}\n\n"
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
        f"Дневной бюджет: {budget_units:g}\n\n"
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
    lang: str | None = None,
) -> str:
    """Плейн-текст сводка create_gdn_campaign для confirm-гейта (esc применяется при показе)."""
    h_lines = "\n".join(f"  • {h}" for h in headlines)
    d_lines = "\n".join(f"  • {d}" for d in descriptions)
    if _lang(lang) == "en":
        return (
            f"Create a display campaign (GDN) “{name}” — paused.\n"
            f"Business: {business_name}\n"
            f"Link: {url}\n"
            f"Daily budget: {budget_units:g}\n"
            "Image: 1 (cropped to 1.91:1 and 1:1)\n\n"
            f"Headlines ({len(headlines)}):\n{h_lines}\n\n"
            f"Descriptions ({len(descriptions)}):\n{d_lines}"
        )
    return (
        f"Создать медийную кампанию (GDN) «{name}» — на паузе.\n"
        f"Бизнес: {business_name}\n"
        f"Ссылка: {url}\n"
        f"Дневной бюджет: {budget_units:g}\n"
        "Изображение: 1 (обрезано в 1.91:1 и 1:1)\n\n"
        f"Заголовки ({len(headlines)}):\n{h_lines}\n\n"
        f"Описания ({len(descriptions)}):\n{d_lines}"
    )


# ── Человекочитаемая сводка черновика мутации (ТЗ §5) ────────────────────────────
KW_INLINE_MAX = 20  # ключей показываем в сводке черновика; больше — во вложении .xlsx
_CURRENCY_HUMAN = {"USD": "USD", "UAH": "грн", "EUR": "EUR", "percent": "%"}


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
        }.get(operation, operation)
    return {
        "add_keywords": "Добавить ключевые слова",
        "remove_keywords": "Удалить ключевые слова",
        "add_negative_keywords": "Добавить минус-слова",
    }.get(operation, operation)


def _fmt_micros(micros: int) -> str:
    """micros (1e6 = единица валюты аккаунта) → «12 480.00» (разделитель тысяч, 2 знака)."""
    try:
        return _thou(int(micros) / 1_000_000, 2)
    except (TypeError, ValueError):
        return str(micros)


def _micros_range(values: list) -> str:
    """Диапазон ставок групп: одинаковые → одно число, иначе «min–max»."""
    nums = [int(x) for x in values] if values else []
    if not nums:
        return "—"
    lo, hi = min(nums), max(nums)
    return _fmt_micros(lo) if lo == hi else f"{_fmt_micros(lo)}–{_fmt_micros(hi)}"


def _before(params: dict) -> dict | None:
    """Снимок текущего значения из proposal.params['_before'] (см. ads.service.read_before)."""
    b = params.get("_before")
    return b if isinstance(b, dict) else None


def _money_summary(label: str, params: dict, lang: str | None = None) -> str:
    c = params.get("campaign", "")
    mode = params.get("mode")
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
            before_s, after_s = _fmt_micros(b["before_micros"]), _fmt_micros(b.get("after_micros"))
            if mode == "increase_by_percent":
                tail = f" (+{v}%)"
            elif mode == "increase_by_amount":
                tail = f" (+{f'{v} {cur}'.strip()})"
            else:
                tail = ""
            return f"Campaign “{c}” — {label}: {before_s} → {after_s}{tail}".rstrip()
        if mode == "increase_by_percent":
            return f"Campaign “{c}” — {label}: +{v}%"
        if mode == "increase_by_amount":
            return f"Campaign “{c}” — {label}: +{v} {cur}".rstrip()
        if mode == "set_to":
            return f"Campaign “{c}” — {label} → {v} {cur}".rstrip()
        return f"Campaign “{c}” — {label}: {v} {cur}".rstrip()
    # §5: реальное «было → станет», если снимок прочитан (числа — в валюте аккаунта).
    if b and b.get("kind") == "budget" and b.get("before_micros") is not None:
        before_s, after_s = _fmt_micros(b["before_micros"]), _fmt_micros(b.get("after_micros"))
        if mode == "increase_by_percent":
            tail = f" (+{v}%)"
        elif mode == "increase_by_amount":
            tail = f" (+{f'{v} {cur}'.strip()})"  # cur='' (валюта аккаунта) → «(+10)», без лишнего пробела
        else:
            tail = ""
        return f"Кампания «{c}» — {label}: {before_s} → {after_s}{tail}".rstrip()
    # fallback без «было» (чтение не удалось / старый черновик)
    if mode == "increase_by_percent":
        return f"Кампания «{c}» — {label}: +{v}%"
    if mode == "increase_by_amount":
        return f"Кампания «{c}» — {label}: +{v} {cur}".rstrip()
    if mode == "set_to":
        return f"Кампания «{c}» — {label} → {v} {cur}".rstrip()
    return f"Кампания «{c}» — {label}: {v} {cur}".rstrip()


def _bid_summary(params: dict, lang: str | None = None) -> str:
    c = params.get("campaign", "")
    mode = params.get("mode")
    try:
        v = f"{float(params.get('value')):g}"
    except (TypeError, ValueError):
        v = str(params.get("value"))
    b = _before(params)
    if _lang(lang) == "en":
        if b and b.get("kind") == "bid" and b.get("before_micros"):
            n = b.get("n_groups") or len(b["before_micros"])
            rng_b = _micros_range(b["before_micros"])
            rng_a = _micros_range(b.get("after_micros") or [])
            if mode == "increase_by_percent":
                return f"Campaign “{c}” — CPC bid: +{v}% (current {rng_b} → {rng_a}; groups: {n})"
            return f"Campaign “{c}” — CPC bid: {rng_b} → {rng_a} (groups: {n})"
        return _money_summary("CPC bid", params, lang)
    if b and b.get("kind") == "bid" and b.get("before_micros"):
        n = b.get("n_groups") or len(b["before_micros"])
        rng_b, rng_a = _micros_range(b["before_micros"]), _micros_range(b.get("after_micros") or [])
        if mode == "increase_by_percent":
            return f"Кампания «{c}» — ставка CPC: +{v}% (текущие {rng_b} → {rng_a}; групп: {n})"
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
        return (
            f"Кампания «{c}» — радиус {rs} км вокруг «{city}» ({cc}). Заменит прежний гео-радиус."
        )
    if operation == "set_geo_location":
        locs = ", ".join(str(x) for x in (params.get("locations") or []))
        cc = params.get("country_code", "")
        return (
            f"Кампания «{c}» — гео-таргетинг: {locs} ({cc}). "
            "Заменит прежний географический таргетинг кампании."
        )
    if operation == "set_bidding_strategy":
        strat = {
            "manual_cpc": "Ручная CPC",
            "maximize_conversions": "Максимум конверсий",
            "maximize_conversion_value": "Максимум ценности конверсий",
            "target_spend": "Максимум кликов",
        }.get(params.get("strategy", ""), params.get("strategy", ""))
        extra = ""
        if params.get("target_cpa"):
            extra = f", target CPA {float(params['target_cpa']):g}"
        elif params.get("target_roas"):
            extra = f", target ROAS {float(params['target_roas']):g}"
        elif params.get("strategy") == "manual_cpc" and params.get("enhanced_cpc"):
            extra = ", enhanced CPC"
        return f"Кампания «{c}» — стратегия ставок → {strat}{extra}."
    if operation in ("add_keywords", "remove_keywords", "add_negative_keywords"):
        kws = params.get("keywords") or []
        mt = match_type_human(params.get("match_type", ""), lng)
        what = "минус-слов" if operation == "add_negative_keywords" else "ключевых слов"
        verb = "удалить" if operation == "remove_keywords" else "добавить"
        head = f"Кампания «{c}» — {verb} {len(kws)} {what} (тип соответствия: {mt}):"
        shown = list(kws)[:KW_INLINE_MAX]
        lines = "\n".join(f"  • {k}" for k in shown)
        if len(kws) > KW_INLINE_MAX:
            lines += f"\n  …ещё {len(kws) - KW_INLINE_MAX} — полный список во вложении .xlsx"
        return f"{head}\n{lines}"
    if operation == "attach_audience":
        names = params.get("_audience_names") or []  # дружелюбные имена (инертны для исполнения)
        rns = params.get("audience_resource_names") or []
        label = ", ".join(str(n) for n in names) if names else f"{len(rns)} шт."
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
        return f"Campaign “{c}” — {rs} km radius around “{city}” ({cc}). Replaces the prior geo-radius."
    if operation == "set_geo_location":
        locs = ", ".join(str(x) for x in (params.get("locations") or []))
        cc = params.get("country_code", "")
        return (
            f"Campaign “{c}” — geo-targeting: {locs} ({cc}). "
            "Replaces the campaign's prior geographic targeting."
        )
    if operation == "set_bidding_strategy":
        strat = {
            "manual_cpc": "Manual CPC",
            "maximize_conversions": "Maximize conversions",
            "maximize_conversion_value": "Maximize conversion value",
            "target_spend": "Maximize clicks",
        }.get(params.get("strategy", ""), params.get("strategy", ""))
        extra = ""
        if params.get("target_cpa"):
            extra = f", target CPA {float(params['target_cpa']):g}"
        elif params.get("target_roas"):
            extra = f", target ROAS {float(params['target_roas']):g}"
        elif params.get("strategy") == "manual_cpc" and params.get("enhanced_cpc"):
            extra = ", enhanced CPC"
        return f"Campaign “{c}” — bidding strategy → {strat}{extra}."
    if operation in ("add_keywords", "remove_keywords", "add_negative_keywords"):
        kws = params.get("keywords") or []
        mt = match_type_human(params.get("match_type", ""), "en")
        what = "negative keywords" if operation == "add_negative_keywords" else "keywords"
        verb = "remove" if operation == "remove_keywords" else "add"
        head = f"Campaign “{c}” — {verb} {len(kws)} {what} (match type: {mt}):"
        shown = list(kws)[:KW_INLINE_MAX]
        lines = "\n".join(f"  • {k}" for k in shown)
        if len(kws) > KW_INLINE_MAX:
            lines += f"\n  …{len(kws) - KW_INLINE_MAX} more — full list in the .xlsx attachment"
        return f"{head}\n{lines}"
    if operation == "attach_audience":
        names = params.get("_audience_names") or []
        rns = params.get("audience_resource_names") or []
        label = ", ".join(str(n) for n in names) if names else f"{len(rns)} item(s)"
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
    if operation == "remove_asset_link":
        n = len(params.get("link_resource_names") or [])
        return f"Detach {n} extension(s) from the campaign (assets are not deleted)."
    return ""  # create_rsa / create_gdn_campaign / unknown — keep caller's summary


def fmt_keywords_summary(
    clusters, by_text: dict, total: int, src: str, lang: str | None = None
) -> str:
    """Сводка keyword research: топ-кластеры с топ-ключами и объёмами. Полная таблица — в .xlsx.

    clusters — объекты с .name/.intent/.keywords (duck-typed); by_text — {ключ: объём/мес}.
    Усечение (кластеров/ключей) помечается явно, без «тихого» обрезания."""
    max_clusters, max_kw = 8, 6
    if _lang(lang) == "en":
        lines = [
            f"🔍 <b>Keywords</b> — {esc(src)}",
            f"Ideas: <b>{total}</b>, clusters: {len(clusters)}\n",
        ]
        for cl in clusters[:max_clusters]:
            intent = f" · <i>{esc(cl.intent)}</i>" if cl.intent else ""
            lines.append(f"<b>{esc(cl.name)}</b>{intent} ({len(cl.keywords)})")
            ordered = sorted(cl.keywords, key=lambda k: by_text.get(k, 0), reverse=True)
            for kw in ordered[:max_kw]:
                lines.append(f"  • {esc(kw)} — {_thou(by_text.get(kw, 0))}/mo")
            if len(cl.keywords) > max_kw:
                lines.append(f"  …{len(cl.keywords) - max_kw} more — see .xlsx")
        if len(clusters) > max_clusters:
            lines.append(f"\n…{len(clusters) - max_clusters} more clusters — see .xlsx")
        lines.append(
            "\n<i>This is a suggestion, not an action. Full table is in the attachment.</i>"
        )
        return "\n".join(lines)
    lines = [
        f"🔍 <b>Ключевые слова</b> — {esc(src)}",
        f"Идей: <b>{total}</b>, кластеров: {len(clusters)}\n",
    ]
    for cl in clusters[:max_clusters]:
        intent = f" · <i>{esc(cl.intent)}</i>" if cl.intent else ""
        lines.append(f"<b>{esc(cl.name)}</b>{intent} ({len(cl.keywords)})")
        ordered = sorted(cl.keywords, key=lambda k: by_text.get(k, 0), reverse=True)
        for kw in ordered[:max_kw]:
            lines.append(f"  • {esc(kw)} — {_thou(by_text.get(kw, 0))}/мес")
        if len(cl.keywords) > max_kw:
            lines.append(f"  …ещё {len(cl.keywords) - max_kw} — см. .xlsx")
    if len(clusters) > max_clusters:
        lines.append(f"\n…ещё {len(clusters) - max_clusters} кластеров — см. .xlsx")
    lines.append("\n<i>Это подсказка, не действие. Полная таблица — во вложении.</i>")
    return "\n".join(lines)


# ── Рендер с данными ─────────────────────────────────────────────────────────────
def fmt_stats(
    account: str, days: int, st: dict, currency: str = "", lang: str | None = None
) -> str:
    """Статистика аккаунта с вычисленными в КОДЕ CTR/CPC (контракт read не трогаем).
    currency (§9) — код валюты аккаунта для денежных строк; пустой → без явной валюты."""
    imp = int(st.get("impressions") or 0)
    clk = int(st.get("clicks") or 0)
    cost = float(st.get("cost") or 0)
    conv = float(st.get("conversions") or 0)
    cval = float(st.get("conv_value") or 0)
    ctr = (clk / imp * 100) if imp else 0.0
    cpc = (cost / clk) if clk else 0.0
    cur = f" {esc(currency)}" if currency else ""
    if _lang(lang) == "en":
        return (
            f"📊 <b>Account …{esc(str(account)[-4:])}</b> · {days} d.\n\n"
            f"Impressions: <b>{_thou(imp)}</b>\n"
            f"Clicks:      <b>{_thou(clk)}</b>  (CTR {ctr:.2f}%)\n"
            f"Cost:        <b>{_thou(cost, 2)}{cur}</b>\n"
            f"Avg. CPC:    <b>{_thou(cpc, 2)}{cur}</b>\n"
            f"Conversions: <b>{conv:g}</b>\n"
            f"Value:       <b>{_thou(cval, 2)}{cur}</b>"
        )
    return (
        f"📊 <b>Аккаунт …{esc(str(account)[-4:])}</b> · {days} дн.\n\n"
        f"Показы:      <b>{_thou(imp)}</b>\n"
        f"Клики:       <b>{_thou(clk)}</b>  (CTR {ctr:.2f}%)\n"
        f"Расход:      <b>{_thou(cost, 2)}{cur}</b>\n"
        f"Ср. CPC:     <b>{_thou(cpc, 2)}{cur}</b>\n"
        f"Конверсии:   <b>{conv:g}</b>\n"
        f"Ценность:    <b>{_thou(cval, 2)}{cur}</b>"
    )


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
}
_OP_HUMAN_EN = {
    "update_budget": "budget",
    "update_bid": "CPC bid",
    "add_keywords": "add keywords",
    "remove_keywords": "remove keywords",
    "add_negative_keywords": "negative keywords",
    "pause_campaign": "pause campaign",
    "resume_campaign": "resume campaign",
    "set_geo_proximity": "geo-radius",
    "set_geo_location": "geo-locations",
    "set_bidding_strategy": "bidding strategy",
    "attach_audience": "audiences",
    "create_rsa": "create RSA",
    "create_gdn_campaign": "create GDN campaign",
    "create_search_campaign": "create Search campaign",
}


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
        if e.status == "failed" and isinstance(e.result, dict):
            err = str(e.result.get("error") or "").strip()
            if err:
                L.append(f"    ↳ {esc(err[:120])}")
    L.append("")
    if en:
        L.append("<i>Full history and “before→after” are stored in the DB (audit_log).</i>")
    else:
        L.append("<i>Полная история и «было→станет» хранятся в БД (audit_log).</i>")
    return "\n".join(L)


def campaigns_title(account: str, lang: str | None = None) -> str:
    if _lang(lang) == "en":
        return f"📋 <b>Campaigns of account …{esc(str(account)[-4:])}</b>\nChoose a campaign:"
    return f"📋 <b>Кампании аккаунта …{esc(str(account)[-4:])}</b>\nВыбери кампанию:"


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
