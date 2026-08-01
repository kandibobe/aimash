"""Thought extraction must never leak reasoning into Telegram text."""

import asyncio

import pytest

from agent.response import (
    get_agent_thought_log,
    get_agent_thoughts,
    process_agent_response,
)


def test_extracts_thought_and_returns_only_visible_text():
    text = process_agent_response(
        "<thought>40 × 1.2 = 48; риск +20%</thought>\n\nПодтвердите бюджет 48 USD."
    )

    assert text == "Подтвердите бюджет 48 USD."
    assert get_agent_thoughts() == ("40 × 1.2 = 48; риск +20%",)
    assert get_agent_thought_log() == "40 × 1.2 = 48; риск +20%"
    assert "40 ×" not in text


def test_multiple_and_case_insensitive_blocks_are_extracted():
    text = process_agent_response(
        "Начало\n<THOUGHT>первый расчёт</THOUGHT>\n<thought>второй риск</thought>\nКонец"
    )

    assert text == "Начало\n\nКонец"
    assert get_agent_thoughts() == ("первый расчёт", "второй риск")


def test_nested_and_unclosed_thoughts_fail_closed():
    assert (
        process_agent_response("Ответ<thought>outer <thought>inner</thought> tail</thought> виден")
        == "Ответ виден"
    )
    assert get_agent_thoughts() == ("outer inner tail",)

    assert process_agent_response("Виден<thought>секретный расчёт") == "Виден"
    assert get_agent_thoughts() == ("секретный расчёт",)


def test_stray_closing_tag_is_removed_and_context_is_reset():
    process_agent_response("<thought>старое</thought>Ответ")
    assert process_agent_response("Новый ответ</thought>") == "Новый ответ"
    assert get_agent_thoughts() == ()


@pytest.mark.asyncio
async def test_thought_storage_is_isolated_between_concurrent_tasks():
    async def worker(name: str) -> tuple[str, tuple[str, ...]]:
        text = process_agent_response(f"<thought>{name}</thought>{name}-visible")
        await asyncio.sleep(0)
        return text, get_agent_thoughts()

    first, second = await asyncio.gather(worker("one"), worker("two"))

    assert first == ("one-visible", ("one",))
    assert second == ("two-visible", ("two",))


def test_non_string_response_is_rejected():
    process_agent_response("<thought>старое</thought>Ответ")
    with pytest.raises(TypeError):
        process_agent_response(None)  # type: ignore[arg-type]
    assert get_agent_thoughts() == ()
