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

# Заголовок листа/вкладки (loc → «Findings» на EN).
FINDINGS_TITLE = "Находки"

# Ярлыки важности: код Google-style (critical/warning/info) читается в таблице хуже, чем слово.
_SEVERITY = {
    "ru": {"critical": "🛑 Критично", "warning": "❗ Важно", "info": "🟡 К сведению"},
    "en": {"critical": "🛑 Critical", "warning": "❗ Warning", "info": "🟡 Info"},
}

# Колонки листа. «#» — ранг (находки уже отсортированы worst-first: строка 1 листа = №1 карточки).
_COLUMNS = ["#", "Важность", "Семья", "Кампания", "Под риском", "Что не так", "Проверка"]
MONEY_COL = 4  # 0-based индекс «Под риском» — единственная числовая колонка листа
MONEY_FORMAT = "#,##0.00"  # тот же формат, что у «Расход» в reports.queries.METRIC_FORMATS
# (колонка, формат) — для xlsx и для Sheets (SheetTab.formats). Реестр один, как METRIC_FORMATS.
FINDINGS_FORMATS: list[tuple[int, str]] = [(MONEY_COL, MONEY_FORMAT)]


def findings_headers(currency: str = "", lang: str = "ru") -> list[str]:
    """Шапка листа: код валюты на денежной колонке (§9, как metric_headers)."""
    out = [loc(h, lang) for h in _COLUMNS]
    if currency:
        out[MONEY_COL] = f"{out[MONEY_COL]}, {currency}"
    return out


def findings_meta_rows(result, lang: str = "ru") -> list[list]:
    """Шапка-обзор листа: карточка аудита БЕЗ топ-3/дисклеймера (действия — не задача бумаги),
    по строке на строку. Прозу не переписываем — берём ту же, что у /audit."""
    from audit.render import render_audit

    text = render_audit(result, lang, actions=False)
    return [[line] if line else [] for line in text.split("\n")]


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
                finding_text(f, lang, cur),
                f.check_id,
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
