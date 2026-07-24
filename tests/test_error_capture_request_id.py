"""Регресс §15: «код инцидента» (request_id) доживает до глобального on_error.

Класс бага: aiogram зовёт on_error УЖЕ ПОСЛЕ того, как TraceMiddleware.finally сбросил contextvar
(ErrorsMiddleware — снаружи router-middleware), поэтому get_context() там вернул бы дефолт '-', и
пользователь видел карточку err_unexpected с бесполезным «код -», а ошибку нельзя было связать с
логом/`/diag`. Фикс: stash_context_on прикрепляет снимок req-контекста к исключению (внутри живого
scope), capture_exception его восстанавливает на время лога/персиста.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.errors as E  # noqa: E402
from core.context import (  # noqa: E402
    get_context,
    reset_context,
    set_context,
    stash_context_on,
    stashed_context,
)


async def test_capture_recovers_stashed_request_id(monkeypatch):
    """aiogram-путь: контекст уже сброшен ('-'), но исключение несёт снимок → код реальный, не '-'."""
    captured: dict = {}

    async def _fake_persist(exc, *, where, request_id, chat_id, customer_id=None):
        captured.update(request_id=request_id, chat_id=chat_id)

    monkeypatch.setattr(E, "_persist", _fake_persist)

    exc = ValueError("boom")
    # То, что делает TraceMiddleware внутри живого scope перед re-raise:
    tok = set_context(request_id="abcd1234", chat_id=42, operation="message")
    try:
        stash_context_on(exc)
    finally:
        reset_context(tok)  # scope закрыт — contextvar снова дефолтный '-'
    assert get_context().request_id == "-"

    code = await E.capture_exception(exc, where="handler")

    assert code == "abcd1234"  # НЕ '-' — восстановлен из снимка
    assert captured["request_id"] == "abcd1234"  # error_events тоже получил реальный id
    assert captured["chat_id"] == 42
    assert get_context().request_id == "-"  # восстановление временное — после capture снова дефолт


async def test_capture_without_stash_uses_live_context(monkeypatch):
    """Scheduler-путь: capture зовётся ВНУТРИ живого scope, снимка нет → берётся живой ctx (не '-')."""
    captured: dict = {}

    async def _fake_persist(exc, *, where, request_id, chat_id, customer_id=None):
        captured.update(request_id=request_id)

    monkeypatch.setattr(E, "_persist", _fake_persist)

    tok = set_context(request_id="live5678", chat_id=7, operation="scheduler:report")
    try:
        code = await E.capture_exception(ValueError("x"), where="scheduler:report")
    finally:
        reset_context(tok)

    assert code == "live5678"
    assert captured["request_id"] == "live5678"


async def test_stashed_context_absent_is_none():
    """Обычное исключение без снимка → None (capture тогда падает на живой контекст)."""
    assert stashed_context(ValueError("x")) is None


async def test_live_scope_wins_over_stale_stash(monkeypatch):
    """Если контекст ЖИВОЙ (не '-'), снимок из исключения не подменяет его — восстановление только
    когда текущий контекст пуст. Защита от подмены свежего request_id устаревшим снимком."""
    captured: dict = {}

    async def _fake_persist(exc, *, where, request_id, chat_id, customer_id=None):
        captured.update(request_id=request_id)

    monkeypatch.setattr(E, "_persist", _fake_persist)

    exc = ValueError("boom")
    tok_old = set_context(request_id="oldstash", chat_id=1)
    try:
        stash_context_on(exc)
    finally:
        reset_context(tok_old)

    tok_live = set_context(request_id="livenow0", chat_id=2)
    try:
        code = await E.capture_exception(exc, where="handler")
    finally:
        reset_context(tok_live)

    assert code == "livenow0"  # живой контекст победил устаревший снимок
    assert captured["request_id"] == "livenow0"
