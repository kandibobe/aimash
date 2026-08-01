"""Рендер «было → станет»: черновик мутации словами. Без Telegram, без БД, без google-ads.

Один и тот же текст человек обязан увидеть в ЛЮБОМ контуре подтверждения — и в Telegram-карточке
(`core.texts`), и в headless-подтверждении (`confirm.gate.build_summary`, дальше MCP-WRITE). Пока
рендер жил внутри `bot/texts.py`, headless-путь показывал на подтверждение сырой dict: человек
подтверждал не то, что читал. Отсюда два правила модуля:

* никаких импортов `bot` / `aiogram` / `sqlalchemy` — чистота приколочена тестом
  `tests/test_confirm_render.py::test_render_is_headless`;
* `lang` — ОБЯЗАТЕЛЬНЫЙ аргумент, а не contextvar: в контуре без Telegram-запроса язык брать
  неоткуда, а молчаливый «ru» на английской карточке — тихий дефект. Разрешение языка из
  contextvar осталось в тонкой обёртке `core.texts` (там же, где живёт `core.i18n`).

Текст plain: HTML-экранирование — на вызывающем (`core.texts.esc` при показе).
"""

from __future__ import annotations

from confirm.consequences import MONTH_DAYS, PROJECTION_DAYS
from core.limits import ZERO_DECIMAL_CURRENCIES


def _thou(n: float, dec: int = 0) -> str:
    """Число с пробелом-разделителем тысяч: 12480 -> '12 480', 4512.3 -> '4 512.30'."""
    return f"{n:,.{dec}f}".replace(",", " ")


def status_human(status: str, lang: str) -> str:
    if lang == "en":
        return {"ENABLED": "enabled ▶️", "PAUSED": "paused ⏸", "REMOVED": "removed 🗑"}.get(
            status, status
        )
    return {"ENABLED": "включена ▶️", "PAUSED": "на паузе ⏸", "REMOVED": "удалена 🗑"}.get(
        status, status
    )


KW_INLINE_MAX = 20  # ключей показываем в сводке черновика; больше — во вложении .xlsx


def _kw_tail(kws: object, lang: str, *, promised: bool) -> str:
    """Хвост усечённого списка ключей: «…ещё N» и — ТОЛЬКО при promised — обещание вложения.

    Обещание печатается не по факту «список длинный», а по факту «канал доставки есть и решение
    принято» (`confirm.attachment.plan_attachment`). Дефолт `promised=False` — fail-closed: не
    обещаем того, чего не отправим. Раньше эти четыре хвоста были выписаны в четырёх местах и
    обещали безусловно; отсюда и брались обещания без файла (add_negatives_to_shared_set)."""
    more = len(kws or []) - KW_INLINE_MAX  # type: ignore[arg-type]
    if more <= 0:
        return ""
    if lang == "en":
        return f"\n  …{more} more" + (" — full list in the .xlsx attachment" if promised else "")
    return f"\n  …ещё {more}" + (" — полный список во вложении .xlsx" if promised else "")


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


def match_type_human(mt: str, lang: str) -> str:
    """broad/phrase/exact → человекочитаемый тип соответствия."""
    if lang == "en":
        return {"broad": "broad", "phrase": "phrase", "exact": "exact"}.get(
            str(mt).lower(), str(mt)
        )
    return {"broad": "широкое", "phrase": "фразовое", "exact": "точное"}.get(
        str(mt).lower(), str(mt)
    )


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


def _plain_value(value: object) -> str:
    """Compact deterministic rendering for nested optional campaign settings."""

    if isinstance(value, dict):
        return ", ".join(f"{key}={_plain_value(item)}" for key, item in value.items()) or "—"
    if isinstance(value, list):
        return ", ".join(_plain_value(item) for item in value) or "—"
    if value is None or value == "":
        return "—"
    return str(value)


def _text_block(label: str, values: list[object]) -> str:
    return f"{label} ({len(values)}):\n" + "\n".join(f"  • {value}" for value in values)


