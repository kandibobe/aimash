"""Генерация RSA-текстов (заголовки/описания) через LLM (OpenRouter, роль "copy").

ЖЁСТКО (golden rule #4): длину считает КОД, не модель. Любой элемент сверх лимита
(headline ≤30, description ≤90; кириллица = 1 символ) ОТБРАСЫВАЕТСЯ; при нехватке —
догенерация (до MAX_ATTEMPTS). Модель не трогает SDK — это только текст; применение к
объявлению идёт отдельно через mutation-гейт (фаза 2.C, поэлементное подтверждение).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, Field

from adcopy.validate import LIMITS, validate
from agent.router import chat

DEFAULT_HEADLINES = 15
DEFAULT_DESCRIPTIONS = 4
MAX_ATTEMPTS = 3


class CopyBrief(BaseModel):
    """Контекст для генерации (заполняет агент/пользователь, валидирует код)."""

    topic: str
    keywords: list[str] = Field(default_factory=list, max_length=50)
    usp: str | None = None  # уникальное торговое предложение
    tone: str | None = None  # тон (напр. «деловой», «дружелюбный»)
    geo: str | None = None
    language: str = "ru"
    n_headlines: int = Field(default=DEFAULT_HEADLINES, ge=1, le=30)
    n_descriptions: int = Field(default=DEFAULT_DESCRIPTIONS, ge=1, le=10)


@dataclass
class RsaDraft:
    headlines: list[str]
    descriptions: list[str]
    dropped_headlines: int = 0  # отброшено кодом за превышение лимита
    dropped_descriptions: int = 0
    attempts: int = 0


_SYSTEM = (
    "Ты — копирайтер Google Ads (RSA). Возвращай СТРОГО JSON-объект вида "
    '{"headlines": ["…"], "descriptions": ["…"]} без пояснений и без markdown. '
    "Заголовок — до 30 видимых символов, описание — до 90 (кириллица считается как 1 символ). "
    "Без КАПСА и спама, с призывом к действию, варианты уникальны и не дублируют друг друга."
)


def _user_prompt(brief: CopyBrief, need_h: int, need_d: int) -> str:
    parts = [
        f"Язык: {brief.language}.",
        f"Тематика: {brief.topic}.",
    ]
    if brief.keywords:
        parts.append("Ключевые слова: " + ", ".join(brief.keywords) + ".")
    if brief.usp:
        parts.append(f"УТП: {brief.usp}.")
    if brief.tone:
        parts.append(f"Тон: {brief.tone}.")
    if brief.geo:
        parts.append(f"Гео: {brief.geo}.")
    parts.append(
        f"Сгенерируй {need_h} заголовков (≤{LIMITS['headline']}) и "
        f"{need_d} описаний (≤{LIMITS['description']})."
    )
    return " ".join(parts)


def _parse(content: str) -> tuple[list[str], list[str]]:
    """Достаёт JSON-объект из ответа модели (терпимо к ```-обёрткам/тексту вокруг)."""
    s = content or ""
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return [], []
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return [], []
    if not isinstance(data, dict):
        return [], []
    h = [str(x) for x in data.get("headlines", []) if isinstance(x, (str, int, float))]
    d = [str(x) for x in data.get("descriptions", []) if isinstance(x, (str, int, float))]
    return h, d


def _ingest(raw: list[str], kind: str, limit: int, out: list[str], seen: set[str]) -> int:
    """Принять валидные элементы в out (с дедупом), вернуть число отброшенных за длину.
    Длину/лимит считает КОД (validate) — модель не может протащить слишком длинный текст.
    Отброшенные тоже попадают в seen — чтобы тот же текст не пере-считывался в догенерации."""
    dropped = 0
    for item in raw:
        if len(out) >= limit:
            break
        t = (item or "").strip()
        if not t:
            continue
        key = t.casefold()
        if key in seen:
            continue
        seen.add(key)  # запомнить (принятый ИЛИ отброшенный) — без повторной оценки
        ok, _ = validate(t, kind)
        if not ok:
            dropped += 1
            continue
        out.append(t)
    return dropped


async def generate_rsa(brief: CopyBrief) -> RsaDraft:
    """Сгенерировать валидные RSA-тексты. Догенерирует, пока не наберёт нужное число или
    не исчерпает MAX_ATTEMPTS. Возвращает только уложившиеся в лимит элементы."""
    headlines: list[str] = []
    descriptions: list[str] = []
    seen_h: set[str] = set()
    seen_d: set[str] = set()
    dropped_h = dropped_d = attempts = 0

    while attempts < MAX_ATTEMPTS and (
        len(headlines) < brief.n_headlines or len(descriptions) < brief.n_descriptions
    ):
        attempts += 1
        need_h = brief.n_headlines - len(headlines)
        need_d = brief.n_descriptions - len(descriptions)
        msg = await chat(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _user_prompt(brief, need_h, need_d)},
            ],
            role="copy",
            temperature=0.8,
        )
        raw_h, raw_d = _parse(getattr(msg, "content", "") or "")
        before = (len(headlines), len(descriptions))
        dropped_h += _ingest(raw_h, "headline", brief.n_headlines, headlines, seen_h)
        dropped_d += _ingest(raw_d, "description", brief.n_descriptions, descriptions, seen_d)
        if not raw_h and not raw_d:
            break  # модель не вернула ничего парсимого — нет смысла повторять
        if (len(headlines), len(descriptions)) == before:
            break  # попытка не дала НИ одного нового валидного варианта — модель «застряла»

    return RsaDraft(headlines, descriptions, dropped_h, dropped_d, attempts)
