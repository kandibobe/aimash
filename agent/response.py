"""Post-process model responses before they cross the Telegram transport boundary."""

from __future__ import annotations

import re
from contextvars import ContextVar


_THOUGHT_TAG = re.compile(r"<\s*(?P<closing>/)?\s*thought\s*>", re.IGNORECASE)
_EXCESS_BLANK_LINES = re.compile(r"\n(?:[ \t]*\n){2,}")

# ContextVar keeps concurrent Telegram updates isolated while letting the caller log
# the extracted reasoning to Langfuse immediately after process_agent_response().
agent_thoughts_var: ContextVar[tuple[str, ...]] = ContextVar("agent_thoughts", default=())


def get_agent_thoughts() -> tuple[str, ...]:
    """Thought blocks extracted in the current async/thread context."""
    return agent_thoughts_var.get()


def get_agent_thought_log() -> str:
    """A single log-ready value for the current response."""
    return "\n\n".join(get_agent_thoughts())


def process_agent_response(raw_response: str) -> str:
    """Extract ``<thought>`` blocks and return Telegram-safe visible text.

    Multiple and nested blocks are supported. An unclosed opening tag is handled
    fail-closed: everything after it is retained for logs and omitted from Telegram.
    The context variable is reset on every call so a response without a thought block
    cannot inherit reasoning from a previous response.
    """
    agent_thoughts_var.set(())
    if not isinstance(raw_response, str):
        raise TypeError("raw_response must be a string")

    visible: list[str] = []
    thoughts: list[str] = []
    current_thought: list[str] = []
    depth = 0
    cursor = 0

    for tag in _THOUGHT_TAG.finditer(raw_response):
        segment = raw_response[cursor : tag.start()]
        if depth:
            current_thought.append(segment)
        else:
            visible.append(segment)

        if tag.group("closing"):
            if depth:
                depth -= 1
                if depth == 0:
                    thought = "".join(current_thought).strip()
                    if thought:
                        thoughts.append(thought)
                    current_thought = []
            # A stray closing tag is omitted rather than leaked to Telegram.
        else:
            depth += 1
        cursor = tag.end()

    tail = raw_response[cursor:]
    if depth:
        current_thought.append(tail)
        thought = "".join(current_thought).strip()
        if thought:
            thoughts.append(thought)
    else:
        visible.append(tail)

    agent_thoughts_var.set(tuple(thoughts))
    telegram_text = "".join(visible)
    telegram_text = _EXCESS_BLANK_LINES.sub("\n\n", telegram_text)
    return telegram_text.strip()