def _create_rsa_summary(params: dict, lang: str) -> str:
    en = lang == "en"
    campaign = str(params.get("campaign") or "—")
    group = str(params.get("ad_group_id") or "—")
    url = str(params.get("final_url") or "—")
    path = "/".join(str(params.get(key) or "").strip("/") for key in ("path1", "path2"))
    path = path.strip("/")
    headlines = list(params.get("headlines") or [])
    descriptions = list(params.get("descriptions") or [])
    if en:
        lines = [
            f"Create an RSA in campaign “{campaign}”, ad group {group} — paused.",
            f"Final URL: {url}" + (f" · path: {path}" if path else ""),
            "",
            _text_block("Headlines", headlines),
            "",
            _text_block("Descriptions", descriptions),
        ]
    else:
        lines = [
            f"Создать RSA в кампании «{campaign}», группа {group} — на паузе.",
            f"Final URL: {url}" + (f" · path: {path}" if path else ""),
            "",
            _text_block("Заголовки", headlines),
            "",
            _text_block("Описания", descriptions),
        ]
    return "\n".join(lines)


def _create_search_summary(params: dict, lang: str, *, attachment: bool) -> str:
    en = lang == "en"
    name = str(params.get("campaign_name") or "—")
    url = str(params.get("final_url") or "—")
    currency = str(params.get("_account_currency") or "").upper()
    cur = f" {currency}" if currency else ""
    budget = _fmt_micros(params.get("budget_daily_micros"), currency)
    cpc_raw = params.get("cpc_bid_micros")
    cpc = _fmt_micros(cpc_raw, currency) + cur if cpc_raw is not None else "default by account"
    if not en and cpc_raw is None:
        cpc = "по умолчанию для аккаунта"
    geo = ", ".join(str(item) for item in (params.get("geo_locations") or []))
    geo = geo or ("all locations" if en else "все локации")
    languages = ", ".join(str(item) for item in (params.get("languages") or [])) or "—"
    networks = str(params.get("networks") or "search")
    schedule = _plain_value(params.get("ad_schedule_blocks") or [])
    dates = f"{params.get('start_date') or ('today' if en else 'сегодня')} — {params.get('end_date') or ('no end date' if en else 'без даты конца')}"
    headlines = list(params.get("headlines") or [])
    descriptions = list(params.get("descriptions") or [])
    keywords = list(params.get("keywords") or [])
    keyword_types = list(params.get("keyword_match_types") or [])
    default_mt = str(params.get("match_type") or "phrase")
    shown_keywords = []
    for index, keyword in enumerate(keywords[:KW_INLINE_MAX]):
        mt = keyword_types[index] if index < len(keyword_types) else default_mt
        shown_keywords.append(f"[{match_type_human(str(mt), lang)}] {keyword}")
    kw_block = _text_block("Keywords" if en else "Ключевые слова", shown_keywords)
    kw_block += _kw_tail(keywords, lang, promised=attachment)
    path = "/".join(str(params.get(key) or "").strip("/") for key in ("path1", "path2"))
    path = path.strip("/") or "—"
    assets = list(params.get("asset_specs") or [])
    reuse = list(params.get("existing_asset_links") or [])
    images = list(params.get("image_media_ids") or [])
    if en:
        lines = [
            f"Create a search campaign “{name}” — paused.",
            f"Final URL: {url} · path: {path}",
            f"Daily budget: {budget}{cur} · Max CPC: {cpc}",
            f"Geo: {geo} · Languages: {languages}",
            f"Networks: {networks} · Schedule: {schedule} · Dates: {dates}",
            f"Bidding: {_plain_value(params.get('bidding'))}",
            f"URL options: {_plain_value(params.get('url_options'))}",
            "",
            _text_block("Headlines", headlines),
            "",
            _text_block("Descriptions", descriptions),
            "",
            kw_block,
            "",
            f"New assets ({len(assets)}): {_plain_value(assets)}",
            f"Reused assets ({len(reuse)}): {_plain_value(reuse)}",
            f"Images: {len(images)}",
        ]
    else:
        lines = [
            f"Создать поисковую кампанию «{name}» — на паузе.",
            f"Final URL: {url} · path: {path}",
            f"Дневной бюджет: {budget}{cur} · Max CPC: {cpc}",
            f"ГЕО: {geo} · Языки: {languages}",
            f"Сети: {networks} · Расписание: {schedule} · Даты: {dates}",
            f"Стратегия: {_plain_value(params.get('bidding'))}",
            f"URL-опции: {_plain_value(params.get('url_options'))}",
            "",
            _text_block("Заголовки", headlines),
            "",
            _text_block("Описания", descriptions),
            "",
            kw_block,
            "",
            f"Новые ассеты ({len(assets)}): {_plain_value(assets)}",
            f"Переиспользуемые ассеты ({len(reuse)}): {_plain_value(reuse)}",
            f"Изображения: {len(images)}",
        ]
    return "\n".join(lines)


