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
    # §8 (P1-G): колонка «Активных кампаний» (ENABLED) — перед метриками.
    acc_headers = ["ID", "Аккаунт", "Валюта", "Статус", "Активных кампаний", *metric_headers("")]
    acc.append(acc_headers)
    for cr in summary.children:
        a = cr.account
        n_active = getattr(cr, "active_campaigns", None)
        acc.append(
            [
                a.id,
                getattr(a, "name", "") or "",
                a.currency or "",
                getattr(a, "status", "") or "",
                n_active if n_active is not None else "",
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
