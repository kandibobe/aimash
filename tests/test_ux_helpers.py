"""Офлайн-тесты bot/ux.py: chat-action, диагностика RSA, длинные proposal. Без сети/Telegram."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.enums import ChatAction  # noqa: E402

import bot.ux as ux  # noqa: E402


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.actions: list[tuple[int, str]] = []
        self.fail = fail

    async def send_chat_action(self, chat_id, action):
        if self.fail:
            raise RuntimeError("telegram down")
        self.actions.append((chat_id, action))


class FakeMessage:
    def __init__(self, bot=None, chat_id: int = 7) -> None:
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id)
        self.answers: list[tuple[object, dict]] = []
        self.documents: list[tuple[object, dict]] = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))

    async def answer_document(self, document, **kw):
        self.documents.append((document, kw))


# ── chat_action ──────────────────────────────────────────────────────────────────
async def test_chat_action_sends_and_cancels():
    bot = FakeBot()
    msg = FakeMessage(bot=bot)
    async with ux.typing_action(msg):
        await asyncio.sleep(0.02)
    assert len(bot.actions) >= 1
    assert bot.actions[0][0] == 7
    assert bot.actions[0][1] == ChatAction.TYPING
    n = len(bot.actions)
    await asyncio.sleep(0.02)  # после выхода фон-таск отменён → не растёт
    assert len(bot.actions) == n


async def test_chat_action_noop_without_bot():
    msg = FakeMessage(bot=None)
    async with ux.typing_action(msg):  # _bot_and_chat → None → тихий no-op
        await asyncio.sleep(0.01)
    # главное — не упало


async def test_chat_action_swallows_send_errors():
    bot = FakeBot(fail=True)
    msg = FakeMessage(bot=bot)
    async with ux.upload_action(msg):  # send_chat_action бросает — должно проглотиться
        await asyncio.sleep(0.02)
    assert bot.actions == []


# ── fmt_rsa_diagnostics ──────────────────────────────────────────────────────────
def _draft(got_h: int, got_d: int, drop_h: int = 0, drop_d: int = 0):
    return SimpleNamespace(
        headlines=["h"] * got_h,
        descriptions=["d"] * got_d,
        dropped_headlines=drop_h,
        dropped_descriptions=drop_d,
    )


def test_fmt_rsa_diagnostics_ru_shows_ratio_and_dropped():
    s = ux.fmt_rsa_diagnostics(_draft(12, 4, drop_h=3), 15, 4)
    assert "12/15" in s and "4/4" in s and "3" in s


def test_fmt_rsa_diagnostics_en():
    s = ux.fmt_rsa_diagnostics(_draft(12, 4), 15, 4, lang="en")
    assert "headlines" in s and "12/15" in s and "4/4" in s


# ── редакция текста ошибок (golden rule #5) ──────────────────────────────────────
def test_err_text_redacts_secret_in_exception():
    # секрето-подобная строка в тексте исключения не должна дойти до пользователя
    e = RuntimeError("auth failed refresh_token=1//0xSECRETLONGTOKENvalue12345 denied")
    out = ux.err_text(e)
    assert "1//0xSECRETLONGTOKENvalue12345" not in out
    assert "REDACTED" in out
    assert out.startswith("RuntimeError:")  # формат «ТипОшибки: текст» сохранён


def test_err_text_plain_for_clean_message():
    # валидационная ошибка — без подсказки-суффикса (текст самодостаточен)
    out = ux.err_text(ValueError("кампания не найдена"))
    assert out == "ValueError: кампания не найдена"


def test_err_text_appends_hint_for_transient_errors():
    # сеть/таймаут/LLM → короткая подсказка «что делать»; валидация — без подсказки
    assert "попробуй ещё раз" in ux.err_text(TimeoutError("read timed out")).lower()
    assert "сеть" in ux.err_text(ConnectionError("connection refused")).lower()

    class _OpenAIErr(Exception):
        __module__ = "openai.error"

    assert "/model" in ux.err_text(_OpenAIErr("rate limit exceeded"))
    assert ux._err_hint(ValueError("bad")) == ""


# ── длинные proposal ─────────────────────────────────────────────────────────────
def test_proposal_fits_threshold():
    assert ux.proposal_fits("short") is True
    assert ux.proposal_fits("x" * 5000) is False


async def test_send_proposal_text_attaches_txt_and_cleans_up():
    msg = FakeMessage(bot=FakeBot())
    await ux.send_proposal_text(
        msg,
        full_text="полный текст " * 400,
        header_html="📝 Черновик (во вложении)",
        reply_markup="KB",
        parse_mode="HTML",
    )
    assert len(msg.documents) == 1  # .txt вложение
    assert len(msg.answers) == 1  # короткое сообщение С кнопками
    assert msg.answers[0][1].get("reply_markup") == "KB"
    doc = msg.documents[0][0]  # FSInputFile
    assert not os.path.exists(doc.path)  # temp удалён в finally