def _create_media_campaign_summary(operation: str, params: dict, lang: str) -> str:
    en = lang == "en"
    kind = {
        "create_gdn_campaign": "display campaign (GDN)" if en else "медийную кампанию (GDN)",
        "create_demand_gen_campaign": "Demand Gen campaign" if en else "Demand Gen кампанию",
        "create_video_campaign": "video campaign" if en else "видеокампанию",
    }[operation]
    currency = str(params.get("_account_currency") or "").upper()
    cur = f" {currency}" if currency else ""
    budget = _fmt_micros(params.get("budget_daily_micros"), currency)
    geo = ", ".join(str(item) for item in (params.get("geo_locations") or []))
    geo = geo or ("all locations" if en else "все локации")
    headlines = list(params.get("headlines") or [])
    long_headline = str(params.get("long_headline") or "—")
    descriptions = list(params.get("descriptions") or [])
    lines = [
        (
            f"Create a {kind} “{params.get('campaign_name') or '—'}” — paused."
            if en
            else f"Создать {kind} «{params.get('campaign_name') or '—'}» — на паузе."
        ),
        ("Business" if en else "Бизнес") + f": {params.get('business_name') or '—'}",
        f"Final URL: {params.get('final_url') or '—'}",
    ]
    if operation != "create_gdn_campaign":
        lines.append(f"YouTube: {params.get('youtube_video_id') or '—'}")
    lines.extend(
        [
            ("Daily budget" if en else "Дневной бюджет") + f": {budget}{cur}",
            ("Geo" if en else "ГЕО") + f": {geo}",
        ]
    )
    if operation == "create_gdn_campaign":
        lines.append("Image: 1" if en else "Изображение: 1")
    if operation == "create_demand_gen_campaign":
        lines.append(("Goal" if en else "Цель") + f": {params.get('goal') or 'clicks'}")
        has_logo = bool(params.get("logo_media_id"))
        logo_value = "yes" if has_logo and en else "есть" if has_logo else "no" if en else "нет"
        lines.append(("Logo" if en else "Логотип") + f": {logo_value}")
    lines.extend(
        [
            "",
            _text_block("Headlines" if en else "Заголовки", headlines),
            ("Long headline" if en else "Длинный заголовок") + f": {long_headline}",
            "",
            _text_block("Descriptions" if en else "Описания", descriptions),
        ]
    )
    return "\n".join(lines)


def _geo_type_human(value: str, lang: str) -> str:
    """G11: тип гео-таргетинга человеческим языком. Пустая строка (Google вернул UNSPECIFIED) —
    пустая и на выходе: «было» неизвестно, и выдумывать его нельзя."""
    en = lang == "en"
    if value == "PRESENCE":
        return "только присутствие в регионе" if not en else "presence in region only"
    if value == "PRESENCE_OR_INTEREST":
        return "присутствие ИЛИ интерес" if not en else "presence OR interest"
    if value == "SEARCH_INTEREST":
        return "интерес к региону" if not en else "search interest"
    return ""


