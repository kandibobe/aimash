"""Тесты сессии курации RSA (adcopy.session.SessionStore) на временном SQLite (conftest).

Проверяем жизненный цикл курации: создание (все pending) → одобрить/отклонить →
массовое одобрение → доработка (replace → снова pending) → счётчики/готовность к созданию.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adcopy.session import SessionStore  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from db.session import init_db  # noqa: E402

_H = ["Заголовок раз", "Заголовок два", "Заголовок три", "Заголовок четыре"]
_D = ["Описание первое.", "Описание второе.", "Описание третье."]


async def _make() -> tuple[SessionStore, str]:
    await init_db()
    store = SessionStore()
    sid = await store.create(
        chat_id=1,
        customer_id=DRAFT_ACCOUNT_ID,
        campaign="Search",
        ad_group_id="42",
        ad_group_name="AG",
        final_url="https://example.com/",
        headlines=list(_H),
        descriptions=list(_D),
    )
    return store, sid


async def test_create_all_pending():
    store, sid = await _make()
    s = await store.get(sid)
    assert s is not None
    assert len(s.headlines) == 4 and len(s.descriptions) == 3
    assert all(e["state"] == "pending" for e in s.headlines + s.descriptions)
    # длину посчитал КОД при создании
    assert all(e["len"] == len(e["text"]) for e in s.headlines)  # кириллица=1
    assert s.next_pending() == ("h", 0)
    assert s.can_finalize() is False


async def test_set_state_approve_reject_and_order():
    store, sid = await _make()
    await store.set_state(sid, "h", 0, "approved")
    await store.set_state(sid, "h", 1, "rejected")
    s = await store.get(sid)
    assert s.headlines[0]["state"] == "approved"
    assert s.headlines[1]["state"] == "rejected"
    assert s.approved_texts("h") == [_H[0]]  # только одобренные, по порядку
    assert s.next_pending() == ("h", 2)  # пропустили решённые


async def test_can_finalize_threshold():
    store, sid = await _make()
    # 2 заголовка + 2 описания одобрены — ещё рано (нужно ≥3 заголовка)
    for i in (0, 1):
        await store.set_state(sid, "h", i, "approved")
    for i in (0, 1):
        await store.set_state(sid, "d", i, "approved")
    s = await store.get(sid)
    assert s.counts() == (2, 2)
    assert s.can_finalize() is False
    await store.set_state(sid, "h", 2, "approved")  # 3-й заголовок
    s = await store.get(sid)
    assert s.counts() == (3, 2)
    assert s.can_finalize() is True


async def test_approve_all_valid():
    store, sid = await _make()
    await store.set_state(sid, "h", 0, "rejected")  # отклонённый остаётся отклонённым
    s = await store.approve_all_valid(sid)
    assert s.headlines[0]["state"] == "rejected"
    assert all(e["state"] == "approved" for e in s.headlines[1:])
    assert all(e["state"] == "approved" for e in s.descriptions)
    assert s.can_finalize() is True  # 3 заголовка + 3 описания одобрены


async def test_replace_element_refine_back_to_pending():
    store, sid = await _make()
    await store.set_state(sid, "d", 0, "approved")
    s = await store.replace_element(sid, "d", 0, "Новое описание после правки.")
    assert s.descriptions[0]["text"] == "Новое описание после правки."
    assert s.descriptions[0]["state"] == "pending"  # после доработки требует повторного одобрения
    assert s.descriptions[0]["len"] == len("Новое описание после правки.")


async def test_get_missing_session_returns_none():
    await init_db()
    store = SessionStore()
    assert await store.get("nonexistent") is None


async def test_replace_all_default_approved_and_mark_pending():
    """replace_all: дефолт — все approved (list-UX §10); mark="pending" — регенерация §19.5.2
    (новый набор снова проходит поэлементную курацию, длину пересчитал КОД)."""
    store, sid = await _make()
    s = await store.replace_all(
        sid, ["Новый первый", "Новый второй", "Новый третий"], ["Опис 1", "Опис 2"]
    )
    assert [e["text"] for e in s.headlines] == ["Новый первый", "Новый второй", "Новый третий"]
    assert all(e["state"] == "approved" for e in s.headlines + s.descriptions)
    assert s.can_finalize() is True

    s = await store.replace_all(sid, list(_H), list(_D), mark="pending")
    assert all(e["state"] == "pending" for e in s.headlines + s.descriptions)
    assert all(e["len"] == len(e["text"]) for e in s.headlines)  # длина — КОД, кириллица=1
    assert s.can_finalize() is False
    assert s.next_pending() == ("h", 0)
