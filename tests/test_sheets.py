"""Тесты Google Sheets-экспорта (ТЗ §9). Чистая сборка вкладок + публикация с мок-сервисом.
Без сети/живого OAuth. Фейк ReportData — дакт-тайпинг (SimpleNamespace)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reports.queries import METRIC_HEADERS  # noqa: E402
from reports.sheets import build_sheets_data, publish_report_to_sheets  # noqa: E402

_ROW = [100, 10, 0.1, 0.5, 5.0, 2.0, 50.0, 2.5, 10.0]  # как Metrics.as_row()


def _metrics():
    return SimpleNamespace(as_row=lambda: list(_ROW))


def _report(*, with_prev=True, note=""):
    period = SimpleNamespace(
        label="7 дней",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 7),
        previous=lambda: SimpleNamespace(label="пред. 7 дней"),
    )
    bd = SimpleNamespace(
        title="Кампании",
        note=note,
        dim_headers=["Кампания", "Статус"],
        rows=[(("Brand", "ENABLED"), _metrics()), (("Sale", "PAUSED"), _metrics())],
        key="campaign",
    )
    return SimpleNamespace(
        customer_id="7753643025",
        period=period,
        totals=_metrics(),
        prev_totals=_metrics() if with_prev else None,
        breakdowns=[bd],
    )


def test_build_sheets_data_summary_and_breakdown():
    tabs = build_sheets_data(_report())
    assert tabs[0].title == "Сводка"
    assert ["Период", *METRIC_HEADERS] in tabs[0].rows  # шапка итогов
    assert tabs[0].rows[4][0] == "7 дней"  # строка текущего периода
    assert any(r and r[0] == "пред. 7 дней" for r in tabs[0].rows)  # сравнение
    # вкладка разбивки: шапка размерностей + метрик, затем строки данных
    camp = tabs[1]
    assert camp.title == "Кампании"
    assert camp.rows[0] == ["Кампания", "Статус", *METRIC_HEADERS]
    assert camp.rows[1][:2] == ["Brand", "ENABLED"]
    assert len(camp.rows) == 3  # шапка + 2 строки


def test_build_sheets_data_no_comparison_omits_prev():
    tabs = build_sheets_data(_report(with_prev=False))
    assert not any(r and r and r[0] == "пред. 7 дней" for r in tabs[0].rows)


def test_build_sheets_data_note_first_row():
    tabs = build_sheets_data(_report(note="показаны топ-50"))
    assert tabs[1].rows[0] == ["показаны топ-50"]


def test_sanitize_title_dedup_and_forbidden_chars():
    rep = _report()
    rep.breakdowns = [
        SimpleNamespace(title="A/B:test", note="", dim_headers=["d"], rows=[]),
        SimpleNamespace(
            title="A B test", note="", dim_headers=["d"], rows=[]
        ),  # коллизия после очистки
    ]
    tabs = build_sheets_data(rep)
    names = [t.title for t in tabs]
    assert "A B test" in names  # '/' и ':' заменены на пробел
    assert "A B test (2)" in names  # дубликат уникализирован
    assert all(not (set("[]:*?/\\") & set(n)) for n in names)  # запрещённых символов нет


# ── Публикация с мок-сервисом ────────────────────────────────────────────────────
class _Exec:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _Values:
    def __init__(self, log):
        self.log = log

    def batchUpdate(self, *, spreadsheetId, body):
        self.log.append(("values.batchUpdate", spreadsheetId, body))
        return _Exec({})


class _Spreadsheets:
    def __init__(self, log):
        self.log = log

    def create(self, *, body, fields):
        self.log.append(("create", body, fields))
        return _Exec(
            {
                "spreadsheetId": "SID123",
                "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/SID123",
            }
        )

    def values(self):
        return _Values(self.log)


class FakeService:
    def __init__(self):
        self.log: list = []

    def spreadsheets(self):
        return _Spreadsheets(self.log)


def test_publish_creates_spreadsheet_and_writes_values():
    svc = FakeService()
    url = publish_report_to_sheets(_report(), service=svc)
    assert url == "https://docs.google.com/spreadsheets/d/SID123"
    kinds = [e[0] for e in svc.log]
    assert "create" in kinds and "values.batchUpdate" in kinds
    create = next(e for e in svc.log if e[0] == "create")
    titles = [s["properties"]["title"] for s in create[1]["sheets"]]
    assert titles == ["Сводка", "Кампании"]  # лист на сводку + на разбивку
    batch = next(e for e in svc.log if e[0] == "values.batchUpdate")
    ranges = [d["range"] for d in batch[2]["data"]]
    assert ranges == ["'Сводка'!A1", "'Кампании'!A1"]