def _geo_before_str(b: dict, en: bool) -> str:
    """D6: текущее ГЕО из снимка _before (kind='geo') одной строкой: локации + радиусы, либо
    «все регионы» (пустой таргетинг = показ везде — это НЕ ошибка)."""
    parts = list(b.get("before_locations") or []) + list(b.get("before_proximity") or [])
    if not parts:
        return "all regions" if en else "все регионы"
    return ", ".join(str(x) for x in parts)


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


def _money_summary(label: str, params: dict, lang: str) -> str:
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
    if lang == "en":
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
            base = f"Campaign “{c}” — {label}: {before_s} → {after_s}{tail}".rstrip()
            # П1: раскрываем радиус общего бюджета — пользователь видит, что изменение затронет и
            # ДРУГИЕ кампании (иначе fail-closed гард apply_update_budget не даст применить).
            sc = b.get("shared_campaigns") or []
            if sc:
                shown = ", ".join(f"“{n}”" for n in sc[:5])
                more = f" +{len(sc) - 5}" if len(sc) > 5 else ""
                base += f"\n⚠️ Shared budget — also affects: {shown}{more}"
            return base
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
        base = f"Кампания «{c}» — {label}: {before_s} → {after_s}{tail}".rstrip()
        # П1: раскрываем радиус общего бюджета — пользователь видит, что изменение затронет и ДРУГИЕ
        # кампании (иначе fail-closed гард apply_update_budget не даст применить).
        sc = b.get("shared_campaigns") or []
        if sc:
            shown = ", ".join(f"«{n}»" for n in sc[:5])
            more = f" +{len(sc) - 5}" if len(sc) > 5 else ""
            base += f"\n⚠️ Общий бюджет — затронет также: {shown}{more}"
        return base
    # fallback без «было» (чтение не удалось / старый черновик)
    if pct:
        return f"Кампания «{c}» — {label}: {sign}{v}%"
    if amt:
        return f"Кампания «{c}» — {label}: {sign}{v} {cur}".rstrip()
    if mode == "set_to":
        return f"Кампания «{c}» — {label} → {v} {cur}".rstrip()
    return f"Кампания «{c}» — {label}: {v} {cur}".rstrip()


def fmt_consequences(cons, lang: str = "ru") -> str:
    """Блок «последствия» для карточки L3 из готовых чисел `confirm.consequences.Consequences`.

    Здесь ТОЛЬКО раскладка и формат — ни одного вычисления (правило 4/15: числа считает код, и
    ровно один раз, в одном месте). Пустая строка на `None` — блок необязателен."""
    if cons is None:
        return ""
    cur = cons.currency or None
    sign = (
        "+" if cons.delta_micros >= 0 else "−"
    )  # U+2212: минус на денежной карточке обязан быть виден
    d = _fmt_micros(abs(cons.delta_micros), cur)
    dh = _fmt_micros(abs(cons.delta_horizon_micros), cur)
    pct = f" ({sign}{abs(cons.delta_pct):.0f}%)" if cons.delta_pct is not None else ""
    en = lang == "en"
    lines = [
        "📊 Последствия (линейная проекция, не прогноз аукциона):"
        if not en
        else "📊 Consequences (linear projection, not an auction forecast):",
    ]
    if en:
        lines.append(
            f"• per day: {_fmt_micros(cons.before_micros, cur)} → "
            f"{_fmt_micros(cons.after_micros, cur)} — {sign}{d}{pct}"
        )
        lines.append(f"• over {PROJECTION_DAYS} days: {sign}{dh}")
        lines.append(
            f"• monthly cap (×{MONTH_DAYS}): "
            f"{_fmt_micros(cons.month_before_micros, cur)} → "
            f"{_fmt_micros(cons.month_after_micros, cur)}"
        )
        if cons.days_to_month is not None:
            lines.append(
                f"• at the new pace the previous monthly cap is used up in "
                f"{cons.days_to_month:.0f} days instead of {MONTH_DAYS:.0f}"
            )
    else:
        lines.append(
            f"• в сутки: {_fmt_micros(cons.before_micros, cur)} → "
            f"{_fmt_micros(cons.after_micros, cur)} — {sign}{d}{pct}"
        )
        lines.append(f"• за {PROJECTION_DAYS} дней: {sign}{dh}")
        lines.append(
            f"• месячный потолок (×{MONTH_DAYS}): "
            f"{_fmt_micros(cons.month_before_micros, cur)} → "
            f"{_fmt_micros(cons.month_after_micros, cur)}"
        )
        if cons.days_to_month is not None:
            lines.append(
                f"• прежний месячный потолок при новой скорости кончится за "
                f"{cons.days_to_month:.0f} дней вместо {MONTH_DAYS:.0f}"
            )
    return "\n".join(lines)


