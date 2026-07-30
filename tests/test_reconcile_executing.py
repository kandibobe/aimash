"""Тесты реконсиляции зависших executing-черновиков (§12: полнота audit-журнала).

Процесс может умереть ПОСЛЕ claim, посреди SDK-мутации — исход в Google Ads неизвестен.
reconcile_stale_executing помечает такие черновики терминальным needs_review (атомарный CAS,
НЕ авто-ретрай), пишет audit-строку, error_event и уведомляет владельца. Реальный ConfirmStore
на temp SQLite (conftest); SDK/сеть не трогаем.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from sqlalchemy import delete, select, update  # noqa: E402

from confirm.store import ConfirmStore, list_recent_audit  # noqa: E402
from db.models import AuditLog, ErrorEvent, Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402
from scheduler.jobs import reconcile_stale_executing  # noqa: E402

DRAFT = "7753643025"


@pytest.fixture(autouse=True)
async def _no_foreign_executing():
    """Джоба сканирует ВСЮ таблицу — значит предусловие «чужих зависших executing нет» обязан
    установить тест, а не надеяться на девственную БД.

    БД в прогоне одна на процесс (conftest), и соседние модули законно оставляют после себя
    executing-черновики со состаренным `decided_at`: `test_budget_blast_radius._backdate_decided`
    старит траты на 25 ч именно для того, чтобы проверить выпадение из суточного окна капа. При
    случайном порядке (`pytest-randomly`) такой сосед иногда идёт раньше — и `assert n == 1` ловил
    ЕГО черновик вместе с нашим: падало только в полном прогоне, в изоляции было зелено.

    Чистим ДО каждого теста и модуля целиком: `n == 0`-тесты («свежий не тронут», «pending и
    терминальные не тронуты») ломались бы тем же соседом.
    """
    await init_db()
    async with Session() as s:
        await s.execute(delete(Proposal).where(Proposal.status == "executing"))
        await s.commit()


class _FakeBot:
    """Минимальный бот: собирает send_message для проверки уведомления владельца."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kw) -> None:
        self.sent.append((chat_id, text))


async def _mk_executing(store: ConfirmStore, *, chat_id: int = 100, minutes_ago: int = 0) -> str:
    """pending → confirmed → claim (executing); опционально состарить decided_at."""
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT,
        params={"campaign": "X"},
        summary="s",
        chat_id=chat_id,
        user_initiated=True,
    )
    assert await store.confirm(cid, chat_id=chat_id, actor_user_id=7, actor_username="anton")
    assert await store.claim(cid, operation="update_budget") is not None
    if minutes_ago:
        backdated = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        async with Session() as s:
            await s.execute(
                update(Proposal).where(Proposal.confirmation_id == cid).values(decided_at=backdated)
            )
            await s.commit()
    return cid


async def _status(cid: str) -> str:
    async with Session() as s:
        p = (await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))).scalar_one()
        return p.status


async def test_stale_executing_marked_needs_review_with_audit():
    await init_db()
    store = ConfirmStore()
    cid = await _mk_executing(store, chat_id=101, minutes_ago=90)

    bot = _FakeBot()
    n = await reconcile_stale_executing(bot, stale_minutes=30)

    assert n == 1
    assert await _status(cid) == "needs_review"
    # audit-строка needs_review с причиной (§12: событие видно в журнале)
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.confirmation_id == cid, AuditLog.status == "needs_review"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert "неизвестен" in str(rows[0].result.get("error", "")).casefold()
    # уведомление владельцу (chat_id черновика), без авто-повтора
    assert bot.sent and bot.sent[0][0] == 101
    assert "НЕИЗВЕСТНО" in bot.sent[0][1]


async def test_fresh_executing_untouched():
    await init_db()
    store = ConfirmStore()
    cid = await _mk_executing(store, minutes_ago=5)  # свежий: живой процесс ещё работает

    n = await reconcile_stale_executing(None, stale_minutes=30)

    assert n == 0
    assert await _status(cid) == "executing"


async def test_pending_and_terminal_untouched():
    await init_db()
    store = ConfirmStore()
    # pending (не подтверждён) — не трогаем даже старый
    cid_p = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid_p,
        operation="update_budget",
        customer_id=DRAFT,
        params={},
        summary="s",
        chat_id=1,
        user_initiated=True,
    )
    # applied (терминальный)
    cid_a = await _mk_executing(store, minutes_ago=90)
    await store.finalize(cid_a, result={"ok": True})

    n = await reconcile_stale_executing(None, stale_minutes=30)

    assert await _status(cid_p) == "pending"
    assert await _status(cid_a) == "applied"
    # cid_a финализирован ДО джобы → его она не считает; других executing нет
    assert n == 0


async def test_mark_needs_review_loses_race_to_finalize():
    """Гонка: живой процесс успел finalize — CAS mark_needs_review проигрывает без спурьёзной
    audit-строки (rowcount=0 → False)."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk_executing(store, minutes_ago=90)
    await store.finalize(cid, result={"ok": True})  # «живой процесс» успел первым

    ok = await store.mark_needs_review(cid, error="stale")

    assert ok is False
    assert await _status(cid) == "applied"
    async with Session() as s:
        nr = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.confirmation_id == cid, AuditLog.status == "needs_review"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert nr == []  # без спурьёзной строки


async def test_no_reclaim_after_needs_review():
    """needs_review терминален: claim (требует confirmed) не «воскрешает» черновик — replay-защита."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk_executing(store, minutes_ago=90)
    assert await store.mark_needs_review(cid, error="stale") is True

    assert await store.claim(cid, operation="update_budget") is None
    assert await _status(cid) == "needs_review"


async def test_needs_review_visible_in_journal():
    await init_db()
    store = ConfirmStore()
    cid = await _mk_executing(store, minutes_ago=90)
    assert await store.mark_needs_review(cid, error="исход неизвестен") is True

    events = await list_recent_audit(50)
    ev = next(e for e in events if e.confirmation_id == cid)
    assert ev.status == "needs_review"

    from core import texts

    out = texts.fmt_journal([ev])
    assert "требует проверки" in out
    assert "неизвестен" in out  # причина показана (как для failed)


async def test_error_event_written_for_diag():
    await init_db()
    store = ConfirmStore()
    await _mk_executing(store, minutes_ago=90)

    n = await reconcile_stale_executing(None, stale_minutes=30)
    assert n == 1

    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(ErrorEvent).where(ErrorEvent.where == "scheduler:reconcile-executing")
                )
            )
            .scalars()
            .all()
        )
    assert rows, "capture_exception должен записать error_event для /diag"
