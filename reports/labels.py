"""Ярлыки экспортов RU/EN (§9 + RU/EN-требование ТЗ) — ОДИН словарь на .xlsx и Google Sheets.

Интерфейс бота двуязычный, а выгрузки были русскими всегда: EN-менеджер получал файл с колонками
«Показы/Клики/Расход» и листами «Сводка»/«Кампании». docs/ACCEPTANCE.md при этом утверждал, что
RU-утечка закрыта — она была закрыта только в сообщениях, не в артефактах.

Перевод — по ТОЧНОЙ строке (loc): ярлыки порождает код (queries/xlsx/sheets), пользовательских
данных здесь нет, поэтому словарь полон по построению, а гард `tests/test_export_i18n.py` требует,
чтобы каждый ярлык из кода имел EN-пару. Значения ячеек (имена кампаний, ENABLED, MOBILE) не
переводим — это данные Google, а не наши подписи.
"""

from __future__ import annotations

# Колонки метрик (единый порядок для xlsx, Sheets и текстовой сводки).
METRIC_HEADERS = [
    "Показы",
    "Клики",
    "CTR",
    "Сред. CPC",
    "Расход",
    "Конверсии",
    "Ценность",
    "CPA",
    "ROAS",
]
METRIC_HEADERS_EN = [
    "Impressions",
    "Clicks",
    "CTR",
    "Avg. CPC",
    "Cost",
    "Conversions",
    "Conv. value",
    "CPA",
    "ROAS",
]
# Денежные колонки — ПО ПОЗИЦИИ (Сред. CPC, Расход, Ценность, CPA), чтобы код валюты приклеивался
# независимо от языка шапки. ROAS — отношение, безразмерно → без валюты.
_MONEY_IDX = frozenset({3, 4, 6, 7})


def metric_headers(currency: str = "", lang: str = "ru") -> list[str]:
    """Шапка метрик на языке lang + код валюты на денежных колонках (§9): «Расход» → «Расход, USD».
    Пустой currency → без суффикса (обратная совместимость: == METRIC_HEADERS)."""
    base = METRIC_HEADERS_EN if lang == "en" else METRIC_HEADERS
    if not currency:
        return list(base)
    return [f"{h}, {currency}" if i in _MONEY_IDX else h for i, h in enumerate(base)]


# Структурные ярлыки экспортов: заголовки листов, колонки-измерения, типы строк «Пропущено/ошибки».
_EN: dict[str, str] = {
    # листы
    "Сводка": "Summary",
    "Сводка MCC": "MCC summary",
    "Аккаунты": "Accounts",
    "Пропущено и ошибки": "Skipped and errors",
    # заголовки разбивок (reports.queries)
    "Кампании": "Campaigns",
    "Группы объявлений": "Ad groups",
    "Ключевые слова": "Keywords",
    "Объявления": "Ads",
    "Устройства": "Devices",
    "Сети": "Networks",
    "По дням": "By day",
    # колонки-измерения
    "Кампания": "Campaign",
    "Группа": "Ad group",
    "Статус": "Status",
    "Ключ": "Keyword",
    "Тип": "Type",
    "ID объявления": "Ad ID",
    "Заголовки": "Headlines",
    "Ссылка": "Final URL",
    "Устройство": "Device",
    "Сеть": "Network",
    "Дата": "Date",
    "Период": "Period",
    # MCC-книги
    "Валюта": "Currency",
    "Аккаунтов": "Accounts",
    "ID": "ID",
    "Аккаунт": "Account",
    "Кампании (статус)": "Campaigns (status)",
    "Причина": "Reason",
    # лист «Находки» (reports.findings): аудит по выгружаемому отчёту
    "Находки": "Findings",
    "Важность": "Severity",
    "Семья": "Family",
    "Под риском": "At risk",
    "Что не так": "Issue",
    "Проверка": "Check",
    # вкладки выгрузки /audit (reports.findings): обзор + свод по семьям
    "Обзор": "Overview",
    "По семьям": "By family",
    "Находок": "Findings",  # шапка колонки-счётчика свода семей (та же EN, что у листа находок)
    "Штраф": "Penalty",
    # секция ДАННЫХ обогащённой выгрузки /audit (слой сбора прицепляет сырьё сверх report.breakdowns —
    # запросы/QS/гео). Enum-значения Google (ABOVE_AVERAGE…) не переводим — данные, не подписи.
    "Поисковые запросы": "Search terms",
    "Показатель качества": "Quality score",
    "География": "Geo",
    "Запрос": "Search term",
    "QS": "QS",
    "Ожид. CTR": "Exp. CTR",
    "Релевантность": "Ad relevance",
    "Целевая стр.": "Landing page",
    "Регион": "Region",
    # MCC-книга: колонки здоровья считает движок по УЖЕ собранным отчётам (0 доп. чтений) — сноска
    # честно говорит, что это НЕ полный аудит (ctx-сигналов нет ⇒ балл может быть завышен).
    "Балл": "Score",
    "Оценка": "Grade",
    "Балл — по данным отчёта (без полного аудита); полный разбор — /audit": (
        "Score is based on report data only (not a full audit); run /audit for the full breakdown"
    ),
    # типы строк листа «Пропущено и ошибки»
    "неактивный (не ENABLED)": "inactive (not ENABLED)",
    "пропущен (нет доступа на чтение)": "skipped (no read access)",
    "менеджерский (без собственных метрик)": "manager (no own metrics)",
    "ошибка чтения": "read error",
}

# Пометка об усечении (Breakdown.note_kind → человекочитаемое «чего именно»).
_NOTE_UNITS = {
    "keyword": ("ключей", "keywords"),
    "ad": ("объявлений", "ads"),
}


def loc(s: str, lang: str = "ru") -> str:
    """Перевести ярлык экспорта. Неизвестная строка возвращается как есть (данные не трогаем)."""
    return _EN.get(s, s) if lang == "en" else s


def loc_all(items, lang: str = "ru") -> list[str]:
    return [loc(s, lang) for s in items]


def top_note(kind: str, n: int, lang: str = "ru") -> str:
    """Пометка «показан топ-N по расходу» (§9: без тихих обрезаний) на языке lang."""
    ru, en = _NOTE_UNITS.get(kind, (kind, kind))
    return f"top {n} {en} by cost" if lang == "en" else f"показаны топ-{n} {ru} по расходу"


def account_report_title(customer_id: str, lang: str = "ru") -> str:
    return f"Account report {customer_id}" if lang == "en" else f"Отчёт по аккаунту {customer_id}"


def mcc_summary_title(manager_id: str, lang: str = "ru") -> str:
    return f"MCC summary {manager_id}" if lang == "en" else f"Сводка по MCC {manager_id}"


def mcc_deep_title(manager_id: str, lang: str = "ru") -> str:
    return (
        f"Deep MCC report {manager_id}" if lang == "en" else f"Глубокий отчёт по MCC {manager_id}"
    )


def period_line(period, lang: str = "ru") -> str:
    """«Период: последние 7 дней (2026-06-18 — 2026-06-24)» / EN — подпись периода локализована
    тем же label_i18n, что и в сообщениях бота (одна правда о названии периода)."""
    from reports.period import label_i18n

    head = "Period" if lang == "en" else "Период"
    return (
        f"{head}: {label_i18n(period, lang)} "
        f"({period.date_from.isoformat()} — {period.date_to.isoformat()})"
    )


def currency_line(currency: str, lang: str = "ru") -> str:
    return f"Currency: {currency}" if lang == "en" else f"Валюта: {currency}"
