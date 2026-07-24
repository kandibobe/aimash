"""1.7 (аудит 2026-07-06): «частые аккаунты» оператора в пикерах (⭐-закрепы).

Инварианты: агрегат по СВОЕЙ активности (audit_log ∪ proposals ∪ recommendation) за 30 дней,
Draft не «частый», старые строки не считаются, TTL-кэш работает, сбой БД → []; клавиатура
закрепляет ⭐ только для аккаунтов, ПРИСУТСТВУЮЩИХ в rows (read-замок не обходится), без дубля
с «↻ как в прошлый раз».
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot.keyboards import report_accounts_kb  # noqa: E402

A, B, C = "1112223334", "2223334445", "3334445556"


async def _seed(chat: int):
    from sqlalchemy import delete

    from db.models import AuditLog, Proposal, Recommendation
    from db.session import Session, init_db

    await init_db()
    async with Session() as s:
        for model in (AuditLog, Proposal, Recommendation):
            await s.execute(delete(model).where(model.chat_id == chat))
        # A: 3 события (2 audit + 1 proposal); B: 1 событие; Draft: шум (не считается);
        # C: старое событие (за окном 30 дней).
        s.add(
            AuditLog(
                confirmation_id=f"c1-{chat}",
                operation="x",
                customer_id=A,
                chat_id=chat,
                status="applied",
            )
        )
        s.add(
            AuditLog(
                confirmation_id=f"c2-{chat}",
                operation="x",
                customer_id=A,
                chat_id=chat,
                status="applied",
            )
        )
        s.add(
            Proposal(
                confirmation_id=f"c3-{chat}",
                operation="x",
                customer_id=A,
                chat_id=chat,
                params={},
                summary="s",
                status="pending",
            )
        )
        s.add(
            Recommendation(
                rec_uid=f"uid-{chat}-b",
                chat_id=chat,
                customer_id=B,
                topic="optimize",
                kind="k",
                severity="info",
                priority=1.0,
                evidence={},
                body="b",
                source="advise",
                status="shown",
            )
        )
        s.add(
            AuditLog(
                confirmation_id=f"c4-{chat}",
                operation="x",
                customer_id=bm.DRAFT_ACCOUNT_ID,
                chat_id=chat,
                status="applied",
            )
        )
        old = AuditLog(
            confirmation_id=f"c5-{chat}",
            operation="x",
            customer_id=C,
            chat_id=chat,
            status="applied",
        )
        old.created_at = datetime.now(timezone.utc) - timedelta(days=90)
        s.add(old)
        await s.commit()


async def test_frequent_orders_by_activity_and_filters():
    chat = 6301
    await _seed(chat)
    bm._FREQ_CACHE.pop(chat, None)
    got = await bm._frequent_accounts(chat, n=3, days=30)
    assert got[0] == A  # 3 события > 1
    assert B in got
    assert bm.DRAFT_ACCOUNT_ID not in got  # Draft — не «частый»
    assert C not in got  # событие старше окна


async def test_frequent_ttl_cache_and_empty():
    chat = 6302
    await _seed(chat)
    bm._FREQ_CACHE.pop(chat, None)
    first = await bm._frequent_accounts(chat)
    assert first  # прогрет
    # подмена кэша доказывает, что повторный вызов НЕ ходит в БД (TTL жив)
    import time as _t

    bm._FREQ_CACHE[chat] = (_t.monotonic(), ["9998887776"])
    assert await bm._frequent_accounts(chat) == ["9998887776"]
    # пустая история → []
    bm._FREQ_CACHE.pop(999888, None)
    assert await bm._frequent_accounts(999888) == []


def test_keyboard_pins_frequent_without_dup_and_only_from_rows():
    rows = [
        SimpleNamespace(id=bm.DRAFT_ACCOUNT_ID, name="Draft", currency=""),
        SimpleNamespace(id=A, name="Башня", currency="UAH"),
        SimpleNamespace(id=B, name="DARIAL", currency="USD"),
    ]
    kb = report_accounts_kb(rows, "report", last=A, frequent=[A, B, "7778889990"])
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    # A уже закреплён как «↻ как в прошлый раз» → ⭐ только для B; чужой id (нет в rows) — нет.
    assert sum(1 for t in texts if t.startswith("⭐")) == 1
    assert any(t == "⭐ DARIAL" for t in texts)
    assert not any("7778889990" in t and t.startswith("⭐") for t in texts)
    # на странице >0 закрепов нет (нужно больше строк, чем помещается на страницу 0)
    many = rows + [
        SimpleNamespace(id=f"90000000{i:02d}", name=f"Acct{i}", currency="") for i in range(20)
    ]
    kb2 = report_accounts_kb(many, "report", last=A, frequent=[B], page=1)
    texts2 = [btn.text for row in kb2.inline_keyboard for btn in row]
    assert not any(t.startswith("⭐") for t in texts2)