def _bid_summary(params: dict, lang: str) -> str:
    c = params.get("campaign", "")
    mode = params.get("mode")
    sign = _mode_sign(mode)
    pct = str(mode or "").endswith("_by_percent")
    try:
        v = f"{float(params.get('value')):g}"
    except (TypeError, ValueError):
        v = str(params.get("value"))
    b = _before(params)
    if lang == "en":
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


def _keyword_bid_summary(params: dict, lang: str) -> str:
    """Ф1: карточка ставки на уровне КЛЮЧА. В отличие от групповой (_bid_summary) называет сам ключ
    и группы, в которых он найден: пользователь должен видеть, ЧТО именно подорожает."""
    c = params.get("campaign", "")
    kw = params.get("keyword", "")
    mode = params.get("mode")
    sign = _mode_sign(mode)
    pct = str(mode or "").endswith("_by_percent")
    try:
        v = f"{float(params.get('value')):g}"
    except (TypeError, ValueError):
        v = str(params.get("value"))
    b = _before(params)
    en = lang == "en"
    mt = params.get("match_type")
    mt_s = f" [{match_type_human(mt, lang)}]" if mt else ""
    if b and b.get("kind") == "keyword_bid" and b.get("before_micros"):
        acc = b.get("currency")
        n = b.get("n_keywords") or len(b["before_micros"])
        rng_b = _micros_range(b["before_micros"], acc)
        rng_a = _micros_range(b.get("after_micros") or [], acc)
        groups = [g for g in (b.get("ad_groups") or []) if g]
        where = ", ".join(groups[:3]) + ("…" if len(groups) > 3 else "")
        if en:
            head = f"Keyword “{kw}”{mt_s} — CPC bid: "
            body = f"{sign}{v}% ({rng_b} → {rng_a})" if pct else f"{rng_b} → {rng_a}"
            tail = f"\nCampaign “{c}”, ad groups ({n}): {where}" if where else f"\nCampaign “{c}”"
            return head + body + tail
        head = f"Ключ «{kw}»{mt_s} — ставка CPC: "
        body = f"{sign}{v}% ({rng_b} → {rng_a})" if pct else f"{rng_b} → {rng_a}"
        tail = f"\nКампания «{c}», группы ({n}): {where}" if where else f"\nКампания «{c}»"
        return head + body + tail
    # fallback без «было» (чтение не удалось / старый черновик)
    cur = _CURRENCY_HUMAN.get(params.get("currency") or "", "")
    change = f"{sign}{v}%" if pct else f"→ {v} {cur}".rstrip()
    if en:
        return f"Keyword “{kw}”{mt_s} (campaign “{c}”) — CPC bid: {change}"
    return f"Ключ «{kw}»{mt_s} (кампания «{c}») — ставка CPC: {change}"


