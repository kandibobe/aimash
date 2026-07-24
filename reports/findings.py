"""Лист «Находки» выгрузок: AuditResult → строки таблицы. ОДИН реестр на .xlsx и Google Sheets.

Чистый модуль (без openpyxl/сети/bot) — как reports.queries.METRIC_FORMATS: раскладку строит здесь,
а xlsx/sheets только раскладывают её по своему API. Так проза находки существует в ОДНОМ месте
(`audit.render.finding_text` — тот же текст, что видит клиент в карточке /audit и в совете), и лист
не может разойтись с карточкой.

Шапка листа — целиком `render_audit(..., actions=False)`: баннеры «почини измерение» / «аккаунт
приостановлен» / «неполные данные — балл завышен» приезжают бесплатно и без регресса честности.

⚠️ Ни колонки с ОПЕРАЦИЕЙ находки, ни кнопки «применить» здесь нет и не будет: экспорт — бумага.
Любое изменение аккаунта идёт прямой командой через confirm-гейт (золотое правило №3), а не «кнопкой
из Excel». Гард — tests/test_export_findings.py::test_findings_sheet_is_paper_not_a_button.
"""

from __future__ import annotations

from reports.labels import loc

# Заголовки вкладок выгрузки /audit (loc → EN). Один источник — чтобы вкладка и её ярлык не разошлись.
OVERVIEW_TITLE = "Обзор"
FAMILY_SUMMARY_TITLE = "По семьям"
# Заголовок листа/вкладки (loc → «Findings» на EN).
FINDINGS_TITLE = "Находки"

# Ярлыки важности: код Google-style (critical/warning/info) читается в таблице хуже, чем слово.
_SEVERITY = {
    "ru": {"critical": "🛑 Критично", "warning": "❗ Важно", "info": "🟡 К сведению"},
    "en": {"critical": "🛑 Critical", "warning": "❗ Warning", "info": "🟡 Info"},
}

# Колонки листа. «#» — ранг (находки уже отсортированы worst-first: строка 1 листа = №1 карточки).
# Колонки фактов (Расход…Ключ/запрос) — замечание 5 (2026-07-17): вся конкретика находки сидела в
# одном текстовом столбце «Что не так» — в таблице по ней нельзя ни сортировать, ни фильтровать.
# Ключи берём из Finding.facts как есть (их считает КОД в audit.engine); нет факта → пустая ячейка.
_COLUMNS = [
    "#",
    "Важность",
    "Семья",
    "Кампания",
    "Под риском",
    "Расход",
    "Клики",
    "CPA",
    "Тип соответствия",
    "Регион",
    "Ключ/запрос",
    "Что не так",
    "Проверка",
]
MONEY_COL = 4  # 0-based индекс «Под риском»
COST_COL, CLICKS_COL, CPA_COL = 5, 6, 7  # числовые колонки фактов
MONEY_FORMAT = "#,##0.00"  # тот же формат, что у «Расход» в reports.queries.METRIC_FORMATS
INT_FORMAT = "#,##0"  # «Клики» — целые, как в METRIC_FORMATS
# (колонка, формат) — для xlsx и для Sheets (SheetTab.formats). Реестр один, как METRIC_FORMATS.
FINDINGS_FORMATS: list[tuple[int, str]] = [
    (MONEY_COL, MONEY_FORMAT),
    (COST_COL, MONEY_FORMAT),
    (CLICKS_COL, INT_FORMAT),
    (CPA_COL, MONEY_FORMAT),
]
_CURRENCY_COLS = (MONEY_COL, COST_COL, CPA_COL)  # куда клеить код валюты (§9)


def findings_headers(currency: str = "", lang: str = "ru") -> list[str]:
    """Шапка листа: код валюты на денежных колонках (§9, как metric_headers)."""
    out = [loc(h, lang) for h in _COLUMNS]
    if currency:
        for c in _CURRENCY_COLS:
            out[c] = f"{out[c]}, {currency}"
    return out


def findings_meta_rows(result, lang: str = "ru") -> list[list]:
    """Шапка-обзор листа: карточка аудита БЕЗ топ-3/дисклеймера (действия — не задача бумаги),
    по строке на строку. Прозу не переписываем — берём ту же, что у /audit. all_quick_wins=True:
    бумага — инвентарь, а не клавиатура: «⚡ Быстрые победы» здесь перечисляют ВСЕ такие находки,
    а не срез первых 8, под которые бот рисует кнопки (замечание 5, 2026-07-17)."""
    from audit.render import render_audit

    text = render_audit(result, lang, actions=False, all_quick_wins=True)
    return [[line] if line else [] for line in text.split("\n")]


