"""Экспорт результатов keyword research в .xlsx (openpyxl). READ-ONLY, без секретов.

Один лист «Ключевые слова»: строки сгруппированы по кластеру (интент), внутри — по убыванию
объёма поиска. Метрики уже посчитаны кодом (ads.keyword_plan.KeywordIdea); micros→валюту тоже
считал КОД. Стиль шапки/форматов — как в reports.xlsx (единый вид экспорта).
"""

from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from ads.keyword_plan import KeywordIdea, seasonality_sparkline
from keywords.cluster import Cluster
from reports.safe_cell import safe_row  # A9: защита от формула-инъекции в ячейках


def _append(ws, row) -> None:
    """A9: ws.append с обезвреживанием ячеек (safe_row) — имена/ключи из данных клиента."""
    ws.append(safe_row(row))


def _writerow(w, row) -> None:
    """A9: csv.writerow с обезвреживанием ячеек (safe_row) — CSV injection в тексте ключа."""
    w.writerow(safe_row(row))


_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_HEADER_FONT = Font(bold=True, color="FFFFFF")


def _headers(currency: str = "") -> list[str]:
    """D8: заголовки таблицы. Денежные колонки несут ВАЛЮТУ АККАУНТА (ставки в micros→валюту уже
    посчитал КОД, ads.keyword_plan) — раньше «Ставка/CPC» без валюты вводили в заблуждение при
    не-USD аккаунте. Пустая валюта (тест-аккаунт/сбой чтения) → без суффикса, как раньше."""
    suf = f" ({currency})" if currency else ""
    return [
        "Кластер",
        "Интент",
        "Ключевое слово",
        "Объём/мес",
        "Конкуренция",
        "Индекс (0-100)",
        f"Сред. CPC{suf}",
        f"Ставка низ{suf}",
        f"Ставка верх{suf}",
        "Пик сезона",
        "Сезонность (12 мес)",  # §7: полная кривая как unicode-спарклайн
    ]


HEADERS = _headers()  # обратная совместимость (тесты/старые вызовы без валюты)
# Форматы числовых колонок (с позиции «Объём/мес»); «Пик сезона» (10)/«Сезонность» (11) — текст.
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
    negatives: list[str] | None = None,
    currency: str = "",
) -> Workbook:
    by_text = {i.text: i for i in ideas}  # метрики по тексту ключа
    headers = _headers(currency)  # D8: денежные колонки с валютой аккаунта
    wb = Workbook()
    ws = wb.active
    ws.title = "Ключевые слова"

    src = ", ".join(seeds or []) or (url or "")
    _append(ws, [f"Keyword research — сиды: {src} (язык: {language})"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    _append(ws, [f"Всего ключей: {len(by_text)}, кластеров: {len(clusters)}"])
    _append(ws, [])

    header_row = ws.max_row + 1
    _append(ws, headers)
    for c in range(1, len(headers) + 1):
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
            _append(
                ws,
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
                    seasonality_sparkline(getattr(idea, "monthly", None)),
                ],
            )

    for col, fmt in _NUM_FORMATS.items():
        for r in range(header_row + 1, ws.max_row + 1):
            ws.cell(row=r, column=col).number_format = fmt
    _autosize(ws, len(headers))

    # §7 «предложение минус-слов» — отдельный лист (advisory; добавление идёт командой за confirm-гейтом).
    if negatives:
        ws2 = wb.create_sheet("Минус-слова")
        _append(
            ws2, ["Предложенные минус-слова (добавляются отдельной командой после подтверждения)"]
        )
        ws2.cell(row=1, column=1).font = Font(bold=True, size=14)
        _append(ws2, [])
        hr = ws2.max_row + 1
        _append(ws2, ["#", "Минус-слово"])
        for c in range(1, 3):
            ws2.cell(row=hr, column=c).fill = _HEADER_FILL
            ws2.cell(row=hr, column=c).font = _HEADER_FONT
        ws2.freeze_panes = f"A{hr + 1}"
        for i, n in enumerate(negatives, 1):
            _append(ws2, [i, n])
        _autosize(ws2, 2)
    return wb


def write_keywords_xlsx(
    clusters: list[Cluster],
    ideas: list[KeywordIdea],
    path: str,
    *,
    seeds: list[str] | None = None,
    url: str | None = None,
    language: str = "ru",
    negatives: list[str] | None = None,
    currency: str = "",
) -> str:
    """Сохранить .xlsx по пути path. Возвращает path. currency (D8) — валюта аккаунта в колонках ставок."""
    build_workbook(
        clusters,
        ideas,
        seeds=seeds,
        url=url,
        language=language,
        negatives=negatives,
        currency=currency,
    ).save(path)
    return path


def write_keywords_csv(
    clusters: list[Cluster],
    ideas: list[KeywordIdea],
    path: str,
    *,
    seeds: list[str] | None = None,
    url: str | None = None,
    language: str = "ru",
    currency: str = "",
) -> str:
    """ТЗ §7 «CSV/table export»: те же данные, что .xlsx, но плоским CSV (для импорта в таблицы/BI).
    Кодировка utf-8-sig (BOM) — Excel корректно открывает кириллицу. Группировка по кластеру, внутри —
    по убыванию объёма (как в .xlsx). Возвращает path. Минус-слова сюда не кладём (advisory, отдельно)."""
    import csv

    by_text = {i.text: i for i in ideas}
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        _writerow(w, _headers(currency))  # D8: денежные колонки с валютой аккаунта
        for cl in clusters:
            rows = sorted(
                (by_text.get(k) for k in cl.keywords),
                key=lambda i: (i.avg_monthly_searches if i else 0),
                reverse=True,
            )
            for idea in rows:
                if idea is None:
                    continue
                _writerow(
                    w,
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
                        seasonality_sparkline(getattr(idea, "monthly", None)),
                    ],
                )
    return path


def write_keyword_list_xlsx(keywords: list[str], match_type: str, action: str, path: str) -> str:
    """Компактный список ключей/минус-слов для ЧЕРНОВИКА мутации (ТЗ §5: большие списки —
    вложением, не текстом). Одна вкладка: # / ключ / тип соответствия / действие. Без метрик
    и секретов. Возвращает path."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Черновик ключей"
    _append(ws, [f"{action} — {len(keywords)} шт. (тип соответствия: {match_type})"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    _append(ws, [])

    header_row = ws.max_row + 1
    headers = ["#", "Ключевое слово", "Тип соответствия", "Действие"]
    _append(ws, headers)
    for c in range(1, len(headers) + 1):
        ws.cell(row=header_row, column=c).fill = _HEADER_FILL
        ws.cell(row=header_row, column=c).font = _HEADER_FONT
    ws.freeze_panes = f"A{header_row + 1}"

    for i, kw in enumerate(keywords, 1):
        _append(ws, [i, kw, match_type, action])
    _autosize(ws, len(headers))
    wb.save(path)
    return path
