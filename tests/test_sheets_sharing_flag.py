"""B3: публичность Google Sheets — под флагом settings.sheets_public_link (дефолт True — прежнее
поведение). False ⇒ _share_anyone НЕ шарит публично (таблица приватна). При True бот предупреждает
«доступно всем по ссылке» (i18n sheets_public_warn).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reports.sheets as sheets  # noqa: E402
from core import i18n  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class _FakeDrive:
    def __init__(self):
        self.created = []

    def permissions(self):
        return self

    def create(self, **kw):
        self.created.append(kw)
        return self

    def execute(self):
        return {"id": "x"}


class _BoomDrive:
    def permissions(self):
        return self

    def create(self, **kw):
        raise RuntimeError("drive down")


def test_public_link_flag_off_does_not_share():
    drive = _FakeDrive()
    with patched(settings, "sheets_public_link", False):
        share = sheets._share_anyone("sheet1", role="reader", drive_service=drive)
    assert share == sheets.SHARE_OFF  # выключено ВЛАДЕЛЬЦЕМ — это не «сбой шаринга»
    assert not sheets.is_shared(share)
    assert drive.created == []  # публичный доступ НЕ выдан (таблица приватна)


def test_public_link_flag_on_shares():
    drive = _FakeDrive()
    with patched(settings, "sheets_public_link", True):
        share = sheets._share_anyone("sheet1", role="reader", drive_service=drive)
    assert share == "reader" and sheets.is_shared(share)  # статус = ВЫДАННАЯ роль
    assert drive.created and drive.created[0]["body"]["type"] == "anyone"


def test_share_failure_is_distinguishable_from_flag_off():
    """Отказ Drive и «выключено владельцем» — РАЗНЫЕ статусы: бот показывает разные подсказки
    (раньше оба давали False и врали пользователю «не удалось открыть доступ»)."""
    with patched(settings, "sheets_public_link", True):
        share = sheets._share_anyone("sheet1", role="writer", drive_service=_BoomDrive())
    assert share == sheets.SHARE_FAILED
    assert not sheets.is_shared(share)
    assert sheets.SHARE_FAILED != sheets.SHARE_OFF


def test_share_failure_reason_carries_status_and_is_redacted():
    """Диагноз обязан нести HTTP-статус Google (без него не отличить «нет scope» от «политика
    домена»), но НЕ секреты — текст исключения проходит redact_text (правило 5)."""
    err = RuntimeError("403 Forbidden: refresh_token=1//supersecrettokenvalue0123456789")
    err.resp = SimpleNamespace(status=403)
    reason = sheets._share_failure_reason(err)
    assert "status=403" in reason and "RuntimeError" in reason
    assert "supersecrettokenvalue0123456789" not in reason


def test_public_warn_string_exists_ru_en():
    for lang in ("ru", "en"):
        msg = i18n.t("sheets_public_warn", lang)
        assert msg and ("ссылке" in msg or "link" in msg.lower())


def test_share_off_note_exists_ru_en():
    """Отдельная подсказка для «владелец выключил публичные ссылки» — не «не удалось»."""
    for lang in ("ru", "en"):
        msg = i18n.t("sheets_share_off_note", lang)
        assert msg and msg != i18n.t("sheets_share_failed_note", lang)
