"""Тесты доработки одного элемента RSA (adcopy.refine.refine_element).

LLM подменяется фейком (patched adcopy.refine.chat). Длину результата НЕ доверяем модели —
её считает КОД (adcopy.validate); тест показывает, что переросток ловится кодом.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adcopy.refine as refine  # noqa: E402
from adcopy.validate import validate  # noqa: E402

_BRIEF = {"topic": "доставка цветов", "keywords": ["цветы"], "language": "ru"}


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _fake_chat(content: str):
    async def _chat(messages, role=None, temperature=None):
        return SimpleNamespace(content=content)

    return _chat


async def test_refine_returns_single_valid_line():
    with patched(refine, "chat", _fake_chat("Быстрая доставка цветов")):
        text = await refine.refine_element(_BRIEF, "h", "сделай динамичнее")
    assert text == "Быстрая доставка цветов"
    ok, n = validate(text, "headline")
    assert ok and n == len(text)  # кириллица = 1


async def test_refine_strips_quotes_and_takes_first_line():
    with patched(refine, "chat", _fake_chat("«Доставка за час»\nещё вариант")):
        text = await refine.refine_element(_BRIEF, "h", "короче")
    assert text == "Доставка за час"  # кавычки сняты, взята первая строка


async def test_refine_overlong_is_caught_by_code():
    long = "я" * 35  # 35 кириллических символов > 30
    with patched(refine, "chat", _fake_chat(long)):
        text = await refine.refine_element(_BRIEF, "h", "длиннее")
    ok, n = validate(text, "headline")
    assert ok is False and n == 35  # код ловит переросток (кириллица=1), модель не протащит


async def test_refine_description_kind():
    with patched(refine, "chat", _fake_chat("Подробное описание услуги доставки.")):
        text = await refine.refine_element(_BRIEF, "d", "добавь деталей")
    ok, _ = validate(text, "description")
    assert ok and text.startswith("Подробное")
