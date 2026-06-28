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
    intent: str = ""  # ТЗ §7: транзакционный | коммерческий | информационный | навигационный
    keywords: list[str] = field(default_factory=list)
    priority: float = 0.0  # вес кластера (объём × интент) для приоритезации (§7); считает КОД


# ── Таксономия интента (ТЗ §7) — КОД нормализует ответ модели (golden rule #4) ────
INTENTS = ("транзакционный", "коммерческий", "информационный", "навигационный")
_INTENT_SYNONYMS = {
    "транзакционный": "транзакционный",
    "transactional": "транзакционный",
    "транзакция": "транзакционный",
    "коммерческий": "коммерческий",
    "комерческий": "коммерческий",
    "commercial": "коммерческий",
    "информационный": "информационный",
    "informational": "информационный",
    "инфо": "информационный",
    "навигационный": "навигационный",
    "navigational": "навигационный",
    "navigation": "навигационный",
    "брендовый": "навигационный",  # «бренд»/навигация по ТЗ — навигационный интент
    "бренд": "навигационный",
    "brand": "навигационный",
}


def normalize_intent(raw: str) -> str:
    """Привести интент от модели к таксономии ТЗ §7 (4 значения). КОД, не доверяем модели:
    синонимы/англ./'брендовый' маппятся; неизвестное → '' (не показываем мусорный лейбл)."""
    return _INTENT_SYNONYMS.get((raw or "").strip().casefold(), "")


_SYSTEM = (
    "Ты — специалист по поисковому маркетингу. Сгруппируй ключевые слова по поисковому "
    "интенту/теме. Верни СТРОГО JSON-массив объектов вида "
    '[{"name":"Короткое имя группы","intent":"транзакционный|коммерческий|информационный|'
    'навигационный","keywords":["…"]}] без пояснений и markdown. intent — РОВНО одно из четырёх '
    "значений. Каждый ключ — ровно в одной группе; НЕ выдумывай новых ключей и не меняй написание."
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
        intent = normalize_intent(str(obj.get("intent") or ""))  # КОД сводит к таксономии ТЗ §7
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


# ── Приоритезация кластеров (ТЗ §7 «приоритетные кластеры») — считает КОД ──────────
# Вес интента: ближе к покупке → выше (метрики к модели НЕ уходят, golden rule #4).
_INTENT_WEIGHT = {
    "транзакционный": 1.0,
    "коммерческий": 0.8,
    "навигационный": 0.5,
    "информационный": 0.3,
}
_DEFAULT_INTENT_WEIGHT = 0.4  # интент не распознан — нейтральный вес


def rank_clusters(clusters: list[Cluster], by_text: dict[str, int]) -> list[Cluster]:
    """Проставить priority = суммарный объём × вес интента и вернуть кластеры по убыванию приоритета
    (§7). «Технические» группы («Прочее…») всегда в конце. by_text — {ключ: средн. объём/мес}."""

    def _leftover(c: Cluster) -> bool:
        return c.name.startswith("Прочее")

    for c in clusters:
        vol = sum(int(by_text.get(k, 0) or 0) for k in c.keywords)
        c.priority = round(vol * _INTENT_WEIGHT.get(c.intent, _DEFAULT_INTENT_WEIGHT), 2)
    return sorted(clusters, key=lambda c: (_leftover(c), -c.priority))


# ── Предложение минус-слов (ТЗ §7 «предложение минус-слов») — advisory ────────────
_NEG_SYSTEM = (
    "Ты — специалист по контекстной рекламе Google Ads. По теме и списку идей ключевых слов "
    "предложи МИНУС-СЛОВА — слова, по которым показывать объявление НЕ стоит (нерелевантный/"
    "нецелевой трафик: напр. «бесплатно», «скачать», «своими руками», «вакансии», «бу», «отзывы», "
    "«фото», «википедия» — но ТОЛЬКО уместные ДЛЯ ЭТОЙ темы). Верни СТРОГО JSON-массив строк "
    '["…"] без пояснений и markdown, до 20 штук, в нижнем регистре.'
)


def _parse_neg(content: str, limit: int) -> list[str]:
    """JSON-массив строк → список минус-слов (нижний регистр, дедуп, не более limit)."""
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
            out.append(t)
        if len(out) >= limit:
            break
    return out


async def suggest_negative_keywords(
    topic: str, idea_texts: list[str], *, language: str = "ru", limit: int = 20
) -> list[str]:
    """Предложить минус-слова по теме + идеям (§7). ADVISORY: ничего не меняет, в аккаунт НЕ пишет —
    добавление минус-слов идёт ОТДЕЛЬНОЙ командой через confirm-гейт. Fallback [] при сбое/пустом."""
    sample = [t for t in idea_texts[:120] if t and t.strip()]
    if not (topic or "").strip() and not sample:
        return []
    try:
        msg = await chat(
            [
                {"role": "system", "content": _NEG_SYSTEM},
                {
                    "role": "user",
                    "content": f"Язык: {language}. Тема: {topic}.\nИдеи ключей:\n"
                    + "\n".join(sample),
                },
            ],
            role="parsing",
            temperature=0.3,
        )
        return _parse_neg(getattr(msg, "content", "") or "", limit)
    except Exception:  # noqa: BLE001 — advisory, не критично; всегда есть пустой fallback
        return []
