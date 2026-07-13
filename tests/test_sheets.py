"""Тесты Google Sheets-экспорта (ТЗ §9). Чистая сборка вкладок + публикация с мок-сервисом.
Без сети/живого OAuth. Фейк ReportData — дакт-тайпинг (SimpleNamespace)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reports.queries import METRIC_HEADERS  # noqa: E402
from reports.sheets import (  # noqa: E402
    SHARE_FAILED,
    build_sheets_data,
    is_shared,
    publish_report_to_sheets,
)

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


class _Perms:
    def __init__(self, log, boom=False):
        self.log, self._boom = log, boom

    def create(self, *, fileId, body, fields):
        if self._boom:
            raise RuntimeError("drive down")
        self.log.append(("permissions.create", fileId, body))
        return _Exec({"id": "perm1"})


class FakeDrive:
    """Мок Drive v3 — фиксирует permissions.create (P1: anyone-with-link на созданных таблицах)."""

    def __init__(self, boom=False):
        self.log: list = []
        self._boom = boom

    def permissions(self):
        return _Perms(self.log, self._boom)


def test_publish_creates_spreadsheet_and_writes_values():
    svc, drive = FakeService(), FakeDrive()
    url, share = publish_report_to_sheets(_report(), service=svc, drive_service=drive)
    assert url == "https://docs.google.com/spreadsheets/d/SID123"
    assert share == "reader"  # статус = выданная роль (не bool)
    kinds = [e[0] for e in svc.log]
    assert "create" in kinds and "values.batchUpdate" in kinds
    create = next(e for e in svc.log if e[0] == "create")
    titles = [s["properties"]["title"] for s in create[1]["sheets"]]
    assert titles == ["Сводка", "Кампании"]  # лист на сводку + на разбивку
    batch = next(e for e in svc.log if e[0] == "values.batchUpdate")
    ranges = [d["range"] for d in batch[2]["data"]]
    assert ranges == ["'Сводка'!A1", "'Кампании'!A1"]
    # P1: отчёт расшарен anyone-with-link ЧИТАТЕЛЕМ (не writer — финансовый артефакт)
    perm = next(e for e in drive.log if e[0] == "permissions.create")
    assert perm[1] == "SID123" and perm[2] == {"type": "anyone", "role": "reader"}


def test_publish_share_failure_degrades_without_raising():
    """P1: сбой Drive-шаринга НЕ роняет экспорт — ссылка возвращается, статус = SHARE_FAILED."""
    url, share = publish_report_to_sheets(
        _report(), service=FakeService(), drive_service=FakeDrive(boom=True)
    )
    assert url == "https://docs.google.com/spreadsheets/d/SID123"
    assert share == SHARE_FAILED and not is_shared(share)


def test_publish_logs_success(caplog):
    """§15: путь Sheets больше не «молчащий» — успешная публикация пишет лог (раньше при сбое
    scope/сети не было трейса). Логируется без секретов (имя/длительность/число вкладок)."""
    import logging as _logging

    svc = FakeService()
    with caplog.at_level(_logging.INFO, logger="aimash"):
        publish_report_to_sheets(_report(), service=svc, drive_service=FakeDrive())
    assert any("sheets-publish: ok" in r.getMessage() for r in caplog.records)


def test_publish_logs_failure_and_reraises(caplog):
    """Сбой Sheets API логируется (warning) и пробрасывается дальше (бот покажет понятное)."""
    import logging as _logging

    class _Boom:
        def spreadsheets(self):
            raise RuntimeError("scope drive.file missing")

    raised = False
    with caplog.at_level(_logging.WARNING, logger="aimash"):
        try:
            publish_report_to_sheets(_report(), service=_Boom())
        except RuntimeError:
            raised = True
    assert raised  # ошибка проброшена (не проглочена)
    assert any("sheets-publish:" in r.getMessage() for r in caplog.records)
