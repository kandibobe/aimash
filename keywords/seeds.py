"""§19.4.2 (i): авто-генерация 10 seed-ключей по теме кампании, URL и профилю клиента (LLM).

Только текст: модель предлагает сид-ключи, КОД их нормализует и кап-ает до n. Это advisory — SDK
не трогается. Паттерн строгого JSON-массива зеркалит keywords.cluster. Fallback при сбое/пустом —
эвристика из темы (разбивка на словосочетания), чтобы конвейер не падал.
"""

from __future__ import annotations

import json
import re

from agent.router import chat

SEED_COUNT = 10  # §19.4.2: «10 самых релевантных ключевых слов»

_SYSTEM = (
    "Ты — специалист по контекстной рекламе Google Ads. По теме кампании (ЧТО рекламируется — "
    "товар/услуга), ссылке и информации о клиенте предложи РОВНО {n} самых релевантных "
    "КОММЕРЧЕСКИХ поисковых ключевых слов (seed-ключей) — то, что вводят готовые купить/заказать. "
    "Пиши ключи на УКАЗАННОМ языке аудитории (не на языке этого промпта). Ключи должны быть про "
    "сам товар/услугу, а НЕ общие слова о стране/направлении. Верни СТРОГО JSON-массив строк "
    '["…","…"] без пояснений и markdown, в нижнем регистре, без дублей, без кавычек/скобок '
    "соответствия — только сами фразы."
)


def _parse(content: str, limit: int) -> list[str]:
    s = content or ""
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in data if isinstance(data, list) else []:
        if not isinstance(x, (str, int, float)):
            continue
        t = str(x).strip().casefold()
        if t and t not in seen:
            seen.add(t)
            out.append(str(x).strip())
        if len(out) >= limit:
            break
    return out


def _fallback_seeds(topic: str, limit: int) -> list[str]:
    """Грубый fallback: словосочетания из темы (без LLM), чтобы Discover всё равно получил сиды."""
    words = [w for w in re.split(r"[\s,;]+", (topic or "").strip()) if len(w) > 2]
    out: list[str] = []
    seen: set[str] = set()
    for i in range(len(words)):
        for span in (2, 1):
            if i + span <= len(words):
                phrase = " ".join(words[i : i + span]).casefold()
                if phrase and phrase not in seen:
                    seen.add(phrase)
                    out.append(phrase)
            if len(out) >= limit:
                return out[:limit]
    return out[:limit]


async def generate_seed_keywords(
    *,
    topic: str,
    url: str | None = None,
    profile: str = "",
    language: str = "ru",
    n: int = SEED_COUNT,
) -> list[str]:
    """Сгенерировать до n seed-ключей (роль parsing). profile — текст профиля/посадочной (контекст).
    Fallback на эвристику из темы при сбое/пустом ответе. Пустые тема и профиль → []."""
    topic = (topic or "").strip()
    if not topic and not (profile or "").strip():
        return []
    ctx = f"Язык: {language}.\nТема кампании: {topic}."
    if url:
        ctx += f"\nURL: {url}"
    if (profile or "").strip():
        ctx += f"\nО клиенте/странице:\n{profile[:1500]}"
    try:
        msg = await chat(
            [
                {"role": "system", "content": _SYSTEM.format(n=n)},
                {"role": "user", "content": ctx},
            ],
            role="parsing",
            temperature=0.4,
        )
        seeds = _parse(getattr(msg, "content", "") or "", n)
    except Exception:  # noqa: BLE001 — конвейер не должен падать из-за LLM
        seeds = []
    return seeds or _fallback_seeds(topic, n)
