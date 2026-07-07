"""§19.4.2 (iv-vi): офлайн-тесты выгрузки ключей в Google Sheets + чтения верифицированного списка.

Сеть (Sheets API) подменяется фейковым service; чистая сборка строк и парсер id тестируются прямо.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reports.sheets import (  # noqa: E402
    build_keyword_sheet_rows,
    parse_spreadsheet_id,
    publish_keywords_to_sheets,
    read_keyword_column,
)


def _idea(text, **kw):
    base = dict(avg_monthly_searches=0, competition="", low_bid=0.0, high_bid=0.0)
    base.update(kw)
    return SimpleNamespace(text=text, **base)


def test_build_rows_has_header_and_relevance():
    ideas = [
        _idea(
            "used cars nairobi",
            avg_monthly_searches=12100,
            competition="MEDIUM",
            low_bid=0.14,
            high_bid=0.32,
        ),
        _idea("free car games", avg_monthly_searches=3100, competition="LOW"),
    ]
    rel = {"used cars nairobi": True, "free car games": False}
    rows = build_keyword_sheet_rows(ideas, rel)
    assert rows[0] == [
        "Keyword",
        "Avg. searches",
        "Competition",
        "Top-of-page bid",
        "Релевантность",
    ]
    assert rows[1][0] == "used cars nairobi" and rows[1][4] == "✅ Релевантно"
    assert rows[2][4] == "❌ Нерелевантно"
    assert rows[1][3] == "0.14–0.32"


def test_build_rows_shows_dash_for_missing_metrics():
    # Тест-аккаунт / ключ без метрик: объём 0, competition UNSPECIFIED, ставки 0 → «—», не ложный 0.
    ideas = [_idea("no metrics kw", avg_monthly_searches=0, competition="UNSPECIFIED")]
    rows = build_keyword_sheet_rows(ideas, {"no metrics kw": True})
    assert rows[1] == ["no metrics kw", "—", "—", "—", "✅ Релевантно"]


def test_sheets_scopes_include_readonly_for_arbitrary_sheets():
    # §19.4.1: список CONSENT (get_refresh_token) должен включать readonly для чтения чужих таблиц.
    from reports.sheets import SHEETS_SCOPES

    assert "https://www.googleapis.com/auth/drive.file" in SHEETS_SCOPES  # создание
    assert "https://www.googleapis.com/auth/spreadsheets.readonly" in SHEETS_SCOPES  # чтение чужих


def test_build_service_does_not_over_request_scopes_on_refresh(monkeypatch):
    """РЕГРЕССИЯ: _build_service НЕ должен слать scope ШИРЕ выданного токену на refresh — иначе
    Google вернёт invalid_scope и упадёт ВЕСЬ Sheets-экспорт (прод-баг 2026-07). Ждём scopes=None."""
    import reports.sheets as rs

    captured = {}

    class _FakeCreds:
        def __init__(self, **kw):
            captured.update(kw)

    fake_google = SimpleNamespace(oauth2=SimpleNamespace(credentials=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.oauth2", fake_google.oauth2)
    monkeypatch.setitem(
        sys.modules,
        "google.oauth2.credentials",
        SimpleNamespace(Credentials=_FakeCreds),
    )
    monkeypatch.setitem(
        sys.modules,
        "googleapiclient.discovery",
        SimpleNamespace(build=lambda *a, **k: SimpleNamespace()),
    )
    rs._build_service()
    assert captured.get("scopes") is None  # scope на refresh не запрашиваем


def test_parse_spreadsheet_id():
    assert (
        parse_spreadsheet_id("https://docs.google.com/spreadsheets/d/ABC123_xyz-9/edit#gid=0")
        == "ABC123_xyz-9"
    )
    assert parse_spreadsheet_id("ABCDEFGHIJKLMNOPQRSTUVWX") == "ABCDEFGHIJKLMNOPQRSTUVWX"
    assert parse_spreadsheet_id("just text") is None
    assert parse_spreadsheet_id("") is None


class _FakeValues:
    def __init__(self, rows):
        self._rows = rows

    def batchUpdate(self, **kw):  # noqa: N802 — зеркалим API google-api-python-client
        return SimpleNamespace(execute=lambda: {})

    def get(self, **kw):  # noqa: N802
        return SimpleNamespace(execute=lambda: {"values": self._rows})


class _FakeSheets:
    def __init__(self, rows=None, created_id="SID123"):
        self._rows = rows or []
        self._id = created_id

    def create(self, **kw):  # noqa: N802
        return SimpleNamespace(
            execute=lambda: {
                "spreadsheetId": self._id,
                "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{self._id}",
            }
        )

    def values(self):
        return _FakeValues(self._rows)


class _FakeService:
    def __init__(self, rows=None, created_id="SID123"):
        self._s = _FakeSheets(rows, created_id)

    def spreadsheets(self):
        return self._s


class _FakePerms:
    def __init__(self, log, boom=False):
        self.log, self._boom = log, boom

    def create(self, *, fileId, body, fields):  # noqa: N803 — зеркалим API
        if self._boom:
            raise RuntimeError("drive down")
        self.log.append((fileId, body))
        return SimpleNamespace(execute=lambda: {"id": "perm1"})

    def execute(self):
        return {}


class _FakeDrive:
    def __init__(self, boom=False):
        self.log: list = []
        self._boom = boom

    def permissions(self):
        return _FakePerms(self.log, self._boom)


def test_publish_keywords_returns_url_id_and_shares_writer():
    ideas = [_idea("used cars", avg_monthly_searches=100)]
    drive = _FakeDrive()
    url, sid, shared = publish_keywords_to_sheets(
        ideas,
        {"used cars": True},
        title="kw-test",
        service=_FakeService(created_id="XYZ"),
        drive_service=drive,
    )
    assert sid == "XYZ"
    assert "XYZ" in url
    # P1 (живой тест 2026-07-06): таблица ключей — anyone-with-link РЕДАКТОР (флоу просит её править)
    assert shared is True
    assert drive.log == [("XYZ", {"type": "anyone", "role": "writer"})]


def test_publish_keywords_share_failure_degrades_without_raising():
    ideas = [_idea("used cars", avg_monthly_searches=100)]
    url, sid, shared = publish_keywords_to_sheets(
        ideas,
        {"used cars": True},
        title="kw-test",
        service=_FakeService(created_id="XYZ"),
        drive_service=_FakeDrive(boom=True),
    )
    assert sid == "XYZ" and "XYZ" in url
    assert shared is False  # ссылка всё равно уходит, бот добавит подсказку «запросите доступ»


def test_read_keyword_column_skips_header_and_dedups():
    rows = [
        ["Keyword", "Avg. searches"],  # шапка
        ["used cars nairobi", "12100"],
        ["second hand cars", "8200"],
        ["used cars nairobi", "12100"],  # дубль
        ["", ""],  # пустая
    ]
    out = read_keyword_column("SID", service=_FakeService(rows=rows))
    assert out == ["used cars nairobi", "second hand cars"]
