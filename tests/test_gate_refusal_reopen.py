"""MAJOR-1 (ревью 2026-07-30): отказ гейта ДО claim на ЖИВОМ кнопочном пути НЕ сжигает черновик.

До фикса `bm._do_confirm` ловил Exception целиком и звал record_failure — confirmed → failed:
отказ рубильника (BZ-1) или капа (B1-4) СЖИГАЛ одноразовое подтверждение, а свойство «не сжигает»
держал только юнит, зовущий _require_confirmation напрямую (мимо живого пути). Теперь GateRefusal
(core.killswitch) маршрутизируется отдельной веткой: CAS-возврат confirmed → pending
(`ConfirmStore.reopen`) + восстановление карточки с кнопками — после снятия причины ТО ЖЕ «да»
проходит штатной дорогой (2FA, владелец, TTL, L3 — заново).

Исполнитель здесь — тонкий стаб поверх НАСТОЯЩЕГО гейта `_require_confirmation` (рубильник, кап,
claim — реальные; фейкается только SDK-слой). Фейки aiogram — локальные, как
tests/test_timeout_needs_review.py, без сети/Telegram.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.mutations import _require_confirmation  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from conftest import attested  # noqa: E402
from core.killswitch import ENV_VAR, GateRefusal  # noqa: E402
from db.models import AuditLog  # noqa: E402
from db.session import Session, init_db  # noqa: E402

OWNER = 100
OP = "update_budget"
CARD_HTML = "<b>Бюджет X</b>: было 10 → станет 12"


# ── Фейки aiogram (минимум для _do_confirm + восстановление карточки) ───────────────
class FakeBot:
    async def send_chat_action(self, *a, **k):
        pass

    async def send_message(self, *a, **k):
        pass


class FakeMessage:
    def __init__(self, chat_id: int = OWNER, bot=None):
        self.chat = type("C", (), {"id": chat_id})()
        self.bot = bot
        self.edits: list[tuple[str, object]] = []  # (text, reply_markup)
        self.html_text = CARD_HTML
        self.reply_markup = object()  # маркер «кнопки ✅/❌ исходной карточки»

    async def answer(self, text: str = "", **kw):
        return self

    async def edit_text(self, text: str = "", **kw):
        self.edits.append((text, kw.get("reply_markup")))
        return self


class FakeCallbackQuery:
    def __init__(self, message, uid: int = OWNER):
        self.message = message
        self.from_user = type("U", (), {"id": uid, "username": "op"})()

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        pass


def _exec_via_real_gate():
    """Исполнитель = НАСТОЯЩИЙ гейт + фейковый SDK: _require_confirmation (рубильник → freshness →
    кап → событие → claim) реальный, а вместо вызова Google Ads — сразу finalize."""

    async def _e(store, cid):
        snap = await store.get_confirmed(cid)
        await _require_confirmation(store, cid, snap.operation)
        await store.finalize(cid, result={"ok": True})
        return {"ok": True}

    return _e


async def _pending_draft(store: ConfirmStore, chat_id: int) -> str:
    """Черновик в 'pending': сам confirm (pending → confirmed) делает _do_confirm — как живой ✅."""
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation=OP,
        customer_id=DRAFT_ACCOUNT_ID,
        params=attested(
            {"campaign": "X", "mode": "increase_by_percent", "value": 20.0},
            {"kind": "budget", "before_micros": 10_000_000, "after_micros": 12_000_000},
        ),
        summary="s",
        chat_id=chat_id,
        user_initiated=True,
    )
    return cid


async def _audit_statuses(cid: str) -> list[str]:
    async with Session() as s:
        rows = (
            (await s.execute(select(AuditLog.status).where(AuditLog.confirmation_id == cid)))
            .scalars()
            .all()
        )
    return list(rows)


# ── Живой путь: рубильник → reopen → то же «да» после снятия ────────────────────────
async def test_killswitch_on_live_path_reopens_draft_then_same_yes_applies(monkeypatch):
    """Полный round-trip MAJOR-1: (1) рубильник включён → _do_confirm НЕ жжёт черновик
    (pending, не failed), восстанавливает карточку с исходными кнопками и пишет audit
    'reopened'; (2) рубильник снят → ТО ЖЕ «да» на той же карточке применяется."""
    await init_db()
    chat = 7401
    store = ConfirmStore()
    cid = await _pending_draft(store, chat)
    msg = FakeMessage(chat_id=chat, bot=FakeBot())

    orig = bm.execute_confirmed
    bm.execute_confirmed = _exec_via_real_gate()
    try:
        monkeypatch.setenv(ENV_VAR, "1")
        applied = await bm._do_confirm(FakeCallbackQuery(msg), cid, state=None)
        assert applied is False
        snap = await store.get_confirmed(cid)
        assert snap.status == "pending"  # НЕ failed: подтверждение не сожжено
        assert "reopened" in await _audit_statuses(cid)  # след «заявка была, отказал гейт»
        text, markup = msg.edits[-1]
        assert "рубильник" in text.lower()  # причина — человеку, на карточке
        assert CARD_HTML in text  # исходная карточка восстановлена…
        assert markup is msg.reply_markup  # …вместе с кнопками ✅/❌
        assert bm._LAST_PENDING.get(chat) == cid  # текстовое «да» снова знает карточку

        monkeypatch.delenv(ENV_VAR)
        applied = await bm._do_confirm(FakeCallbackQuery(msg), cid, state=None)
        assert applied is True  # ТО ЖЕ «да» на той же карточке — весь CAS-путь заново
        assert (await store.get_confirmed(cid)).status == "applied"
    finally:
        bm.execute_confirmed = orig


async def test_plain_permission_error_still_burns_to_failed(monkeypatch):
    """Регрессия-граница: обычный PermissionError (дефект черновика/замок — НЕ GateRefusal)
    по-прежнему жжёт черновик в failed: reopen — только для внешних временных причин."""
    await init_db()
    chat = 7402
    store = ConfirmStore()
    cid = await _pending_draft(store, chat)
    msg = FakeMessage(chat_id=chat, bot=FakeBot())

    async def _raise_plain(store_, cid_):
        raise PermissionError("замок аккаунта: id вне потолка")

    orig = bm.execute_confirmed
    bm.execute_confirmed = _raise_plain
    try:
        assert await bm._do_confirm(FakeCallbackQuery(msg), cid, state=None) is False
    finally:
        bm.execute_confirmed = orig
    assert (await store.get_confirmed(cid)).status == "failed"


# ── CAS-свойства reopen (стор-уровень) ──────────────────────────────────────────────
async def test_reopen_cas_only_from_confirmed():
    """reopen — CAS строго из 'confirmed': pending → False (нечего возвращать), после claim
    (executing) → False (исполняемое не трогаем), повторный reopen → False (одноразовость)."""
    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation=OP,
        customer_id=DRAFT_ACCOUNT_ID,
        params=attested({"campaign": "X"}, {"kind": "budget"}),
        summary="s",
        chat_id=OWNER,
        user_initiated=True,
    )
    assert await store.reopen(cid, reason="r") is False  # из pending — нет

    assert await store.confirm(cid, chat_id=OWNER) is True
    assert await store.reopen(cid, reason="рубильник") is True  # confirmed → pending
    assert (await store.get_confirmed(cid)).status == "pending"
    assert await store.reopen(cid, reason="r") is False  # одноразово

    assert await store.confirm(cid, chat_id=OWNER) is True
    assert await store.claim(cid, operation=OP) is not None  # confirmed → executing
    assert await store.reopen(cid, reason="r") is False  # исполняемое не трогаем


async def test_gate_refusal_is_permission_error():
    """GateRefusal — подкласс PermissionError: старые ловцы PermissionError (MCP-слой, скрипты)
    не теряют отказ гейта; специальную маршрутизацию получает только тот, кто её знает."""
    assert issubclass(GateRefusal, PermissionError)
