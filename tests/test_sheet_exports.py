"""Реестр созданных ботом Google-таблиц (db.sheets_registry + /mysheets).

Раньше ссылка на таблицу жила только в сообщении Telegram (для ключей — ещё в черновике визарда с
TTL 72ч): закрыл визард — ищи в истории чата. Проверяем: запись переживает, выдаётся ТОЛЬКО своему
чату (ссылка anyone-with-link — фактически ключ доступа), сбой БД реестра НЕ роняет экспорт, а
не-публичные таблицы помечены в выдаче. Реальная БД на temp SQLite (conftest).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import texts  # noqa: E402
from db import sheets_registry  # noqa: E402
from db.session import init_db  # noqa: E402

CHAT = 90901
OTHER = 90902


async def _rec(chat_id: int, **kw):
    base = dict(
        chat_id=chat_id,
        kind="keywords",
        spreadsheet_id="SID1",
        url="https://docs.google.com/spreadsheets/d/SID1",
        title="kw-test",
        share="writer",
        customer_id="7753643025",
    )
    base.update(kw)
    await sheets_registry.record(**base)


async def test_record_then_list_recent_newest_first():
    await init_db()
    await _rec(CHAT, spreadsheet_id="OLD", url="https://docs.google.com/spreadsheets/d/OLD")
    await _rec(
        CHAT,
        kind="report",
        spreadsheet_id="NEW",
        url="https://docs.google.com/spreadsheets/d/NEW",
        share="reader",
    )
    rows = await sheets_registry.list_recent(CHAT, limit=10)
    assert [r.url.rsplit("/", 1)[-1] for r in rows][:2] == ["NEW", "OLD"]  # новые сверху
    assert rows[0].kind == "report" and rows[0].share == "reader"
    assert rows[1].customer_id == "7753643025"


async def test_list_recent_is_scoped_to_own_chat():
    """Ссылка anyone-with-link открывается БЕЗ входа в Google — чужому чату её не показываем."""
    await init_db()
    await _rec(OTHER, spreadsheet_id="ALIEN", url="https://docs.google.com/spreadsheets/d/ALIEN")
    rows = await sheets_registry.list_recent(CHAT, limit=10)
    assert all("ALIEN" not in r.url for r in rows)


async def test_record_failure_does_not_raise(monkeypatch):
    """Реестр вторичен: уже успешный экспорт (ссылка у пользователя) не должен падать из-за БД."""
    await init_db()

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(sheets_registry, "Session", _boom)
    await _rec(CHAT)  # не бросает


def _row(share: str, kind: str = "keywords"):
    return SimpleNamespace(
        kind=kind,
        url="https://docs.google.com/spreadsheets/d/SID1",
        title="kw-test",
        customer_id="7753643025",
        share=share,
        created_at=None,
    )


def test_fmt_my_sheets_marks_only_non_public():
    """Публичную не помечаем; off/failed — помечаем: иначе получатель решит, что ссылку можно
    переслать, а она откроется только владельцу."""
    out = texts.fmt_my_sheets([_row("writer")], lang="ru")
    assert "docs.google.com" in out and "🔒" not in out
    for share in ("off", "failed"):
        assert "🔒" in texts.fmt_my_sheets([_row(share)], lang="ru")


def test_fmt_my_sheets_empty_ru_en():
    assert texts.fmt_my_sheets([], lang="ru")
    assert texts.fmt_my_sheets([], lang="en")
