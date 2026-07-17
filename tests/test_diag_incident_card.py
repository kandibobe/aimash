"""Замечание 9 (2026-07-17): /diag — карточка инцидента вместо сырого трейсбека в чат.

Дефекты и гарды: (1) кнопка «🔍 -» из сентинела пустого контекста (core.context, джобы) —
фильтруется; (2) request_id НЕ уникален — detail открывал не тот инцидент, адресация по PK
(DiagCB.eid); (3) сырой traceback в чат — теперь карточка (тип · где · аккаунт · message),
полный трейс только в «📎 Экспорт» (admin-only); (4) redact_text повторно на ВЫДАЧЕ
(карточка + экспорт) — второй рубеж для рядов, записанных до ужесточения правил редакции.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.handlers.commands as cmds  # noqa: E402
import bot.main as bm  # noqa: E402
from bot import texts  # noqa: E402
from bot.callbacks import DiagCB  # noqa: E402
from bot.keyboards import diag_kb  # noqa: E402
from reports.diag_export import build_error_events_text  # noqa: E402


def _row(**kw):
    d = dict(
        id=1,
        request_id="req1",
        chat_id=1,
        customer_id="5437782039",
        where="handler",
        exc_type="RuntimeError",
        message="boom",
        traceback="Traceback (most recent call last): ...",
        created_at=None,
    )
    d.update(kw)
    return SimpleNamespace(**d)


def _detail_cbs(markup) -> list[DiagCB]:
    return [
        DiagCB.unpack(b.callback_data)
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data and ":detail:" in b.callback_data
    ]


# ── (1) сентинел «-» не порождает кнопку ────────────────────────────────────────────────
def test_diag_kb_skips_sentinel_rid():
    """request_id='-' (ошибка вне апдейта: пустой core.context) — не код инцидента, кнопка
    «🔍 -» была мусором. Кнопки строятся только для настоящих rid."""
    rows = [_row(id=2, request_id="-"), _row(id=1, request_id="req1")]
    cbs = _detail_cbs(diag_kb(rows, today=False, is_admin=True))
    assert [cb.rid for cb in cbs] == ["req1"]


# ── (2) detail адресует конкретный ряд по PK ────────────────────────────────────────────
def test_diag_kb_detail_carries_pk():
    """rid дублируется между рядами (несколько ошибок одного апдейта) — кнопка одна (дедуп),
    несёт PK СВЕЖАЙШЕГО ряда (rows reverse-chron ⇒ первый выигрывает)."""
    rows = [_row(id=7, request_id="dup"), _row(id=3, request_id="dup")]
    cbs = _detail_cbs(diag_kb(rows, today=False, is_admin=True))
    assert len(cbs) == 1
    assert cbs[0].eid == 7


async def test_load_error_detail_prefers_pk():
    """eid>0 → ряд по PK (не свежайший-по-rid); eid=0 (старые кнопки) — прежний фолбэк по rid."""
    from db.models import ErrorEvent
    from db.session import Session, init_db

    await init_db()
    async with Session() as s:
        older = ErrorEvent(request_id="dup16", where="w1", exc_type="ValueError", message="older")
        newer = ErrorEvent(request_id="dup16", where="w2", exc_type="KeyError", message="newer")
        s.add_all([older, newer])
        await s.commit()
        old_id = older.id

    by_pk = await bm._load_error_detail("dup16", eid=old_id)
    assert by_pk is not None and by_pk.message == "older"  # именно адресованный ряд
    by_rid = await bm._load_error_detail("dup16")
    assert by_rid is not None and by_rid.message == "newer"  # фолбэк — свежайший (как раньше)


@contextmanager
def _patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


async def test_diag_detail_handler_passes_eid():
    """on_diag_cb прокидывает eid из callback_data в _load_error_detail (иначе PK-адресация
    мертва: кнопка несёт eid, а хендлер грузил бы по rid)."""
    seen: dict = {}

    async def _admin(_uid):
        return True

    async def _load(rid, eid=0):
        seen.update(rid=rid, eid=eid)
        return None  # ветка not_found — _safe_edit с текстом, разметка не нужна

    async def _edit(_cq, _text, **kw):
        return None

    cq = SimpleNamespace(message=None, from_user=SimpleNamespace(id=1, username="t"), answer=None)

    async def _cq_answer(*a, **kw):
        return None

    cq.answer = _cq_answer
    with (
        _patched(cmds, "_is_admin", _admin),
        _patched(bm, "_load_error_detail", _load),
        _patched(bm, "_safe_edit", _edit),
    ):
        await cmds.on_diag_cb(cq, DiagCB(action="detail", rid="req1", eid=42))
    assert seen == {"rid": "req1", "eid": 42}


# ── (3) карточка вместо трейсбека ───────────────────────────────────────────────────────
def test_error_card_is_card_not_traceback():
    """Карточка несёт тип/где/аккаунт/message и подсказку про Экспорт; сырой traceback — НЕТ."""
    row = _row(traceback="TRACE_MARKER_LINE\n  File ...", message="читаемое сообщение")
    card = texts.fmt_error_detail(row)
    assert "TRACE_MARKER_LINE" not in card  # трейсбек больше не льётся в чат
    for part in ("RuntimeError", "handler", "5437782039", "читаемое сообщение", "Экспорт"):
        assert part in card, f"в карточке нет «{part}»"


# ── (4) redact_text на выдаче (карточка + экспорт) ──────────────────────────────────────
_RAW_REFRESH = "1//AbCdEfGhIjKlMnOpQrst"  # паттерн OAuth refresh (1// + 15+ симв.)
_RAW_SK = "sk-abcdefghijklmnop123"  # OpenAI/OpenRouter-стиль ключ (sk- + 16+)
_RAW_BEARER = "Bearer ya29.a0AfH6SMBxyz"  # Authorization: Bearer <...>


def test_error_card_redacts_on_output():
    """Ряд, записанный ДО ужесточения правил редакции, может нести секрет — карточка скрабит
    повторно (идемпотентно для уже чистых)."""
    card = texts.fmt_error_detail(_row(message=f"fail: {_RAW_REFRESH} и {_RAW_SK}"))
    assert _RAW_REFRESH not in card and _RAW_SK not in card
    assert "REDACTED" in card


def test_export_redacts_on_output():
    """Экспорт (.txt) скрабит message И traceback повторно — та же защита остаточных рядов."""
    dump = build_error_events_text(
        [_row(message=f"msg {_RAW_SK}", traceback=f"call failed: {_RAW_BEARER}\n{_RAW_REFRESH}")]
    )
    for raw in (_RAW_SK, _RAW_REFRESH, "ya29.a0AfH6SMBxyz"):
        assert raw not in dump
    assert "REDACTED" in dump
