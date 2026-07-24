"""Тесты журнала изменений (ТЗ §12/§18): confirm.store.list_recent_audit читает audit_log
(только applied/failed/rejected, reverse-chron, «кто» восстановлен из confirmed-строки), а
texts.fmt_journal рендерит. Реальный ConfirmStore на temp SQLite (conftest); сеть/SDK не трогаем.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import texts  # noqa: E402
from confirm.store import AuditEvent, ConfirmStore, list_recent_audit  # noqa: E402
from db.session import init_db  # noqa: E402

DRAFT = "7753643025"


async def _mk(store: ConfirmStore, operation: str, *, chat_id: int = 100) -> str:
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=DRAFT,
        params={"campaign": "X"},
        summary="s",
        chat_id=chat_id,
        user_initiated=True,
    )
    return cid


async def test_list_recent_audit_outcomes_with_actor_resolution():
    await init_db()
    store = ConfirmStore()

    # applied: confirm (actor anton) → claim → finalize (actor=NULL на applied-строке)
    cid_a = await _mk(store, "update_budget")
    assert await store.confirm(cid_a, chat_id=100, actor_user_id=7, actor_username="anton")
    await store.claim(cid_a, operation="update_budget")
    await store.finalize(cid_a, result={"ok": True})

    # rejected: reject (actor bob прямо на rejected-строке)
    cid_r = await _mk(store, "pause_campaign")
    await store.reject(cid_r, chat_id=100, actor_user_id=8, actor_username="bob")

    # failed: confirm (actor cara) → record_failure (actor=NULL на failed-строке)
    cid_f = await _mk(store, "update_bid")
    assert await store.confirm(cid_f, chat_id=100, actor_user_id=9, actor_username="cara")
    await store.record_failure(cid_f, error="boom details")

    events = await list_recent_audit(15)
    by_cid = {e.confirmation_id: e for e in events}

    # Все три исхода присутствуют; «кто» восстановлен для applied/failed из confirmed-строки.
    assert by_cid[cid_a].status == "applied" and by_cid[cid_a].operation == "update_budget"
    assert by_cid[cid_a].actor_username == "anton"
    assert by_cid[cid_r].status == "rejected" and by_cid[cid_r].actor_username == "bob"
    assert by_cid[cid_f].status == "failed" and by_cid[cid_f].actor_username == "cara"

    # Промежуточные confirmed-строки НЕ показываются как события журнала.
    assert all(e.status in ("applied", "failed", "rejected", "needs_review") for e in events)
    # failed несёт причину (редактированную на записи) в result.
    assert isinstance(by_cid[cid_f].result, dict) and by_cid[cid_f].result.get("error")


def test_fmt_journal_empty():
    out = texts.fmt_journal([])
    assert "Журнал изменений" in out
    assert "пусто" in out.lower()


def test_fmt_journal_renders_events():
    events = [
        AuditEvent(
            datetime(2026, 6, 28, 11, 0, tzinfo=timezone.utc),
            "applied",
            "update_budget",
            7,
            "anton",
            {"ok": True},
            "c1",
        ),
        AuditEvent(
            datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc),
            "failed",
            "update_bid",
            9,
            "cara",
            {"error": "boom details"},
            "c2",
        ),
    ]
    out = texts.fmt_journal(events)
    assert "бюджет" in out and "применено" in out  # человекочитаемая операция + статус
    assert "ставка CPC" in out and "ошибка" in out
    assert "@anton" in out and "@cara" in out  # «кто»
    assert "boom details" in out  # причина ошибки показана
    assert "28.06" in out  # дата события
