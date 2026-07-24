"""2.8 (аудит 2026-07-06): flood-control исходящих — многочанковые отправки не падают на 429.

Инварианты: TelegramRetryAfter → sleep(retry_after)+повтор (с конечным потолком попыток);
между чанками send_html_chunks есть пауза; одиночное сообщение уходит без задержки (как раньше);
не-flood исключения пробрасываются как раньше.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramRetryAfter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bot import ux  # noqa: E402


def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SimpleNamespace(),
        message=f"Flood control: retry after {seconds}",
        retry_after=seconds,
    )


class _FloodMsg:
    """answer падает TelegramRetryAfter первые fail_n раз, затем принимает."""

    def __init__(self, fail_n: int = 1):
        self.fail_n = fail_n
        self.sent: list[str] = []

    async def answer(self, text="", **kw):
        if self.fail_n > 0:
            self.fail_n -= 1
            raise _retry_after(2)
        self.sent.append(text)


async def test_retry_after_sleeps_and_retries(monkeypatch):
    slept: list[float] = []

    async def _fake_sleep(s):
        slept.append(s)

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _fake_sleep)
    msg = _FloodMsg(fail_n=1)
    await ux.answer_with_flood_retry(msg, "привет")
    assert msg.sent == ["привет"]
    assert slept and slept[0] >= 2  # подождали retry_after


async def test_retry_after_gives_up_after_cap(monkeypatch):
    async def _fake_sleep(s):
        pass

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _fake_sleep)
    msg = _FloodMsg(fail_n=99)  # лимит держится дольше потолка попыток
    with pytest.raises(TelegramRetryAfter):
        await ux.answer_with_flood_retry(msg, "x")


async def test_chunks_paused_and_delivered(monkeypatch):
    slept: list[float] = []

    async def _fake_sleep(s):
        slept.append(s)

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _fake_sleep)
    msg = _FloodMsg(fail_n=0)
    long_text = "\n".join(f"строка {i} " + "x" * 80 for i in range(200))
    await ux.send_html_chunks(msg, long_text)
    assert len(msg.sent) >= 3  # реально многочанковое
    # пауз ровно (чанков-1): между чанками, но не перед первым
    assert len([s for s in slept if s == ux.CHUNK_SEND_PAUSE_S]) == len(msg.sent) - 1


async def test_single_message_no_pause(monkeypatch):
    slept: list[float] = []

    async def _fake_sleep(s):
        slept.append(s)

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _fake_sleep)
    msg = _FloodMsg(fail_n=0)
    await ux.send_html_chunks(msg, "короткое сообщение")
    assert msg.sent == ["короткое сообщение"] and slept == []


async def test_non_flood_error_propagates():
    class _Boom:
        async def answer(self, *a, **kw):
            raise RuntimeError("не flood")

    with pytest.raises(RuntimeError):
        await ux.answer_with_flood_retry(_Boom(), "x")