def _fact_num(v, *, nd: int = 2):
    """Числовой факт находки → ячейка: нет/не число → пусто (нет данных ≠ ноль)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    return int(x) if nd == 0 else round(x, nd)


def findings_rows(result, lang: str = "ru") -> list[list]:
    """Строки находок (без шапки), worst-first — весь список, а не топ-8 карточки: в этом и смысл
    выгрузки. at_risk == 0 → пустая ячейка, а НЕ 0.00: находка неденежная, а не «денег ноль»."""
    from audit.render import family_label, finding_text

    cur = getattr(result, "currency", "") or ""
    sev = _SEVERITY["en" if lang == "en" else "ru"]
    rows: list[list] = []
    for i, f in enumerate(getattr(result, "findings", []) or [], 1):
        facts = getattr(f, "facts", None) or {}
        rows.append(
            [
                i,
                sev.get(f.severity, f.severity),
                family_label(f.family, lang),
                f.target_campaign or facts.get("campaign", "") or "",
                round(float(f.at_risk), 2) if f.at_risk > 0 else "",
                _fact_num(facts.get("cost")),
                _fact_num(facts.get("clicks"), nd=0),
                _fact_num(facts.get("cpa")),
                str(facts.get("match_type", "") or ""),
                str(facts.get("region", "") or ""),
                # Кандидат в минус-слова: у находки лежит РОВНО ОДНО из полей (ключ ИЛИ запрос).
                str(facts.get("keyword") or facts.get("search_term") or facts.get("term") or ""),
                finding_text(f, lang, cur),
                f.check_id,
            ]
        )
    return rows


# ── Вкладка «По семьям»: свод из AuditResult.families (уже посчитан движком, без доп-чтений) ──
# Колонки: [Семья, Находок, Под риском(валюта), Штраф]. Штраф — вклад семьи в −score (thresholds).
_FAMILY_COLUMNS = ["Семья", "Находок", "Под риском", "Штраф"]
FAMILY_MONEY_COL = 2  # 0-based индекс «Под риском» — единственная денежная колонка свода
FAMILY_SUMMARY_FORMATS: list[tuple[int, str]] = [(FAMILY_MONEY_COL, MONEY_FORMAT)]


def family_summary_headers(currency: str = "", lang: str = "ru") -> list[str]:
    """Шапка свода семей: код валюты на денежной колонке (как findings_headers)."""
    out = [loc(h, lang) for h in _FAMILY_COLUMNS]
    if currency:
        out[FAMILY_MONEY_COL] = f"{out[FAMILY_MONEY_COL]}, {currency}"
    return out


def family_summary_rows(result, lang: str = "ru") -> list[list]:
    """Свод по семьям из уже вычисленного result.families, сортировка worst-first (по −штрафу).
    at_risk == 0 → пустая ячейка (неденежная семья), а НЕ 0.00 — как в findings_rows."""
    from audit.render import family_label

    families = getattr(result, "families", None) or {}
    rows: list[list] = []
    for fam, data in sorted(families.items(), key=lambda kv: -float(kv[1].get("penalty", 0.0))):
        at_risk = float(data.get("at_risk", 0.0) or 0.0)
        rows.append(
            [
                family_label(fam, lang),
                int(data.get("count", 0)),
                round(at_risk, 2) if at_risk > 0 else "",
                round(float(data.get("penalty", 0.0) or 0.0), 2),
            ]
        )
    return rows


def account_health_cells(report) -> list:
    """Engine-only здоровье аккаунта для строки MCC-книги: [балл, грейд, под риском] по УЖЕ собранному
    отчёту — НИ ОДНОГО доп. чтения Google Ads (полный аудит веером по MCC — это 23 чтения × N
    аккаунтов, отвергнуто по квоте). Сбой движка не роняет книгу: вернём пустые ячейки.

    Балл здесь СЛАБЕЕ, чем у /audit: ctx-сигналов нет, чеки семей молчат ⇒ он может быть завышен.
    Поэтому книга подписывает колонки сноской «оценка по данным отчёта — полный разбор в /audit»."""
    try:
        from audit.engine import build_audit

        r = build_audit(report)
        if not r.has_activity or r.score is None:
            return ["", "", ""]
        return [r.score, r.grade, round(float(r.at_risk), 2) if r.at_risk > 0 else ""]
    except Exception:  # noqa: BLE001 — здоровье необязательно, отчёт важнее
        from core.logging import log

        log.warning("mcc-deep: build_audit упал — строка аккаунта уйдёт без балла")
        return ["", "", ""]
