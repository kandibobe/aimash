"""Доработка одного элемента RSA по правке пользователя (фаза 2.C, кнопка «✏️ Доработать»).

Перегенерирует ОДИН заголовок/описание с учётом инструкции пользователя. Длину результата
считает КОД (вызывающая сторона валидирует через adcopy.validate); модель не может протащить
текст сверх лимита — он будет отвергнут.
"""

from __future__ import annotations

from adcopy.validate import LIMITS
from llm.router import chat

# kind в UI — "h"/"d"; внутри маппим в имя типа adcopy.validate.
_KIND_NAME = {
    "h": "headline",
    "d": "description",
    "headline": "headline",
    "description": "description",
}
_KIND_HUMAN = {"headline": "заголовок", "description": "описание"}

_SYSTEM = (
    "Ты — копирайтер Google Ads (RSA). Верни РОВНО одну строку — переписанный элемент, "
    "без кавычек, без пояснений, без markdown. Соблюдай лимит символов (кириллица = 1 символ), "
    "без КАПСА и спама, с призывом к действию."
)


def _user_prompt(brief: dict, kind: str, instruction: str) -> str:
    name = _KIND_NAME[kind]
    limit = LIMITS[name]
    parts = [
        f"Язык: {brief.get('language', 'ru')}.",
        f"Тематика: {brief.get('topic', '')}.",
    ]
    if brief.get("keywords"):
        parts.append("Ключевые слова: " + ", ".join(brief["keywords"]) + ".")
    if brief.get("usp"):
        parts.append(f"УТП: {brief['usp']}.")
    if brief.get("tone"):
        parts.append(f"Тон: {brief['tone']}.")
    if brief.get("geo"):
        parts.append(f"Гео: {brief['geo']}.")
    parts.append(
        f"Перепиши {_KIND_HUMAN[name]} (≤{limit} символов) с учётом правки: {instruction}. "
        "Верни одну строку."
    )
    return " ".join(parts)


async def refine_element(brief: dict, kind: str, instruction: str) -> str:
    """Перегенерировать один элемент. Возвращает текст (длину валидирует вызывающая сторона)."""
    msg = await chat(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _user_prompt(brief, kind, instruction)},
        ],
        role="copy",
        temperature=0.7,
    )
    text = (getattr(msg, "content", "") or "").strip()
    # Модель иногда возвращает многострочье/кавычки — берём первую непустую строку без кавычек.
    for line in text.splitlines():
        line = line.strip().strip('"').strip("«»").strip()
        if line:
            return line
    return text
