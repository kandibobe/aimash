"""Тексты и шаблоны сообщений бота (RU). Вынесены отдельно — упрощает правки и будущую
EN-локализацию (ТЗ §4). Формат — HTML (parse_mode='HTML' на стороне отправки в bot.main);
ВСЕ динамические данные (имена кампаний, текст ошибок) обязательно через esc().
"""

from __future__ import annotations

import html


def esc(s: object) -> str:
    """Экранирование для HTML parse_mode (имена кампаний/ошибки могут содержать < & >)."""
    return html.escape(str(s), quote=False)


def _thou(n: float, dec: int = 0) -> str:
    """Число с пробелом-разделителем тысяч: 12480 -> '12 480', 4512.3 -> '4 512.30'."""
    return f"{n:,.{dec}f}".replace(",", " ")


def status_human(status: str) -> str:
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
    "/rsa — сгенерировать тексты объявления (RSA) с поэлементным подтверждением\n"
    "/keywords — подбор ключевых слов (объём, конкуренция, кластеры) + .xlsx\n"
    "🖼 пришли фото — соберу медийную кампанию (GDN), создам после «да»\n"
    "/model — выбрать модель ИИ (OpenRouter)\n"
    "/balance — бюджет ИИ: баланс OpenRouter и траты\n"
    "/cancel — отменить текущий черновик\n\n"
    "<i>Отчёты по расписанию и алерты аномалий работают в фоне.</i>"
)


# ── Переключатель модели ИИ (/model) ─────────────────────────────────────────────
def fmt_model_menu(active: str | None, parsing: str, copy: str) -> str:
    """Экран /model: что активно сейчас + что реально пойдёт в запросы (parsing/copy)."""
    head = (
        f"🧠 <b>Активная модель:</b> <code>{esc(active)}</code>"
        if active
        else "🧠 <b>Модель:</b> по умолчанию (из настроек)"
    )
    same = parsing == copy
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

PROPOSAL_PENDING = "📝 <b>Черновик изменения</b>\n\n{summary}\n\nПодтвердить?"
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


def audiences_title(campaign: str) -> str:
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


def fmt_rsa_element(kind: str, idx: int, total: int, e: dict, campaign: str, ad_group: str) -> str:
    """Карточка одного элемента курации: тип, текст, длина/лимит, кампания/группа."""
    from adcopy.validate import LIMITS

    name = "Заголовок" if kind == "h" else "Описание"
    limit = LIMITS["headline" if kind == "h" else "description"]
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


def fmt_rsa_overview(h_appr: int, d_appr: int, h_total: int, d_total: int) -> str:
    """Итоговый экран курации: сколько одобрено из скольких, готовность к созданию."""
    ready = "✅ можно создавать" if (h_appr >= 3 and d_appr >= 2) else "нужно ≥3 загол. и ≥2 опис."
    return (
        "📋 <b>Итог курации RSA</b>\n"
        f"Заголовки одобрены: <b>{h_appr}</b>/{h_total}\n"
        f"Описания одобрены: <b>{d_appr}</b>/{d_total}\n\n"
        f"{ready}"
    )


def fmt_rsa_proposal_summary(
    ad_group: str, headlines: list[str], descriptions: list[str], final_url: str
) -> str:
    """Плейн-текст сводка create_rsa для confirm-гейта (esc применяется при показе)."""
    h_lines = "\n".join(f"  • {h}" for h in headlines)
    d_lines = "\n".join(f"  • {d}" for d in descriptions)
    return (
        f"Создать объявление (RSA) в группе «{ad_group}» — на паузе.\n"
        f"Ссылка: {final_url}\n\n"
        f"Заголовки ({len(headlines)}):\n{h_lines}\n\n"
        f"Описания ({len(descriptions)}):\n{d_lines}"
    )


def fmt_gdn_proposal_summary(
    name: str,
    url: str,
    budget_units: float,
    headlines: list[str],
    descriptions: list[str],
    business_name: str,
) -> str:
    """Плейн-текст сводка create_gdn_campaign для confirm-гейта (esc применяется при показе)."""
    h_lines = "\n".join(f"  • {h}" for h in headlines)
    d_lines = "\n".join(f"  • {d}" for d in descriptions)
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


def match_type_human(mt: str) -> str:
    """broad/phrase/exact → человекочитаемый тип соответствия (RU)."""
    return {"broad": "широкое", "phrase": "фразовое", "exact": "точное"}.get(
        str(mt).lower(), str(mt)
    )


def keyword_action_label(operation: str) -> str:
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


def _money_summary(label: str, params: dict) -> str:
    c = params.get("campaign", "")
    mode = params.get("mode")
    cur = _CURRENCY_HUMAN.get(str(params.get("currency", "")), str(params.get("currency", "")))
    try:
        v = f"{float(params.get('value')):g}"
    except (TypeError, ValueError):
        v = str(params.get("value"))
    # §5: реальное «было → станет», если снимок прочитан (числа — в валюте аккаунта).
    b = _before(params)
    if b and b.get("kind") == "budget" and b.get("before_micros") is not None:
        before_s, after_s = _fmt_micros(b["before_micros"]), _fmt_micros(b.get("after_micros"))
        if mode == "increase_by_percent":
            tail = f" (+{v}%)"
        elif mode == "increase_by_amount":
            tail = f" (+{v} {cur})".rstrip()
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


