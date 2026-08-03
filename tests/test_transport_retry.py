"""BZ-3 (2026-07-30): флуд-ретрай фонового Telegram-транспорта + AST-гард класса.

До этого каждая джоба слала `bot.send_message` напрямую: первый же TelegramRetryAfter превращался
в «недоставлено» — терпимо для аномалий (повтор через 6 ч), фатально для error-алертов и
.xlsx-курьера (state='failed' навсегда). Теперь единственная дорога — `scheduler.transport`
(send_bot_message / send_bot_file / send_bot_document), и ретрай в ней.

Гард класса — AST-запрет: НОВАЯ точка отправки в scheduler/*.py обязана идти через transport и
получает ретрай по построению, а не по памяти автора.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from scheduler import transport


def _retry_after(seconds: float) -> transport.TelegramRetryAfter:
    return transport.TelegramRetryAfter(seconds)


class _FloodBot:
    """send_message/send_document падают TelegramRetryAfter первые fail_n раз, затем принимают.
    retry_after крошечный (0.01 с) — тест не спит заметно."""

    def __init__(self, fail_n: int = 1):
        self.fail_n = fail_n
        self.calls = 0
        self.sent: list[tuple[int, str]] = []
        self.documents: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.calls += 1
        if self.fail_n > 0:
            self.fail_n -= 1
            raise _retry_after(0.01)
        self.sent.append((chat_id, text))

    async def send_document(self, chat_id, document, **kw):
        self.calls += 1
        if self.fail_n > 0:
            self.fail_n -= 1
            raise _retry_after(0.01)
        self.documents.append((chat_id, kw.get("filename", "")))


async def test_send_message_survives_two_floods():
    """2 флуда подряд → 3-я попытка доставляет (потолок _RETRY_AFTER_MAX_ATTEMPTS=3)."""
    bot = _FloodBot(fail_n=2)
    await transport.send_bot_message(bot, 7, "hi", parse_mode="HTML")
    assert bot.calls == 3
    assert bot.sent == [(7, "hi")]


async def test_send_message_gives_up_after_max_attempts():
    """Флуд не кончается → после 3 попыток исключение отдаётся вызывающему (per-recipient
    решение — его: чек-поинт алертов не двигается, батч повторится)."""
    bot = _FloodBot(fail_n=99)
    with pytest.raises(transport.TelegramRetryAfter):
        await transport.send_bot_message(bot, 7, "hi")
    assert bot.calls == 3
    assert bot.sent == []


async def test_non_flood_error_goes_straight_out():
    """Блокировка чата/сеть ожиданием не лечатся — прочие исключения наружу с ПЕРВОЙ попытки."""

    class _Dead:
        calls = 0

        async def send_message(self, chat_id, text, **kw):
            self.calls += 1
            raise RuntimeError("chat blocked")

    bot = _Dead()
    with pytest.raises(RuntimeError, match="chat blocked"):
        await transport.send_bot_message(bot, 7, "hi")
    assert bot.calls == 1


async def test_send_document_retries_too():
    """Вложение (.txt-дайджест) идёт тем же ретраем — send_bot_document → send_bot_file."""
    bot = _FloodBot(fail_n=1)
    await transport.send_bot_document(bot, 7, text="детали", filename="digest.txt")
    assert bot.calls == 2
    assert bot.documents == [(7, "digest.txt")]


def test_no_direct_send_outside_transport():
    """AST-гард класса: в scheduler/*.py (кроме transport.py) нет ни одного вызова
    `.send_message(...)` / `.send_document(...)` — иначе новая точка отправки молча теряет ретрай.
    Разбор исходника, не sys.modules: ленивый вызов внутри функции виден так же, как верхний."""
    sched_dir = pathlib.Path(__file__).resolve().parents[1] / "scheduler"
    offenders: list[str] = []
    for py in sorted(sched_dir.rglob("*.py")):
        if py.name == "transport.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"send_message", "send_document"}
            ):
                offenders.append(f"scheduler/{py.name}:{node.lineno}")
    assert not offenders, (
        "прямые Telegram-отправки вне scheduler/transport.py (теряют флуд-ретрай BZ-3), "
        f"шли через transport.send_bot_message/send_bot_file: {offenders}"
    )
