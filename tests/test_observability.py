"""Sentry-мониторинг (core.observability): редакция секретов в событии + безопасный no-op.

Главное — проверить, что before_send РЕДАКТИРУЕТ секрето-подобные строки во всех частях события
(сообщение, traceback, breadcrumbs), т.к. RedactionFilter висит на лог-хендлере и до Sentry не
доходит (golden rule #5). Без сети и без реального DSN.
"""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace

from core import context
from core import observability as obs


def test_before_send_redacts_nested_secrets():
    event = {
        "logentry": {"message": "oauth refresh 1//ABCDEFGHIJKLMNOPQRSTU"},
        "exception": {"values": [{"value": "boom api_key=supersecretvalue123 trailing"}]},
        "breadcrumbs": [{"message": "Authorization: Bearer abc.def.ghijklmno"}],
    }
    out = obs._before_send(event, {})
    flat = str(out)
    assert "1//ABCDEFGHIJKLMNOPQRSTU" not in flat
    assert "supersecretvalue123" not in flat
    assert "abc.def.ghijklmno" not in flat
    assert "REDACTED" in out["exception"]["values"][0]["value"]


def test_before_send_never_raises_on_weird_values():
    # Нестроковые значения проходят насквозь; редакция не должна ломать отправку события.
    out = obs._before_send({"obj": object(), "n": 5, "nested": ["a token=zzz123456"]}, {})
    assert out is not None
    assert "zzz123456" not in str(out["nested"])


def test_init_observability_noop_without_dsn():
    # В dev/тестах SENTRY_DSN пуст → init тихий no-op (без исключений, без импорта sentry).
    obs.init_observability()


# ── core.context: корреляционный request_id (§15) ────────────────────────────────
def test_new_request_id_is_short_hex():
    rid = context.new_request_id()
    assert len(rid) == 8
    assert all(c in "0123456789abcdef" for c in rid)
    assert context.new_request_id() != context.new_request_id()  # уникальный


def test_request_scope_sets_and_resets():
    assert context.get_context().request_id == "-"  # вне scope — пусто
    with context.request_scope("job", chat_id=7) as rid:
        c = context.get_context()
        assert c.request_id == rid and c.chat_id == 7 and c.operation == "job"
    assert context.get_context().request_id == "-"  # сброшено в finally


def test_set_context_merges_fields():
    tok = context.set_context(request_id="aaa", chat_id=1, operation="op1")
    try:
        tok2 = context.set_context(operation="op2")  # только operation; остальное сохраняется
        try:
            c = context.get_context()
            assert c.request_id == "aaa" and c.chat_id == 1 and c.operation == "op2"
        finally:
            context.reset_context(tok2)
    finally:
        context.reset_context(tok)
    assert context.get_context().request_id == "-"


# ── core.logging.ContextFilter: впрыск контекста в КАЖДУЮ запись ──────────────────
def test_context_filter_injects_request_id():
    from core.logging import ContextFilter

    rec = logging.LogRecord("aimash", logging.INFO, __file__, 1, "msg", None, None)
    with context.request_scope("op-x", chat_id=42):
        ContextFilter().filter(rec)
        assert rec.request_id == context.get_context().request_id
        assert rec.chat_id == 42
        assert rec.operation == "op-x"
    rec2 = logging.LogRecord("aimash", logging.INFO, __file__, 1, "msg", None, None)
    ContextFilter().filter(rec2)  # вне scope — дефолты, не падает
    assert rec2.request_id == "-" and rec2.chat_id == "-"


# ── core.errors.capture_exception: лог + персист (редактированно) ─────────────────
async def test_capture_exception_persists_redacted_and_returns_request_id():
    from sqlalchemy import select

    from core import errors
    from db.models import ErrorEvent
    from db.session import Session, init_db

    await init_db()
    secret = "sk-or-v1-ABCDEF0123456789ABCDEF0123"  # gitleaks:allow (фейк токен теста)
    with context.request_scope("unit-test") as rid:
        try:
            raise ValueError(f"boom token={secret} tail")
        except ValueError as e:
            returned = await errors.capture_exception(e, where="unit")
    assert returned == rid  # возвращает request_id (код инцидента)
    async with Session() as s:
        rows = (
            (await s.execute(select(ErrorEvent).where(ErrorEvent.request_id == rid)))
            .scalars()
            .all()
        )
    assert len(rows) == 1, "ровно одна запись ErrorEvent"
    row = rows[0]
    assert row.where == "unit" and row.exc_type == "ValueError"
    # golden rule #5: секрет НЕ попал ни в message, ни в traceback (редакция на границе БД)
    assert secret not in row.message
    assert secret not in (row.traceback or "")
    assert "***REDACTED***" in row.message


async def test_capture_exception_never_raises_on_persist_failure(monkeypatch):
    """Сбой персиста НЕ ломает обработку ошибки (best-effort): всё равно возвращает request_id."""
    from core import errors

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(errors, "_persist", boom)
    with context.request_scope("unit-test2") as rid:
        returned = await errors.capture_exception(ValueError("x"), where="unit")
    assert returned == rid


# ── core.texts.fmt_errors: вывод /diag ─────────────────────────────────────────────
def test_fmt_errors_ru_en_and_empty():
    from core import texts

    rows = [
        SimpleNamespace(
            request_id="abc12345",
            where="handler",
            exc_type="ValueError",
            message="boom happened",
            created_at=datetime(2026, 6, 29, 12, 0),
        )
    ]
    ru = texts.fmt_errors(rows, "ru")
    assert "abc12345" in ru and "ValueError" in ru and "Последние ошибки" in ru
    en = texts.fmt_errors(rows, "en")
    assert "Recent errors" in en and "abc12345" in en
    assert "пуст" in texts.fmt_errors([], "ru")
    assert "No errors" in texts.fmt_errors([], "en")
