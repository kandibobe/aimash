"""Офлайн-тесты agent/loop.py (ТЗ §4 AI Agent Core): русская команда → правильный исход.

LLM (router.chat) подменяется заглушкой. Проверяем ветвление handle_command:
clarify / proposal (mutation, НЕ исполняется) / read-intent (rsa/keywords) / текстовый фолбэк /
валидация аргументов В КОДЕ / устойчивость к мусорному tool-call. SDK/сеть не трогаются.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.loop as L  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _tc(name: str, args: dict):
    """Фейк tool_call в форме OpenAI SDK: .function.name / .function.arguments(JSON-строка)."""
    return SimpleNamespace(
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))
    )


def _msg(tool_calls=None, content: str = ""):
    return SimpleNamespace(tool_calls=tool_calls, content=content)


def _chat_returning(*msgs):
    """Заглушка router.chat: на i-й вызов отдаёт msgs[i] (последний повторяется)."""
    calls = {"n": 0}

    async def _chat(messages, **kwargs):
        i = min(calls["n"], len(msgs) - 1)
        calls["n"] += 1
        return msgs[i]

    return _chat, calls


# ── clarify: неоднозначная команда → вопрос, не угадывание ────────────────────────
async def test_ask_clarification():
    fake, _ = _chat_returning(_msg([_tc("ask_clarification", {"question": "Какую кампанию?"})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("повысь бюджет")
    assert out["type"] == "clarify" and out["question"] == "Какую кампанию?"


# ── mutation → proposal (черновик, НЕ исполнен, без user_initiated) ───────────────
async def test_mutation_returns_unexecuted_proposal():
    args = {"campaign": "Brand", "mode": "increase_by_percent", "value": 20}
    fake, _ = _chat_returning(_msg([_tc("update_budget", args)]))
    with patched(L, "chat", fake):
        out = await L.handle_command("повысь бюджет Brand на 20%", chat_id=42)
    assert out["type"] == "proposal" and out["operation"] == "update_budget"
    assert out["params"]["value"] == 20 and out["confirmation_id"]
    # провенанс «прямая команда человека» проставляет бот, НЕ агент про себя (fail-closed)
    assert "user_initiated" not in out["params"]


# ── валидацию диапазонов считает КОД (не доверяем модели) ─────────────────────────
async def test_invalid_args_rejected_in_code():
    # value=0 нарушает Field(gt=0) → ValidationError → текст, а не proposal
    fake, _ = _chat_returning(
        _msg([_tc("update_budget", {"campaign": "X", "mode": "set_to", "value": 0})])
    )
    with patched(L, "chat", fake):
        out = await L.handle_command("установи бюджет X в 0")
    assert out["type"] == "text" and "некорректные аргументы" in out["text"]


# ── read-intent: генерация RSA / подбор ключей → намерение боту (исполняет он) ────
async def test_generate_rsa_intent():
    fake, _ = _chat_returning(_msg([_tc("generate_rsa", {"topic": "доставка цветов"})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("придумай тексты для доставки цветов")
    assert out["type"] == "rsa_intent" and out["brief"]["topic"] == "доставка цветов"


async def test_keyword_research_intent():
    fake, _ = _chat_returning(
        _msg([_tc("keyword_research", {"seeds": ["цветы"], "language": "ru"})])
    )
    with patched(L, "chat", fake):
        out = await L.handle_command("подбери ключи по слову цветы")
    assert out["type"] == "keywords_intent" and out["brief"]["seeds"] == ["цветы"]


# ── нет tool-call и нет текста → одна повторная попытка → фолбэк-текст ─────────────
async def test_empty_response_retries_then_text_fallback():
    fake, calls = _chat_returning(_msg(None, ""), _msg(None, ""))
    with patched(L, "chat", fake):
        out = await L.handle_command("абракадабра")
    assert out["type"] == "text"
    assert calls["n"] == 2  # был ровно один повтор


# ── устойчивость к мусору: битый JSON аргументов и неизвестный инструмент ──────────
async def test_bad_tool_arguments_json():
    bad = SimpleNamespace(function=SimpleNamespace(name="update_budget", arguments="{не json"))
    fake, _ = _chat_returning(_msg([bad]))
    with patched(L, "chat", fake):
        out = await L.handle_command("...")
    assert out["type"] == "text" and "аргументы" in out["text"]


async def test_unknown_tool_name():
    fake, _ = _chat_returning(_msg([_tc("teleport", {})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("...")
    assert out["type"] == "text" and "неизвестный инструмент" in out["text"]
