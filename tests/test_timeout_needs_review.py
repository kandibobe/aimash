"""Денежный путь: таймаут/INTERNAL/DEADLINE во время SDK-мутации → needs_review, НЕ failed.

Баг (предсдаточный аудит 2026-07): asyncio.timeout поверх to_thread НЕ отменяет воркер — mutate мог
закоммититься на сервере, но in-process TimeoutError раньше ловился как обычная ошибка и писался
record_failure («не применено») → юзеру «❌ failed». Внимательный оператор пересоздавал бы кампанию →
дубль бюджета/сущности. Крашевый аналог (процесс упал в executing) уже лечит reconcile_stale_executing
→ mark_needs_review; здесь чиним IN-PROCESS ветку в bm._do_confirm той же честной терминалью.

Фейки aiogram — локальные (как tests/test_twofa_hardening.py), без сети/Telegram.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.ads_errors import is_outcome_unknown_after_mutate  # noqa: E402
from db.session import init_db  # noqa: E402


# ── Фейки aiogram (минимум для _do_confirm) ─────────────────────────────────────────
class FakeBot:
    async def send_chat_action(self, *a, **k):
        pass

    async def send_message(self, *a, **k):
        pass


class FakeMessage:
    def __init__(self, chat_id: int = 100, bot=None):
        self.chat = type("C", (), {"id": chat_id})()
        self.bot = bot
        self.edits: list = []

    async def answer(self, text: str = "", **kw):
        return self

    async def edit_text(self, text: str = "", **kw):
        self.edits.append(text)
        return self

    async def delete(self):
        pass


class FakeCallbackQuery:
    def __init__(self, message, uid: int = 100):
        self.message = message
        self.from_user = type("U", (), {"id": uid, "username": "op"})()

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        pass


def _fake_gads_exc(code_name: str) -> Exception:
    """Дакт-фейк GoogleAdsException: error_code_names(exc) вернёт {code_name} (см. core.ads_errors)."""
    err = type("E", (), {"error_code": type("Code", (), {"name": code_name})()})()
    failure = type("F", (), {"errors": [err]})()
    return type("GoogleAdsException", (Exception,), {"failure": failure})()


def _exec_raises(exc: BaseException):
    """Подмена execute_confirmed: столбит черновик (confirmed→executing, как реальный apply_*), затем
    роняет `exc` — воспроизводит «SDK-вызов уже пошёл и упал», оставляя черновик в executing."""

    async def _e(store, cid):
        snap = await store.get_confirmed(cid)
        claimed = await store.claim(cid, operation=snap.operation)  # confirmed → executing
        assert claimed is not None
        raise exc

    return _e


async def _save_pending(store: ConfirmStore, cid: str, chat_id: int, operation: str) -> None:
    await store.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary=f"{operation} X",
        chat_id=chat_id,
        user_initiated=True,
    )


# ── Классификатор «исход неизвестен» ────────────────────────────────────────────────
def test_classifier_flags_timeout_and_server_unknown_codes():
    assert is_outcome_unknown_after_mutate(TimeoutError("ads deadline")) is True
    assert is_outcome_unknown_after_mutate(_fake_gads_exc("INTERNAL_ERROR")) is True
    assert is_outcome_unknown_after_mutate(_fake_gads_exc("DEADLINE_EXCEEDED")) is True


def test_classifier_rejects_definite_not_applied_errors():
    # Ошибки ДО SDK-вызова (валидация/доступ/резолв) — исход ТОЧНО «не применено» → не needs_review.
    assert is_outcome_unknown_after_mutate(ValueError("бюджет должен быть > 0")) is False
    assert is_outcome_unknown_after_mutate(PermissionError("замок аккаунта")) is False
    assert is_outcome_unknown_after_mutate(_fake_gads_exc("USER_PERMISSION_DENIED")) is False
    assert is_outcome_unknown_after_mutate(_fake_gads_exc("RESOURCE_EXHAUSTED")) is False


# ── Интеграция: _do_confirm маршрутизирует по исходу ────────────────────────────────
async def test_timeout_during_mutation_routes_to_needs_review():
    """TimeoutError во время SDK → черновик НЕ 'failed', а 'needs_review' (исход неизвестен); юзеру
    показываем «сверьте перед повтором», а не «не удалось»."""
    await init_db()
    cid, chat = uuid.uuid4().hex, 7301
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "pause_campaign")
    msg = FakeMessage(chat_id=chat, bot=FakeBot())
    cq = FakeCallbackQuery(msg)

    orig = bm.execute_confirmed
    bm.execute_confirmed = _exec_raises(TimeoutError("ads deadline exceeded"))
    try:
        applied = await bm._do_confirm(cq, cid, state=None)
    finally:
        bm.execute_confirmed = orig

    assert applied is False
    assert (await store.get_confirmed(cid)).status == "needs_review"  # НЕ failed
    # UX: последнее сообщение под кнопкой — «исход неизвестен», не «не удалось»
    last = msg.edits[-1].lower()
    assert "неизвест" in last or "unknown" in last
    assert "не удалось" not in last and "failed to execute" not in last


async def test_server_internal_error_routes_to_needs_review():
    """Серверный INTERNAL_ERROR на мутации (исход мог примениться) → needs_review, не failed."""
    await init_db()
    cid, chat = uuid.uuid4().hex, 7302
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "resume_campaign")
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat, bot=FakeBot()))

    orig = bm.execute_confirmed
    bm.execute_confirmed = _exec_raises(_fake_gads_exc("INTERNAL_ERROR"))
    try:
        await bm._do_confirm(cq, cid, state=None)
    finally:
        bm.execute_confirmed = orig

    assert (await store.get_confirmed(cid)).status == "needs_review"


async def test_definite_failure_still_records_failed():
    """Регрессия: ошибка «точно не применено» (ValueError/доступ) по-прежнему → 'failed' (не
    размываем честный отказ в needs_review)."""
    await init_db()
    cid, chat = uuid.uuid4().hex, 7303
    store = ConfirmStore()
    await _save_pending(store, cid, chat, "pause_campaign")
    msg = FakeMessage(chat_id=chat, bot=FakeBot())
    cq = FakeCallbackQuery(msg)

    orig = bm.execute_confirmed
    bm.execute_confirmed = _exec_raises(ValueError("резолв кампании не удался"))
    try:
        await bm._do_confirm(cq, cid, state=None)
    finally:
        bm.execute_confirmed = orig

    assert (await store.get_confirmed(cid)).status == "failed"  # честный терминал «не применено»
    assert "неизвест" not in msg.edits[-1].lower()  # НЕ показали «исход неизвестен»