def _bid_summary(params: dict) -> str:
    c = params.get("campaign", "")
    mode = params.get("mode")
    try:
        v = f"{float(params.get('value')):g}"
    except (TypeError, ValueError):
        v = str(params.get("value"))
    b = _before(params)
    if b and b.get("kind") == "bid" and b.get("before_micros"):
        n = b.get("n_groups") or len(b["before_micros"])
        rng_b, rng_a = _micros_range(b["before_micros"]), _micros_range(b.get("after_micros") or [])
        if mode == "increase_by_percent":
            return f"Кампания «{c}» — ставка CPC: +{v}% (текущие {rng_b} → {rng_a}; групп: {n})"
        return f"Кампания «{c}» — ставка CPC: {rng_b} → {rng_a} (групп: {n})"
    return _money_summary("ставка CPC", params)


def fmt_mutation_summary(operation: str, params: dict) -> str:
    """Человекочитаемая сводка черновика «было → станет»/действия (plain text; esc — при показе).

    Для ключей — тип соответствия словами + список (усечён до KW_INLINE_MAX; полный — во вложении).
    Возвращает '' для операций со своим богатым форматтером (create_rsa/create_gdn_campaign) — тогда
    вызывающий оставляет собственный summary. Заменяет «сырой dict» из confirm.gate.build_summary."""
    if not isinstance(params, dict):
        return ""
    c = params.get("campaign", "")
    if operation == "update_budget":
        return _money_summary("бюджет", params)
    if operation == "update_bid":
        return _bid_summary(params)
    if operation in ("pause_campaign", "resume_campaign"):
        # §5: показываем текущий статус → новый, если снимок прочитан.
        b = _before(params)
        new = "на паузе ⏸" if operation == "pause_campaign" else "включена ▶️"
        if b and b.get("kind") == "status" and b.get("before_status"):
            return f"Кампания «{c}»: {status_human(b['before_status'])} → {new}"
        verb = "поставить на паузу" if operation == "pause_campaign" else "возобновить"
        return f"Кампания «{c}» — {verb}."
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
        mt = match_type_human(params.get("match_type", ""))
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
    return ""  # create_rsa / create_gdn_campaign / неизвестное — оставить summary вызывающего


def fmt_keywords_summary(clusters, by_text: dict, total: int, src: str) -> str:
    """Сводка keyword research: топ-кластеры с топ-ключами и объёмами. Полная таблица — в .xlsx.

    clusters — объекты с .name/.intent/.keywords (duck-typed); by_text — {ключ: объём/мес}.
    Усечение (кластеров/ключей) помечается явно, без «тихого» обрезания."""
    max_clusters, max_kw = 8, 6
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
def fmt_stats(account: str, days: int, st: dict, currency: str = "") -> str:
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
    return (
        f"📊 <b>Аккаунт …{esc(str(account)[-4:])}</b> · {days} дн.\n\n"
        f"Показы:      <b>{_thou(imp)}</b>\n"
        f"Клики:       <b>{_thou(clk)}</b>  (CTR {ctr:.2f}%)\n"
        f"Расход:      <b>{_thou(cost, 2)}{cur}</b>\n"
        f"Ср. CPC:     <b>{_thou(cpc, 2)}{cur}</b>\n"
        f"Конверсии:   <b>{conv:g}</b>\n"
        f"Ценность:    <b>{_thou(cval, 2)}{cur}</b>"
    )


def _usd(n: float, dec: int = 2) -> str:
    """Денежная строка в долларах OpenRouter-кредитов: $12.34 / $0.0042 (без CJK-ширины)."""
    return f"${_thou(n, dec)}"


def fmt_balance(acct, snap: dict) -> str:
    """Бюджет LLM: баланс/траты OpenRouter (источник истины, переживает рестарты) + живая
    разбивка ТЕКУЩЕГО процесса по ролям «с запуска». acct — openrouter_account.AccountStatus;
    snap — core.usage.snapshot(). Кредит OpenRouter = USD, поэтому показываем в $."""
    L = ["💳 <b>Бюджет LLM · OpenRouter</b>", ""]

    bal = acct.balance
    if bal is not None:
        L.append(f"Остаток:     <b>{_usd(bal)}</b>")
    if acct.total_usage is not None:
        L.append(f"Потрачено:   <b>{_usd(acct.total_usage)}</b> (всего по аккаунту)")
    if bal is None and acct.key_usage is not None:
        # /credits недоступен (нужен management-ключ) — показываем траты по самому ключу.
        L.append(f"Потрачено ключом: <b>{_usd(acct.key_usage, 4)}</b>")
    if acct.limit_remaining is not None:
        L.append(f"Лимит ключа: <b>{_usd(acct.limit_remaining)}</b> остаток")
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
        L.append(f"Стоимость: <b>{_usd(total.cost, 4)}</b>")
        names = {"parsing": "парсинг", "copy": "копирайт", "fallback": "резерв"}
        for role, u in roles.items():
            if u.calls:
                L.append(f"  • {esc(names.get(role, role))}: {u.calls}× · {_usd(u.cost, 4)}")
    else:
        L += ["", "<i>С запуска вызовов модели ещё не было.</i>"]

    return "\n".join(L)


def campaigns_title(account: str) -> str:
    return f"📋 <b>Кампании аккаунта …{esc(str(account)[-4:])}</b>\nВыбери кампанию:"


def fmt_campaign_header(c: dict) -> str:
    return (
        f"📋 <b>{esc(c['name'])}</b>\n"
        f"Статус: {status_human(c.get('status', ''))}\n\n"
        "Выбери действие:"
    )
