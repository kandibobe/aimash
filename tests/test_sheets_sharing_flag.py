"""B3: публичность Google Sheets — под флагом settings.sheets_public_link (дефолт True — прежнее
поведение). False ⇒ _share_anyone НЕ шарит публично (таблица приватна). При True бот предупреждает
«доступно всем по ссылке» (i18n sheets_public_warn).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reports.sheets as sheets  # noqa: E402
from bot import i18n  # noqa: E402
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


def test_public_link_flag_off_does_not_share():
    drive = _FakeDrive()
    with patched(settings, "sheets_public_link", False):
        shared = sheets._share_anyone("sheet1", role="reader", drive_service=drive)
    assert shared is False
    assert drive.created == []  # публичный доступ НЕ выдан (таблица приватна)


def test_public_link_flag_on_shares():
    drive = _FakeDrive()
    with patched(settings, "sheets_public_link", True):
        shared = sheets._share_anyone("sheet1", role="reader", drive_service=drive)
    assert shared is True
    assert drive.created and drive.created[0]["body"]["type"] == "anyone"


def test_public_warn_string_exists_ru_en():
    for lang in ("ru", "en"):
        msg = i18n.t("sheets_public_warn", lang)
        assert msg and ("ссылке" in msg or "link" in msg.lower())
