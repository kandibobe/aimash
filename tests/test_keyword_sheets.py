"""§19.4.2 (iv-vi): офлайн-тесты выгрузки ключей в Google Sheets + чтения верифицированного списка.

Сеть (Sheets API) подменяется фейковым service; чистая сборка строк и парсер id тестируются прямо.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reports.sheets as sheets_mod  # noqa: E402
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


def test_sheets_consent_asks_only_non_sensitive_scope():
    """РЕГРЕССИЯ: consent аккаунта-ХРАНИЛИЩА = ровно [drive.file] (non-sensitive).

    spreadsheets.readonly у Google SENSITIVE: неверифицированному приложению Google отвечает «This app
    is blocked… tried to access sensitive info» — владелец аккаунта не может выдать согласие вовсе
    (2026-07, заказчик). Sensitive-scope несёт ТОЛЬКО Ads-токен (наш аккаунт, consent уже выдан).
    """
    from reports.sheets import SHEETS_READONLY_SCOPE, SHEETS_SCOPE, SHEETS_SCOPES

    assert SHEETS_SCOPES == [SHEETS_SCOPE]  # создание таблиц — и ничего сверх
    assert SHEETS_READONLY_SCOPE not in SHEETS_SCOPES  # вернёшь → снова «This app is blocked»


def test_consent_scopes_match_between_script_and_reports():
    """Списки scope продублированы руками (скрипт не тянет reports) — расхождение ловим здесь."""
    import reports.sheets as rs

    grt = pytest.importorskip("scripts.get_refresh_token")
    assert grt.SHEETS_SCOPES == rs.SHEETS_SCOPES


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


def _capture_creds(monkeypatch, service: Any = None) -> dict:
    """Подменяет google-libs и возвращает dict с kwargs, ушедшими в Credentials(...)."""
    captured: dict = {}

    class _FakeCreds:
        def __init__(self, **kw):
            captured.update(kw)

    fake_google = SimpleNamespace(oauth2=SimpleNamespace(credentials=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.oauth2", fake_google.oauth2)
    monkeypatch.setitem(
        sys.modules, "google.oauth2.credentials", SimpleNamespace(Credentials=_FakeCreds)
    )
    monkeypatch.setitem(
        sys.modules,
        "googleapiclient.discovery",
        SimpleNamespace(
            build=lambda *a, **k: service if service is not None else SimpleNamespace()
        ),
    )
    return captured


def test_sheets_credentials_fall_back_to_ads_token(monkeypatch):
    """SHEETS_REFRESH_TOKEN не задан ⇒ прежнее поведение: Sheets ходит под Ads-токеном (таблицы
    лежат на Google-аккаунте Ads-токена)."""
    from pydantic import SecretStr

    import reports.sheets as rs
    from core.config import settings

    captured = _capture_creds(monkeypatch)
    monkeypatch.setattr(settings, "sheets_refresh_token", SecretStr(""))
    monkeypatch.setattr(settings, "google_ads_refresh_token", SecretStr("ADS-RT"))
    monkeypatch.setattr(settings, "google_ads_client_id", "ads-cid")
    monkeypatch.setattr(settings, "google_ads_client_secret", SecretStr("ads-secret"))
    rs._oauth_credentials()
    assert captured["refresh_token"] == "ADS-RT" and captured["client_id"] == "ads-cid"


def test_sheets_token_overrides_ads_and_inherits_oauth_client(monkeypatch):
    """SHEETS_REFRESH_TOKEN задан ⇒ таблицы создаются на ЕГО Google-аккаунте (аккаунт-хранилище),
    Ads-токен не используется. client_id/secret пустые ⇒ наследуются от Ads (тот же OAuth-клиент
    Google Cloud — чужой клиент refresh не примет)."""
    from pydantic import SecretStr

    import reports.sheets as rs
    from core.config import settings

    captured = _capture_creds(monkeypatch)
    monkeypatch.setattr(settings, "sheets_refresh_token", SecretStr("SHEETS-RT"))
    monkeypatch.setattr(settings, "sheets_client_id", "")
    monkeypatch.setattr(settings, "sheets_client_secret", SecretStr(""))
    monkeypatch.setattr(settings, "google_ads_refresh_token", SecretStr("ADS-RT"))
    monkeypatch.setattr(settings, "google_ads_client_id", "ads-cid")
    monkeypatch.setattr(settings, "google_ads_client_secret", SecretStr("ads-secret"))
    rs._oauth_credentials()
    assert captured["refresh_token"] == "SHEETS-RT"  # НЕ Ads-токен
    assert captured["client_id"] == "ads-cid" and captured["client_secret"] == "ads-secret"
    assert captured["scopes"] is None  # scope на refresh по-прежнему не запрашиваем


class _FakeSheetsService:
    """Минимальный сервис Sheets API: spreadsheets().values().get(...).execute()."""

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kw):
        return self

    def execute(self):
        return {"values": [["Keyword"], ["used cars nairobi"]]}


def _both_tokens(monkeypatch) -> dict:
    """Оба токена заданы (прод-расклад): Ads — наш аккаунт, sheets — аккаунт-хранилище."""
    from pydantic import SecretStr

    from core.config import settings

    captured = _capture_creds(monkeypatch, service=_FakeSheetsService())
    monkeypatch.setattr(settings, "sheets_refresh_token", SecretStr("SHEETS-RT"))
    monkeypatch.setattr(settings, "sheets_client_id", "")
    monkeypatch.setattr(settings, "sheets_client_secret", SecretStr(""))
    monkeypatch.setattr(settings, "google_ads_refresh_token", SecretStr("ADS-RT"))
    monkeypatch.setattr(settings, "google_ads_client_id", "ads-cid")
    monkeypatch.setattr(settings, "google_ads_client_secret", SecretStr("ads-secret"))
    return captured


def test_foreign_sheet_is_read_with_ads_token(monkeypatch):
    """§19.4.1 / `/kw add`: ЧУЖУЮ таблицу читаем Ads-токеном — sensitive-scope spreadsheets.readonly
    есть только у него (у аккаунта-хранилища мы его не просим: consent бы заблокировали)."""
    captured = _both_tokens(monkeypatch)
    assert read_keyword_column("SID") == ["used cars nairobi"]  # own_file=False по умолчанию
    assert captured["refresh_token"] == "ADS-RT"


def test_own_sheet_is_read_with_storage_token(monkeypatch):
    """§19.4.2 round-trip: таблицу, СОЗДАННУЮ ботом, читаем кредами аккаунта-хранилища — Ads-токен её
    не увидит (drive.file видит только своё), если публичные ссылки выключены."""
    captured = _both_tokens(monkeypatch)
    assert read_keyword_column("SID", own_file=True) == ["used cars nairobi"]
    assert captured["refresh_token"] == "SHEETS-RT"


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
    url, sid, share = publish_keywords_to_sheets(
        ideas,
        {"used cars": True},
        title="kw-test",
        service=_FakeService(created_id="XYZ"),
        drive_service=drive,
    )
    assert sid == "XYZ"
    assert "XYZ" in url
    # P1 (живой тест 2026-07-06): таблица ключей — anyone-with-link РЕДАКТОР (флоу просит её править)
    assert share == "writer"  # статус = выданная роль
    assert drive.log == [("XYZ", {"type": "anyone", "role": "writer"})]


def test_publish_keywords_share_failure_degrades_without_raising():
    ideas = [_idea("used cars", avg_monthly_searches=100)]
    url, sid, share = publish_keywords_to_sheets(
        ideas,
        {"used cars": True},
        title="kw-test",
        service=_FakeService(created_id="XYZ"),
        drive_service=_FakeDrive(boom=True),
    )
    assert sid == "XYZ" and "XYZ" in url
    # ссылка всё равно уходит, бот добавит подсказку «запросите доступ»
    assert share == sheets_mod.SHARE_FAILED


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
