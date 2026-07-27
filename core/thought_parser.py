"""Парсер тегов <thought> из ответа LLM: извлечение, логирование, очистка.

Паттерн: модель вставляет <thought>...</thought> перед WRITE-действиями (propose_*/execute_*)
для внутреннего монолога (математика, риски). Пользователь видит только чистый текст.

Живёт в `bot.main._dispatch_command_result` (точка отправки текста пользователю).
"""

from __future__ import annotations

import re
from typing import Any

from core.logging import log

_THOUGHT_RE: re.Pattern = re.compile(
    r'<thought>(.*?)</thought>', re.DOTALL | re.IGNORECASE
)


def extract_thought(text: str) -> str | None:
    """Извлечь содержимое первого <thought>...</thought> из текста.
    None если тегов нет. Не модифицирует text."""
    m = _THOUGHT_RE.search(text)
    return m.group(1).strip() if m else None


def strip_thought(text: str) -> str:
    """Удалить ВСЕ <thought>...</thought> теги из текста, вернуть чистый текст."""
    return _THOUGHT_RE.sub("", text).strip()


def process_agent_response(
    text: str,
    *,
    operation: str | None = None,
    account: str | None = None,
) -> str:
    """Полный цикл: извлечь → залогировать → вернуть чистый текст.

    Если теги найдены:
      - Логи: thought_content, operation, account
      - Возвращает текст без тегов (пользователю)
    Если тегов нет:
      - Возвращает text как есть
    """
    t = text or ""
    thought = extract_thought(t)
    if thought:
        log.info(
            "thought extracted%s%s: %s",
            f" op={operation}" if operation else "",
            f" acct={account}" if account else "",
            thought[:500],
        )
        return strip_thought(t)
    return t


def process_response_dict(
    res: dict[str, Any],
    *,
    operation: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Обработать словарь ответа handle_command: очистить text-поля.

    Модифицирует входящий dict (мутация на месте) и возвращает его же.
    Поля: text, summary, question (clarify)."""
    for key in ("text", "summary", "question"):
        val = res.get(key)
        if isinstance(val, str) and val.strip():
            thought = extract_thought(val)
            if thought:
                log.info(
                    "thought extracted from res.%s%s%s: %s",
                    key,
                    f" op={operation}" if operation else "",
                    f" acct={account}" if account else "",
                    thought[:500],
                )
                res[key] = strip_thought(val)
    return res