def fmt_mutation_summary(
    operation: str, params: dict, lang: str, *, attachment: bool = False
) -> str:
    """Человекочитаемая сводка черновика «было → станет»/действия (plain text; esc — при показе).

    Для ключей — тип соответствия словами + список (усечён до KW_INLINE_MAX). Все поддержанные
    mutations, включая создание кампаний/RSA, имеют здесь детерминированную полную карточку.

    `attachment` — есть ли КАНАЛ доставки .xlsx у вызывающего (решение принимает
    `confirm.attachment.plan_attachment`, а не этот форматтер). Только при `True` в хвосте
    усечённого списка печатается обещание «полный список во вложении». Дефолт `False` —
    fail-closed: сводка уезжает в `summary` черновика, то есть в audit-row, из которого правило 15
    репортит «выполнено»; безусловное обещание клало туда обещание файла, которого никто не слал."""
    if not isinstance(params, dict):
        return ""
    lng = lang
    c = params.get("campaign", "")
    if lng == "en":
        return _mutation_summary_en(operation, params, c, attachment=attachment)
    if operation == "update_budget":
        return _money_summary("бюджет", params, lng)
    if operation == "update_bid":
        return _bid_summary(params, lng)
    if operation == "update_keyword_bid":
        return _keyword_bid_summary(params, lng)
    if operation == "create_rsa":
        return _create_rsa_summary(params, lng)
    if operation == "create_search_campaign":
        return _create_search_summary(params, lng, attachment=attachment)
    if operation in (
        "create_gdn_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
    ):
        return _create_media_campaign_summary(operation, params, lng)
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
    if operation == "set_campaign_display_network":
        # G12: КМС на поисковой кампании — баннеры съедают поисковый бюджет.
        after = "ВКЛ" if params.get("display_network") else "ВЫКЛ"
        b = _before(params)
        if b and b.get("kind") == "display_network":
            before = "ВКЛ" if b.get("before_display_network") else "ВЫКЛ"
            return f"Кампания «{c}»: контекстно-медийная сеть (КМС) {before} → {after}."
        return f"Кампания «{c}»: контекстно-медийная сеть (КМС) → {after}."
    if operation == "set_campaign_geo_target_type":
        # G11: «присутствие ИЛИ интерес» пускает клики людей ФИЗИЧЕСКИ вне регионов.
        after = _geo_type_human(str(params.get("geo_target_type") or ""), lng)
        b = _before(params)
        before = (
            _geo_type_human(str(b.get("before_geo_target_type") or ""), lng)
            if b and b.get("kind") == "geo_target_type"
            else ""
        )
        if before:
            return f"Кампания «{c}»: гео-таргетинг {before} → {after}."
        return f"Кампания «{c}»: гео-таргетинг → {after}."
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
        # Ф4: add_keywords может быть сужен до ОДНОЙ группы — человек обязан видеть КУДА лягут ключи
        # (без группы они идут во все группы кампании; это разные последствия, а не деталь).
        ag = str(params.get("ad_group") or "").strip()
        where = f"«{c}» → группа «{ag}»" if ag else f"«{c}»"
        head = f"Кампания {where} — {verb} {len(kws)} {what} (тип соответствия: {mt}):"
        shown = list(kws)[:KW_INLINE_MAX]
        lines = "\n".join(f"  • {k}" for k in shown) + _kw_tail(kws, lng, promised=attachment)
        return f"{head}\n{lines}"
    if operation == "add_negatives_to_shared_set":
        kws = params.get("keywords") or []
        mt = match_type_human(params.get("match_type", ""), lng)
        ss = str(params.get("shared_set") or "")
        # Честно про создание: существует ли список, узнаём только при исполнении (READ-резолв в
        # execute_confirmed) — дифф не утверждает, а предупреждает.
        head = (
            f"Общий список минус-слов «{ss}» — добавить {len(kws)} минус-слов (тип: {mt}).\n"
            "Списка с таким именем нет — создам. Список действует только на кампании, "
            "к которым привязан:"
        )
        shown = list(kws)[:KW_INLINE_MAX]
        lines = "\n".join(f"  • {k}" for k in shown) + _kw_tail(kws, lng, promised=attachment)
        return f"{head}\n{lines}"
    if operation == "attach_shared_set":
        return (
            f"Кампания «{c}» — привязать общий список минус-слов "
            f"«{params.get('shared_set', '')}»: его минус-слова начнут действовать на кампанию."
        )
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
            # D7: печатаем страну КАК ЕСТЬ (схема её заполняет из запроса/конфига). Прежний
            # фолбэк «UA» на карточке ВРАЛ: показывал Украину для номера любой страны.
            f"Кампания «{c}» — телефон-расширение: {params.get('phone_number', '')} "
            f"({params.get('country_code') or '—'})."
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
    return ""


