"""Экспорт отчёта (reports.service.ReportData) в .xlsx через openpyxl. READ-ONLY, без секретов.

Лист «Сводка» (итоги + сравнение период-к-периоду) + по листу на каждую разбивку
(кампании/группы/ключи/объявления/устройства/сети/дни). Производные метрики уже посчитаны
кодом (queries.Metrics.as_row) — здесь только раскладка и формат ячеек.
"""

from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from reports.queries import Breakdown, metric_headers
from reports.service import ReportData

_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_HEADER_FONT = Font(bold=True, color="FFFFFF")

# Формат ячеек по позиции в METRIC_HEADERS: Показы,Клики,CTR,CPC,Расход,Конв,Ценность,CPA,ROAS.
_METRIC_FORMATS = [
    "#,##0",
    "#,##0",
    "0.00%",
    "0.00",
    "#,##0.00",
    "0.00",
    "#,##0.00",
    "0.00",
    "0.00",
]


def _style_header_row(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    ws.freeze_panes = "A2"


def _autosize(ws, ncols: int, *, cap: int = 48) -> None:
    for c in range(1, ncols + 1):
        letter = get_column_letter(c)
        width = max(
            (len(str(ws.cell(row=r, column=c).value or "")) for r in range(1, ws.max_row + 1)),
            default=10,
        )
        ws.column_dimensions[letter].width = min(max(width + 2, 10), cap)


def _write_breakdown(wb: Workbook, b: Breakdown, currency: str = "") -> None:
    ws = wb.create_sheet(title=b.title[:31])
    if b.note:
        ws.append([b.note])  # пометка об усечении — первой строкой
    headers = b.dim_headers + metric_headers(currency)
    header_row = ws.max_row + 1
    ws.append(headers)
    for dims, m in b.rows:
        ws.append(list(dims) + m.as_row())
    # Если была строка-пометка, шапка не в первой строке — стилизуем шапку отдельно.
    if header_row == 1:
        _style_header_row(ws, len(headers))
    else:
        for c in range(1, len(headers) + 1):
            ws.cell(row=header_row, column=c).fill = _HEADER_FILL
            ws.cell(row=header_row, column=c).font = _HEADER_FONT
    _apply_metric_formats_at(ws, len(b.dim_headers), header_row + 1, len(b.rows))
    _autosize(ws, len(headers))


def _apply_metric_formats_at(ws, dim_count: int, first_data_row: int, ndata: int) -> None:
    for i, fmt in enumerate(_METRIC_FORMATS):
        col = dim_count + 1 + i
        for r in range(first_data_row, first_data_row + ndata):
            ws.cell(row=r, column=col).number_format = fmt


def _write_summary(ws, report: ReportData) -> None:
    p = report.period
    currency = getattr(report, "currency", "") or ""  # defensive: фейк-репорты без поля
    ws.append([f"Отчёт по аккаунту {report.customer_id}"])
    ws.append([f"Период: {p.label} ({p.date_from.isoformat()} — {p.date_to.isoformat()})"])
    if currency:
        ws.append([f"Валюта: {currency}"])  # §9: денежные метрики — в валюте аккаунта
    ws.append([])
    # Таблица итогов: шапка + строка «текущий период» (+ «предыдущий», если есть сравнение).
    headers = metric_headers(currency)
    ws.append(["Период", *headers])
    header_row = ws.max_row
    ws.append([p.label, *report.totals.as_row()])
    ndata = 1
    if report.prev_totals is not None:
        ws.append([p.previous().label, *report.prev_totals.as_row()])
        ndata = 2
    for c in range(1, len(headers) + 2):
        ws.cell(row=header_row, column=c).fill = _HEADER_FILL
        ws.cell(row=header_row, column=c).font = _HEADER_FONT
    _apply_metric_formats_at(ws, 1, header_row + 1, ndata)
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    _autosize(ws, len(headers) + 1)


def build_workbook(report: ReportData) -> Workbook:
    wb = Workbook()
    summary = wb.active
    summary.title = "Сводка"
    _write_summary(summary, report)
    currency = getattr(report, "currency", "") or ""  # defensive: фейк-репорты без поля
    for b in report.breakdowns:
        _write_breakdown(wb, b, currency)
    return wb


def write_report_xlsx(report: ReportData, path: str) -> str:
    """Сохранить отчёт в .xlsx по пути path. Возвращает path."""
    build_workbook(report).save(path)
    return path


# ── MCC-сводка (§8): подытоги по валюте + лист аккаунтов + лист пропусков/ошибок ────
def _mcc_campaign_status_cell(cr) -> str:
    """§8: строка разбивки кампаний по статусу для xlsx («ENABLED:3, PAUSED:5»). Fallback на
    ENABLED-счётчик (active_campaigns), если полной разбивки нет; '' если не прочитано."""
    sc = getattr(cr, "campaign_status", None)
    if isinstance(sc, dict) and sc:
        return ", ".join(f"{k}:{v}" for k, v in sorted(sc.items()))
    n = getattr(cr, "active_campaigns", None)
    return f"ENABLED:{n}" if n is not None else ""


def build_mcc_workbook(summary) -> Workbook:
    """Книга по сводке дочерних MCC (reports.mcc.MccSummary, duck-typed). Листы: «Сводка MCC»
    (подытоги ПО ВАЛЮТАМ — без FX), «Аккаунты» (по строке на лист-аккаунт), «Пропущено/ошибки»
    (read-list/менеджерские/частичные сбои — без тихого замалчивания, §8/§5)."""
    wb = Workbook()
    p = summary.period

    # 1) Сводка MCC: подытоги по валюте (валюта — отдельной колонкой, т.к. денежные колонки
    # разных валют нельзя свести в один заголовок; FX не делаем).
    ws = wb.active
    ws.title = "Сводка MCC"
    ws.append([f"Сводка по MCC {summary.manager_id}"])
    ws.append([f"Период: {p.label} ({p.date_from.isoformat()} — {p.date_to.isoformat()})"])
    ws.append([])
    headers = ["Валюта", "Аккаунтов", *metric_headers("")]
    ws.append(headers)
    header_row = ws.max_row
    for sub in summary.subtotals:
        ws.append([sub.currency, sub.accounts, *sub.totals.as_row()])
    for c in range(1, len(headers) + 1):
        ws.cell(row=header_row, column=c).fill = _HEADER_FILL
        ws.cell(row=header_row, column=c).font = _HEADER_FONT
    _apply_metric_formats_at(ws, 2, header_row + 1, len(summary.subtotals))
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    _autosize(ws, len(headers))

    # 2) Аккаунты: по строке на лист-аккаунт (id/имя/валюта/статус + метрики).
    acc = wb.create_sheet(title="Аккаунты")
    # §8: колонка «Кампании (статус)» — разбивка по статусу (ENABLED:n, PAUSED:m) перед метриками.
    acc_headers = ["ID", "Аккаунт", "Валюта", "Статус", "Кампании (статус)", *metric_headers("")]
    acc.append(acc_headers)
    for cr in summary.children:
        a = cr.account
        acc.append(
            [
                a.id,
                getattr(a, "name", "") or "",
                a.currency or "",
                getattr(a, "status", "") or "",
                _mcc_campaign_status_cell(cr),
                *cr.totals.as_row(),
            ]
        )
    _style_header_row(acc, len(acc_headers))
    _apply_metric_formats_at(acc, 5, 2, len(summary.children))
    _autosize(acc, len(acc_headers))

    # 3) Пропущено/ошибки: read-list пропуски, менеджерские, частичные сбои чтения.
    iss = wb.create_sheet(title="Пропущено и ошибки")
    iss.append(["Тип", "Аккаунт", "Причина"])
    _style_header_row(iss, 3)
    for ch in getattr(summary, "inactive", []):
        name = getattr(ch, "name", "") or ch.id
        iss.append(
            ["неактивный (не ENABLED)", f"{name} ({ch.id})", getattr(ch, "status", "") or ""]
        )
    for cid in summary.skipped:
        iss.append(["пропущен (нет доступа на чтение)", cid, ""])
    for cid in summary.managers:
        iss.append(["менеджерский (без собственных метрик)", cid, ""])
    for cid, reason in summary.errors:
        iss.append(["ошибка чтения", cid, reason])
    _autosize(iss, 3)
    return wb


def write_mcc_xlsx(summary, path: str) -> str:
    """Сохранить MCC-сводку в .xlsx по пути path. Возвращает path."""
    build_mcc_workbook(summary).save(path)
    return path


# ── 2.2 (аудит 2026-07-06): DEEP-книга по всем дочерним MCC — лист на аккаунт ─────────
_SHEET_BAD_CHARS = set("\\/*?:[]")


def _sheet_title(name: str, cid: str) -> str:
    """Имя листа openpyxl: «Имя_last4», санитизация запрещённых символов, ≤31 симв."""
    base = (name or cid or "acct").strip() or "acct"
    base = "".join(("_" if ch in _SHEET_BAD_CHARS else ch) for ch in base)
    return f"{base[:24]}_{str(cid)[-4:]}"[:31]


def build_mcc_deep_workbook(deep) -> Workbook:
    """Книга DEEP-отчёта (reports.mcc.MccDeep, duck-typed): лист «Сводка» (строка на аккаунт,
    метрики в валюте аккаунта — БЕЗ FX) → по ЛИСТУ НА АККАУНТ (итоги+сравнение периода, как
    write_report_xlsx, + таблица разбивки по кампаниям — не 8 листов × N акк.) → «Пропущено и
    ошибки» (без тихого замалчивания)."""
    wb = Workbook()
    p = deep.period

    ws = wb.active
    ws.title = "Сводка"
    ws.append([f"Глубокий отчёт по MCC {deep.manager_id}"])
    ws.append([f"Период: {p.label} ({p.date_from.isoformat()} — {p.date_to.isoformat()})"])
    ws.append([])
    headers = ["ID", "Аккаунт", "Валюта", *metric_headers("")]
    ws.append(headers)
    header_row = ws.max_row
    for ch, report in deep.items:
        ws.append(
            [
                ch.id,
                getattr(ch, "name", "") or "",
                ch.currency or "",
                *report.totals.as_row(),
            ]
        )
    for c in range(1, len(headers) + 1):
        ws.cell(row=header_row, column=c).fill = _HEADER_FILL
        ws.cell(row=header_row, column=c).font = _HEADER_FONT
    _apply_metric_formats_at(ws, 3, header_row + 1, len(deep.items))
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    _autosize(ws, len(headers))

    used_titles = {ws.title}
    for ch, report in deep.items:
        title = _sheet_title(getattr(ch, "name", "") or "", ch.id)
        while title in used_titles:  # коллизия имён (обрезка) — дособираем суффикс
            title = (title[:29] + "_x")[:31]
        used_titles.add(title)
        acc_ws = wb.create_sheet(title=title)
        _write_summary(acc_ws, report)
        camp = next((b for b in report.breakdowns if b.key == "campaign"), None)
        if camp and camp.rows:
            acc_ws.append([])
            camp_headers = camp.dim_headers + metric_headers(report.currency or "")
            acc_ws.append(camp_headers)
            hrow = acc_ws.max_row
            for dims, m in camp.rows:
                acc_ws.append(list(dims) + m.as_row())
            for c in range(1, len(camp_headers) + 1):
                acc_ws.cell(row=hrow, column=c).fill = _HEADER_FILL
                acc_ws.cell(row=hrow, column=c).font = _HEADER_FONT
            _apply_metric_formats_at(acc_ws, len(camp.dim_headers), hrow + 1, len(camp.rows))
            _autosize(acc_ws, len(camp_headers))

    iss = wb.create_sheet(title="Пропущено и ошибки")
    iss.append(["Тип", "Аккаунт", "Причина"])
    _style_header_row(iss, 3)
    for ch in deep.inactive:
        name = getattr(ch, "name", "") or ch.id
        iss.append(
            ["неактивный (не ENABLED)", f"{name} ({ch.id})", getattr(ch, "status", "") or ""]
        )
    for cid in deep.skipped:
        iss.append(["пропущен (нет доступа на чтение)", cid, ""])
    for cid in deep.managers:
        iss.append(["менеджерский (без собственных метрик)", cid, ""])
    for cid, reason in deep.errors:
        iss.append(["ошибка чтения", cid, reason])
    _autosize(iss, 3)
    return wb


def write_mcc_deep_xlsx(deep, path: str) -> str:
    """Сохранить DEEP-книгу по пути path. Возвращает path."""
    build_mcc_deep_workbook(deep).save(path)
    return path
