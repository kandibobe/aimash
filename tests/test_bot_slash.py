"""Тесты слэш-команд /pause /resume (ТЗ §6): создают черновик за confirm-гейтом (как inline-
кнопка и текстовая команда), без имени — подсказка. Реальный ConfirmStore на temp SQLite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from db.session import init_db  # noqa: E402


class FakeMessage:
    def __init__(self, chat_id: int = 300):
        self.chat = SimpleNamespace(id=chat_id)
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))


def _cmd(args: str | None):
    return SimpleNamespace(args=args)  # дакт-замена CommandObject (читается только .args)


async def test_slash_pause_creates_pending_proposal():
    await init_db()
    m = FakeMessage(chat_id=301)
    await bm.pause_(m, _cmd("BrandX"))
    cid = bm._LAST_PENDING.get(301)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "pause_campaign"
    assert snap.status == "pending"  # НЕ исполнено — ждёт ✅
    assert snap.user_initiated is True
    assert snap.params["campaign"] == "BrandX"


async def test_slash_resume_creates_pending_proposal():
    await init_db()
    m = FakeMessage(chat_id=302)
    await bm.resume_(m, _cmd("  Brand Y  "))  # пробелы обрезаются
    snap = await ConfirmStore().get_confirmed(bm._LAST_PENDING[302])
    assert snap.operation == "resume_campaign" and snap.status == "pending"
    assert snap.params["campaign"] == "Brand Y"


async def test_slash_pause_no_arg_shows_hint_no_proposal():
    await init_db()
    bm._LAST_PENDING.pop(303, None)
    m = FakeMessage(chat_id=303)
    await bm.pause_(m, _cmd(None))
    assert m.answers and "/pause" in m.answers[-1][0]  # подсказка по использованию
    assert bm._LAST_PENDING.get(303) is None  # черновик не создан