def _mutation_summary_en(operation: str, params: dict, c: str, *, attachment: bool = False) -> str:
    """EN-ветка fmt_mutation_summary: те же operation/params/плейсхолдеры, английская формулировка.
    RU-ветка остаётся источником истины по структуре — здесь зеркально по-английски."""
    if operation == "update_budget":
        return _money_summary("budget", params, "en")
    if operation == "update_bid":
        return _bid_summary(params, "en")
    if operation == "update_keyword_bid":
        return _keyword_bid_summary(params, "en")
    if operation == "create_rsa":
        return _create_rsa_summary(params, "en")
    if operation == "create_search_campaign":
        return _create_search_summary(params, "en", attachment=attachment)
    if operation in (
        "create_gdn_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
    ):
        return _create_media_campaign_summary(operation, params, "en")
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
    if operation == "set_campaign_display_network":
        after = "ON" if params.get("display_network") else "OFF"
        b = _before(params)
        if b and b.get("kind") == "display_network":
            before = "ON" if b.get("before_display_network") else "OFF"
            return f"Campaign “{c}”: Display Network {before} → {after}."
        return f"Campaign “{c}”: Display Network → {after}."
    if operation == "set_campaign_geo_target_type":
        after = _geo_type_human(str(params.get("geo_target_type") or ""), "en")
        b = _before(params)
        before = (
            _geo_type_human(str(b.get("before_geo_target_type") or ""), "en")
            if b and b.get("kind") == "geo_target_type"
            else ""
        )
        if before:
            return f"Campaign “{c}”: geo targeting {before} → {after}."
        return f"Campaign “{c}”: geo targeting → {after}."
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
        ag = str(params.get("ad_group") or "").strip()  # Ф4: narrowed to a single ad group
        where = f"“{c}” → ad group “{ag}”" if ag else f"“{c}”"
        head = f"Campaign {where} — {verb} {len(kws)} {what} (match type: {mt}):"
        shown = list(kws)[:KW_INLINE_MAX]
        lines = "\n".join(f"  • {k}" for k in shown) + _kw_tail(kws, "en", promised=attachment)
        return f"{head}\n{lines}"
    if operation == "add_negatives_to_shared_set":
        kws = params.get("keywords") or []
        mt = match_type_human(params.get("match_type", ""), "en")
        ss = str(params.get("shared_set") or "")
        head = (
            f"Shared negative list “{ss}” — add {len(kws)} negative keywords (match type: {mt}).\n"
            "If no list with this name exists it will be created. The list only affects "
            "campaigns it is attached to:"
        )
        shown = list(kws)[:KW_INLINE_MAX]
        lines = "\n".join(f"  • {k}" for k in shown) + _kw_tail(kws, "en", promised=attachment)
        return f"{head}\n{lines}"
    if operation == "attach_shared_set":
        return (
            f"Campaign “{c}” — attach shared negative list “{params.get('shared_set', '')}”: "
            "its negative keywords will start applying to this campaign."
        )
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
            f"({params.get('country_code') or '—'})."
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
    return ""
