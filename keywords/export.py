"""Экспорт результатов keyword research в .xlsx (openpyxl). READ-ONLY, без секретов.

Один лист «Ключевые слова»: строки сгруппированы по кластеру (интент), внутри — по убыванию
объёма поиска. Метрики уже посчитаны кодом (ads.keyword_plan.KeywordIdea); micros→валюту тоже
считал КОД. Стиль шапки/форматов — как в reports.xlsx (единый вид экспорта).
"""

from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from ads.keyword_plan import KeywordIdea
from keywords.cluster import Cluster

_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_HEADER_FONT = Font(bold=True, color="FFFFFF")

HEADERS = [
    "Кластер",
    "Интент",
    "Ключевое слово",
    "Объём/мес",
    "Конкуренция",
    "Индекс (0-100)",
    "Сред. CPC",
    "Ставка (низ)",
    "Ставка (верх)",
    "Пик сезона",
]
# Форматы числовых колонок (с позиции «Объём/мес»); «Пик сезона» (10) — текст.
_NUM_FORMATS = {4: "#,##0", 6: "0", 7: "#,##0.00", 8: "#,##0.00", 9: "#,##0.00"}


def _autosize(ws, ncols: int, *, cap: int = 50) -> None:
    for c in range(1, ncols + 1):
        letter = get_column_letter(c)
        width = max(
            (len(str(ws.cell(row=r, column=c).value or "")) for r in range(1, ws.max_row + 1)),
            default=10,
        )
        ws.column_dimensions[letter].width = min(max(width + 2, 10), cap)


def build_workbook(
    clusters: list[Cluster],
    ideas: list[KeywordIdea],
    *,
    seeds: list[str] | None = None,
    url: str | None = None,
    language: str = "ru",
) -> Workbook:
    by_text = {i.text: i for i in ideas}  # метрики по тексту ключа
    wb = Workbook()
    ws = wb.active
    ws.title = "Ключевые слова"

    src = ", ".join(seeds or []) or (url or "")
    ws.append([f"Keyword research — сиды: {src} (язык: {language})"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws.append([f"Всего ключей: {len(by_text)}, кластеров: {len(clusters)}"])
    ws.append([])

    header_row = ws.max_row + 1
    ws.append(HEADERS)
    for c in range(1, len(HEADERS) + 1):
        ws.cell(row=header_row, column=c).fill = _HEADER_FILL
        ws.cell(row=header_row, column=c).font = _HEADER_FONT
    ws.freeze_panes = f"A{header_row + 1}"

    for cl in clusters:
        rows = sorted(
            (by_text.get(k) for k in cl.keywords),
            key=lambda i: (i.avg_monthly_searches if i else 0),
            reverse=True,
        )
        for idea in rows:
            if idea is None:
                continue
            ws.append(
                [
                    cl.name,
                    cl.intent,
                    idea.text,
                    idea.avg_monthly_searches,
                    idea.competition,
                    idea.competition_index,
                    round(idea.avg_cpc, 2),
                    round(idea.low_bid, 2),
                    round(idea.high_bid, 2),
                    idea.peak_month,
                ]
            )

    for col, fmt in _NUM_FORMATS.items():
        for r in range(header_row + 1, ws.max_row + 1):
            ws.cell(row=r, column=col).number_format = fmt
    _autosize(ws, len(HEADERS))
    return wb


def write_keywords_xlsx(
    clusters: list[Cluster],
    ideas: list[KeywordIdea],
    path: str,
    *,
    seeds: list[str] | None = None,
    url: str | None = None,
    language: str = "ru",
) -> str:
    """Сохранить .xlsx по пути path. Возвращает path."""
    build_workbook(clusters, ideas, seeds=seeds, url=url, language=language).save(path)
    return path


def write_keyword_list_xlsx(keywords: list[str], match_type: str, action: str, path: str) -> str:
    """Компактный список ключей/минус-слов для ЧЕРНОВИКА мутации (ТЗ §5: большие списки —
    вложением, не текстом). Одна вкладка: # / ключ / тип соответствия / действие. Без метрик
    и секретов. Возвращает path."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Черновик ключей"
    ws.append([f"{action} — {len(keywords)} шт. (тип соответствия: {match_type})"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws.append([])

    header_row = ws.max_row + 1
    headers = ["#", "Ключевое слово", "Тип соответствия", "Действие"]
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        ws.cell(row=header_row, column=c).fill = _HEADER_FILL
        ws.cell(row=header_row, column=c).font = _HEADER_FONT
    ws.freeze_panes = f"A{header_row + 1}"

    for i, kw in enumerate(keywords, 1):
        ws.append([i, kw, match_type, action])
    _autosize(ws, len(headers))
    wb.save(path)
    return path
