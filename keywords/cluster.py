"""AI-кластеризация ключевых слов по поисковому интенту (Фаза 3, БЛОК E). Только текст.

Модель получает ТОЛЬКО список ключей и группирует их по интенту/теме; метрики (объём,
конкуренция) к ней НЕ уходят — их считает КОД (ads.keyword_plan). Это advisory: ничего в
аккаунте не меняется, SDK не трогается. Защита от мусора: оставляем лишь реально присланные
ключи (модель не может выдумать новых), дедуп между группами; при сбое/пустом ответе —
fallback на одну группу «Все ключи», чтобы фича не падала.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent.router import chat

MAX_CLUSTER_INPUT = 200  # сколько ключей максимум отдаём модели за раз (остаток → «Прочее»)


@dataclass
class Cluster:
    name: str  # короткое имя группы (RU)
    intent: str = ""  # транзакционный | информационный | брендовый | навигационный
    keywords: list[str] = field(default_factory=list)


_SYSTEM = (
    "Ты — специалист по поисковому маркетингу. Сгруппируй ключевые слова по поисковому "
    "интенту/теме. Верни СТРОГО JSON-массив объектов вида "
    '[{"name":"Короткое имя группы","intent":"транзакционный|информационный|брендовый|'
    'навигационный","keywords":["…"]}] без пояснений и markdown. Каждый ключ — ровно в одной '
    "группе; НЕ выдумывай новых ключей и не меняй их написание."
)


def _user_prompt(keywords: list[str], language: str) -> str:
    return (
        f"Язык ключей: {language}. Сгруппируй по интенту следующие ключевые слова "
        "(каждый используй ровно один раз):\n" + "\n".join(keywords)
    )


def _parse(content: str, valid: list[str]) -> list[Cluster]:
    """JSON-массив → Cluster[]. Оставляем только реально присланные ключи (по casefold,
    с возвратом исходного написания), без дублей между группами. Неразложенные → «Прочее»."""
    s = content or ""
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    by_lower = {k.casefold(): k for k in valid}  # вернуть оригинальное написание ключа
    used: set[str] = set()
    clusters: list[Cluster] = []
    for obj in data:
        if not isinstance(obj, dict):
            continue
        name = (str(obj.get("name") or "Без названия").strip() or "Без названия")[:60]
        intent = str(obj.get("intent") or "").strip()[:40]
        kws: list[str] = []
        for k in obj.get("keywords", []):
            if not isinstance(k, (str, int, float)):
                continue
            key = str(k).strip().casefold()
            orig = by_lower.get(key)
            if orig and key not in used:  # только реальные ключи, без дублей между группами
                used.add(key)
                kws.append(orig)
        if kws:
            clusters.append(Cluster(name=name, intent=intent, keywords=kws))

    leftover = [orig for low, orig in by_lower.items() if low not in used]
    if leftover:
        clusters.append(Cluster(name="Прочее", intent="", keywords=leftover))
    return clusters


async def cluster_keywords(keywords: list[str], language: str = "ru") -> list[Cluster]:
    """Сгруппировать ключи по интенту через LLM (роль parsing — дёшево). Fallback при сбое —
    одна группа «Все ключи». Вход дедуплицируется, порядок сохраняется."""
    uniq = list(dict.fromkeys(k.strip() for k in keywords if k.strip()))  # дедуп + порядок
    if not uniq:
        return []
    sample = uniq[:MAX_CLUSTER_INPUT]
    try:
        msg = await chat(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _user_prompt(sample, language)},
            ],
            role="parsing",
            temperature=0.3,
        )
        clusters = _parse(getattr(msg, "content", "") or "", sample)
    except Exception:  # noqa: BLE001 — кластеризация не критична, всегда есть fallback
        clusters = []
    if not clusters:
        return [Cluster(name="Все ключи", intent="", keywords=uniq)]
    if len(uniq) > len(sample):  # вход усекали → остаток отдельной группой, без «тихой» потери
        clusters.append(
            Cluster(name="Прочее (сверх лимита)", intent="", keywords=uniq[len(sample) :])
        )
    return clusters
