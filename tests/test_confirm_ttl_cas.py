"""Волна 1.2: TTL подтверждения энфорсит CAS, а не фоновая джоба.

Раньше срок жизни черновика держал ровно один энфорсер — `scheduler.jobs.cleanup_stale_proposals`.
Джоба не поднялась / упала / отстала на интервал ⇒ вчерашний черновик оставался исполнимым, и
человек подтверждал «было→станет», снятое неизвестно когда. Здесь джоба НЕ вызывается ни в одном
тесте — это и есть суть проверки: отказ обязан приходить от самого перехода статуса.

Симметрично проверяется обратная половина (свежий черновик проходит весь путь) — иначе «отказывать
всем» удовлетворило бы инвариант выше. И отдельно — что `reject` условие возраста НЕ получил: это
инструмент уборщика, лишись он права трогать просроченные строки, они висели бы в `pending` вечно.

Реальный ConfirmStore на temp SQLite (conftest), как tests/test_reject_cas.py.
"""

from __future__ import annotations

import inspect
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update  # noqa: E402

from ads.mutations import _require_confirmation  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.config import settings  # noqa: E402
from db.models import AuditLog, Proposal  # noqa: E402
from db.session import Session, db_dt, init_db  # noqa: E402

DRAFT = "7753643025"
OWNER = 100
OP = "update_budget"


async def _mk(store: ConfirmStore, *, chat_id: int = OWNER) -> str:
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation=OP,
        customer_id=DRAFT,
        params={"campaign": "X"},
        summary="s",
        chat_id=chat_id,
        user_initiated=True,
    )
    return cid


async def _backdate(cid: str, *, hours_over_ttl: float = 1.0) -> None:
    """Состарить черновик на TTL + запас. Возраст двигаем в БД, а не часами в коде: границу считает
    `_ttl_boundary()` по живому `settings.proposal_ttl_hours`, и тест не должен знать её числом."""
    old = datetime.now(timezone.utc) - timedelta(
        hours=float(settings.proposal_ttl_hours) + hours_over_ttl
    )
    async with Session() as s:
        await s.execute(
            update(Proposal).where(Proposal.confirmation_id == cid).values(created_at=db_dt(old))
        )
        await s.commit()


async def _status(cid: str) -> str:
    async with Session() as s:
        p = (await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))).scalar_one()
        return p.status


async def _audit_count(cid: str, status: str) -> int:
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.confirmation_id == cid, AuditLog.status == status
                    )
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


# ── Отказ приходит от CAS, джоба не участвует ───────────────────────────────────────
async def test_confirm_refuses_expired_pending_without_any_janitor_run():
    """Просроченный pending НЕ подтверждается, хотя `cleanup_stale_proposals` не отработал."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)
    await _backdate(cid)

    assert await store.confirm(cid, chat_id=OWNER) is False
    assert await _status(cid) == "pending"  # статус не тронут — это работа уборщика
    assert await _audit_count(cid, "confirmed") == 0  # ни спурьёзной audit-строки


async def test_claim_refuses_expired_confirmed_without_any_janitor_run():
    """Второй рубеж: черновик подтверждён вовремя, а исполнить пытаются назавтра (процесс упал между
    confirm и claim и поднялся спустя сутки). claim обязан вернуть None, SDK не вызывается."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)
    assert await store.confirm(cid, chat_id=OWNER) is True  # подтверждён, пока свежий
    await _backdate(cid)

    assert await store.claim(cid, operation=OP) is None
    assert await _status(cid) == "confirmed"  # не «застолблён» — в executing не ушёл


async def test_expired_confirmation_raises_in_mutation_gate():
    """Тот же отказ на уровне денежного гейта: `_require_confirmation` (его зовёт каждый `apply_*`
    первой строкой после валидации) превращает None от claim в PermissionError."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)
    assert await store.confirm(cid, chat_id=OWNER) is True
    await _backdate(cid)

    with pytest.raises(PermissionError):
        await _require_confirmation(store, cid, OP)


# ── Обратная половина: свежий черновик проходит весь путь ───────────────────────────
async def test_fresh_draft_confirms_and_claims_as_before():
    """Иначе «отказывать всем» удовлетворило бы инварианты выше."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)

    assert await store.confirm(cid, chat_id=OWNER) is True
    snap = await store.claim(cid, operation=OP)
    assert snap is not None and snap.status == "executing"
    assert snap.user_initiated is True  # бит провенанса TTL-гард не трогает


async def test_draft_just_inside_ttl_still_confirms():
    """Граница — не «почти сутки нельзя»: черновик младше TTL проходит. Отсекает случайный
    инвертированный знак сравнения (`<=` вместо `>=`), при котором свежее было бы мёртвым."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)
    await _backdate(cid, hours_over_ttl=-0.5)  # TTL − 30 мин: ещё жив

    assert await store.confirm(cid, chat_id=OWNER) is True


# ── Уборщик обязан сохранить право трогать просроченные строки ──────────────────────
async def test_reject_still_works_on_expired_draft():
    """`reject` условие возраста НЕ получил намеренно: через него `cleanup_stale_proposals` метит
    мёртвые черновики терминальным `rejected` и освобождает временные медиа. Появись TTL и здесь —
    просроченные строки висели бы в `pending` вечно, а кадры §19/§11 осиротели бы на диске."""
    await init_db()
    store = ConfirmStore()
    cid = await _mk(store)
    await _backdate(cid)

    assert await store.reject(cid, chat_id=OWNER) is True
    assert await _status(cid) == "rejected"


# ── Мета-гард: условие возраста живёт в WHERE, а не в проверке после выборки ────────
@pytest.mark.parametrize("method", ["confirm", "claim"])
def test_ttl_condition_is_part_of_the_cas_where(method: str):
    """Зеркало мета-гарда CAS из tests/test_reject_cas.py. Проверка возраста ПОСЛЕ выборки —
    это TOCTOU: между «увидели, что свежий» и «застолбили» на Postgres влезает параллельный
    воркер. Рефактор, вынесший условие из WHERE, валит тест."""
    src = inspect.getsource(getattr(ConfirmStore, method))
    assert "Proposal.created_at" in src and "_ttl_boundary()" in src, (
        f"ConfirmStore.{method} должен держать условие возраста внутри update…where "
        "(TTL энфорсит CAS, а не фоновая джоба — см. Волна 1.2)"
    )


def test_reject_has_no_ttl_condition():
    """Обратный мета-гард к тесту выше: право уборщика на просроченные строки — не случайность."""
    src = inspect.getsource(ConfirmStore.reject)
    assert "_ttl_boundary()" not in src, (
        "reject не должен фильтровать по возрасту: им уборщик закрывает именно просроченные "
        "черновики (cleanup_stale_proposals)"
    )
