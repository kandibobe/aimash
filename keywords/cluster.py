"""AI-кластеризация ключевых слов по поисковому интенту (Фаза 3, БЛОК E). Только текст.

Модель получает ТОЛЬКО список ключей и группирует их по интенту/теме; метрики (объём,
конкуренция) к ней НЕ уходят — их считает КОД (ads.keyword_plan). Это advisory: ничего в
аккаунте не меняется, SDK не трогается. Защита от мусора: оставляем лишь реально присланные
ключи (модель не может выдумать новых), дедуп между группами; при сбое/пустом ответе —
fallback на одну группу «Все ключи», чтобы фича не падала.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from agent.router import chat

MAX_CLUSTER_INPUT = 200  # сколько ключей максимум отдаём модели за раз (остаток → «Прочее»)


@dataclass
class Cluster:
    name: str  # короткое имя группы (RU)
    intent: str = ""  # §7: транзакционный|коммерческий|информационный|навигационный|брендовый
    keywords: list[str] = field(default_factory=list)
    priority: float = 0.0  # вес кластера (объём × интент) для приоритезации (§7); считает КОД


# ── Таксономия интента (ТЗ §7 + docs/KEYWORD_RESEARCH.md) — КОД нормализует ответ модели
# (golden rule #4). «брендовый» — ПЯТАЯ категория (живой тест 2026-07-06: конкурентные бренды
# «janjapan uganda»/«tradecarview uganda» падали в «информационный», т.к. модель не имела куда
# их положить; раньше «брендовый» сворачивался в «навигационный»).
INTENTS = ("транзакционный", "коммерческий", "информационный", "навигационный", "брендовый")
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
    "брендовый": "брендовый",
    "бренд": "брендовый",
    "brand": "брендовый",
    "branded": "брендовый",
    "конкурентный": "брендовый",
    "конкурент": "брендовый",
    "competitor": "брендовый",
    "competitive": "брендовый",
}


def normalize_intent(raw: str) -> str:
    """Привести интент от модели к таксономии §7 (5 значений). КОД, не доверяем модели:
    синонимы/англ./'конкурентный' маппятся; неизвестное → '' (не показываем мусорный лейбл)."""
    return _INTENT_SYNONYMS.get((raw or "").strip().casefold(), "")


_SYSTEM = (
    "Ты — специалист по поисковому маркетингу. Сгруппируй ключевые слова по поисковому "
    "интенту/теме. Верни СТРОГО JSON-массив объектов вида "
    '[{"name":"Короткое имя группы","intent":"транзакционный|коммерческий|информационный|'
    'навигационный|брендовый","keywords":["…"]}] без пояснений и markdown. intent — РОВНО одно '
    "из пяти значений. Ключи с названиями конкретных брендов, маркетплейсов, дилеров и "
    "КОНКУРЕНТОВ (напр. 'tradecarview', 'janjapan', 'sbt japan') → брендовый. Каждый ключ — "
    "ровно в одной группе; НЕ выдумывай новых ключей и не меняй написание."
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
    """Сгруппировать ключи по интенту через LLM (роль keywords: env LLM_KEYWORDS, пусто =
    parsing). Fallback при сбое — одна группа «Все ключи». Вход дедуплицируется,
    порядок сохраняется."""
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
            role="keywords",
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
    "брендовый": 0.7,  # бренд-запросы конкурентов — высокая готовность к покупке
    "навигационный": 0.5,
    "информационный": 0.3,
}
_DEFAULT_INTENT_WEIGHT = 0.4  # интент не распознан — нейтральный вес


def rank_clusters(
    clusters: list[Cluster], by_text: dict[str, int], off_topic: set[str] = frozenset()
) -> list[Cluster]:
    """Проставить priority = суммарный объём × вес интента и вернуть кластеры по убыванию приоритета
    (§7). «Технические» группы («Прочее…») всегда в конце. by_text — {ключ: средн. объём/мес}.
    off_topic — ключи, помеченные нерелевантными (§19.4.2): их объём НЕ идёт в приоритет, поэтому
    кластеры, забитые нерелевантным, тонут вниз. Ничего НЕ удаляется — контракт «менеджер видит всё»
    сохраняется (ключи остаются в кластерах/экспорте)."""

    def _leftover(c: Cluster) -> bool:
        return c.name.startswith("Прочее")

    for c in clusters:
        vol = sum(int(by_text.get(k, 0) or 0) for k in c.keywords if k not in off_topic)
        c.priority = round(vol * _INTENT_WEIGHT.get(c.intent, _DEFAULT_INTENT_WEIGHT), 2)
    return sorted(clusters, key=lambda c: (_leftover(c), -c.priority))


# ── Предложение минус-слов (ТЗ §7 «предложение минус-слов») — advisory ────────────
# База + примеры «мусорных» намерений ПО ЯЗЫКУ кампании. Раньше RU-хардкод («бесплатно/скачать/…»)
# подавался и для не-RU рынков → биас. Теперь примеры выбираются по language; для неизвестного —
# опускаем (пусть модель судит по теме/профилю). Профиль клиента подаётся отдельным блоком.
_NEG_BASE = (
    "Ты — специалист по контекстной рекламе Google Ads. По теме кампании, информации о клиенте "
    "и списку идей ключевых слов предложи МИНУС-СЛОВА — слова, по которым показывать объявление "
    "НЕ стоит (нерелевантный/нецелевой трафик), но ТОЛЬКО уместные ДЛЯ ЭТОЙ темы и этого бизнеса. "
)
_NEG_EXAMPLES = {
    "ru": "Примеры мусорных намерений: «бесплатно», «скачать», «своими руками», «вакансии», «бу», "
    "«отзывы», «фото», «википедия». ",
    "en": 'Examples of junk intent: "free", "download", "diy", "jobs", "used", "reviews", '
    '"wikipedia". ',
}
_NEG_TAIL = (
    'Верни СТРОГО JSON-массив строк ["…"] без пояснений и markdown, до 20 штук, в нижнем регистре.'
)


def _neg_system(language: str) -> str:
    """Системный промпт минус-слов с примерами ПО ЯЗЫКУ (RU/EN) или без — чтобы не тащить RU-биас
    на не-RU рынки (живой тест: RU few-shot искажал минус-слова для англоязычных кампаний)."""
    lang = (language or "").strip().casefold()
    if lang.startswith("ru"):
        ex = _NEG_EXAMPLES["ru"]
    elif lang.startswith("en"):
        ex = _NEG_EXAMPLES["en"]
    else:
        ex = ""  # неизвестный язык — без языко-специфичных примеров (биас недопустим)
    return _NEG_BASE + ex + _NEG_TAIL


def _drop_protected(negatives: list[str], protected: set[str]) -> list[str]:
    """Выкинуть минус-слова, чьи casefold-токены пересекают защищённые токены (бренд/услуги клиента).
    Иначе модель может предложить в минус собственный бренд/услугу клиента — и он перестанет
    показываться по своим же запросам. protected — набор уже-casefold токенов."""
    if not protected:
        return negatives
    out: list[str] = []
    for neg in negatives:
        toks = {t for t in re.split(r"[^0-9a-zа-яё]+", neg.casefold()) if t}
        if toks & protected:
            continue  # минус-слово задевает бренд/услугу клиента — не предлагаем
        out.append(neg)
    return out


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
    topic: str,
    idea_texts: list[str],
    *,
    language: str = "ru",
    limit: int = 20,
    profile: str = "",
    protected: set[str] = frozenset(),
) -> list[str]:
    """Предложить минус-слова по теме + идеям + профилю клиента (§7). ADVISORY: ничего не меняет,
    в аккаунт НЕ пишет — добавление минус-слов идёт ОТДЕЛЬНОЙ командой через confirm-гейт.
    profile — бизнес-контекст (чтобы минус-слова отражали бизнес, а не только тему-строку).
    protected — токены бренда/услуг клиента (пост-фильтр: не предлагаем их в минус).
    Fallback [] при сбое/пустом."""
    sample = [t for t in idea_texts[:120] if t and t.strip()]
    if not (topic or "").strip() and not sample:
        return []
    ctx = f"Язык: {language}. Тема: {topic}.\n"
    if (profile or "").strip():
        ctx += f"О клиенте:\n{profile[:1000]}\n"
    ctx += "Идеи ключей:\n" + "\n".join(sample)
    try:
        msg = await chat(
            [
                {"role": "system", "content": _neg_system(language)},
                {"role": "user", "content": ctx},
            ],
            role="keywords",
            temperature=0.3,
        )
        negs = _parse_neg(getattr(msg, "content", "") or "", limit)
    except Exception:  # noqa: BLE001 — advisory, не критично; всегда есть пустой fallback
        return []
    return _drop_protected(negs, protected)
