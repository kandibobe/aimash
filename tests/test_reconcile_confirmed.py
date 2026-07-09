"""A6: реконсиляция черновиков, зависших в 'confirmed' (рестарт МЕЖДУ confirm() и claim()).

Пока статус 'confirmed', SDK НЕ вызывался (claim confirmed→executing идёт прямо перед SDK) →
изменение ТОЧНО не применено. reconcile_stale_confirmed помечает такие 'failed' (атомарный CAS)
+ audit + уведомление «не применено, повтори». Раньше этот статус не покрывала ни одна джоба
(cleanup — 'pending', reconcile_stale_executing — 'executing') → черновик висел вечно.

Плюс мета-гард: каждый нетерминальный статус (pending/confirmed/executing) покрыт своей джобой.
Реальный ConfirmStore на temp SQLite (conftest); SDK/сеть не трогаем.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update  # noqa: E402

from confirm.store import ConfirmStore  # noqa: E402
from db.models import AuditLog, Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402
from scheduler.jobs import reconcile_stale_confirmed  # noqa: E402

DRAFT = "7753643025"


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kw) -> None:
        self.sent.append((chat_id, text))


async def _mk_confirmed(store: ConfirmStore, *, chat_id: int = 100, minutes_ago: int = 0) -> str:
    """pending → confirmed (БЕЗ claim — застряли ДО исполнения); опционально состарить decided_at."""
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


async def test_stale_confirmed_marked_failed_with_audit_and_notice():
    await init_db()
    store = ConfirmStore()
    cid = await _mk_confirmed(store, chat_id=201, minutes_ago=90)

    bot = _FakeBot()
    n = await reconcile_stale_confirmed(bot, stale_minutes=30)

    assert n == 1
    assert await _status(cid) == "failed"  # НЕ применено — честный failed (не needs_review)
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.confirmation_id == cid, AuditLog.status == "failed"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert "не применено" in str(rows[0].result.get("error", "")).casefold()
    assert bot.sent and bot.sent[0][0] == 201
    assert "НЕ применено" in bot.sent[0][1]


async def test_fresh_confirmed_untouched():
    """Свежий confirmed (живой процесс вот-вот вызовет claim) — не трогаем."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk_confirmed(store, minutes_ago=2)

    n = await reconcile_stale_confirmed(None, stale_minutes=30)

    assert n == 0
    assert await _status(cid) == "confirmed"


async def test_mark_confirmed_failed_loses_race_to_claim():
    """Гонка A6: живой процесс успел claim (confirmed→executing) — CAS mark_confirmed_failed
    проигрывает (rowcount=0 → False), исполняемый черновик не трогаем, спурьёзной audit нет."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk_confirmed(store, minutes_ago=90)
    assert await store.claim(cid, operation="update_budget") is not None  # живой claim победил

    ok = await store.mark_confirmed_failed(cid, error="stale")

    assert ok is False
    assert await _status(cid) == "executing"  # исполнение продолжается
    async with Session() as s:
        failed = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.confirmation_id == cid, AuditLog.status == "failed"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert failed == []


async def test_terminal_and_pending_untouched():
    await init_db()
    store = ConfirmStore()
    # pending — покрывает cleanup_stale_proposals, не эта джоба
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
    n = await reconcile_stale_confirmed(None, stale_minutes=30)
    assert n == 0
    assert await _status(cid_p) == "pending"


def test_every_nonterminal_status_has_a_reconciler():
    """Мета-гард класса A6: каждый НЕтерминальный статус proposal покрыт своей джобой-реконсилятором
    (новый статус без реконсиляции — «вечно висящий» черновик — валит этот тест)."""
    import inspect

    import scheduler.jobs as J

    # статус → подстрока-маркер в теле джобы, которая его обрабатывает
    coverage = {
        "pending": "cleanup_stale_proposals",
        "confirmed": "reconcile_stale_confirmed",
        "executing": "reconcile_stale_executing",
    }
    for status, fn_name in coverage.items():
        fn = getattr(J, fn_name, None)
        assert fn is not None, f"нет джобы {fn_name} для статуса {status}"
        src = inspect.getsource(fn)
        assert f'"{status}"' in src or f"'{status}'" in src, (
            f"джоба {fn_name} должна фильтровать статус {status}"
        )
