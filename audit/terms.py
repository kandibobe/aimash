"""Ф4: майнинг поисковых запросов — ЧИСТЫЙ КОД, без LLM (golden rule #4/#6).

Две противоположные операции над одним и тем же отчётом `search_term_view`:
  • сбор урожая (`harvest`) — запросы, которые КОНВЕРТЯТ, но их нет в ключах ⇒ добавить (в плюс);
  • n-граммы (`waste_ngrams`) — слова/пары, которые системно жгут бюджет и не конвертят НИГДЕ ⇒ в минус.

Ключевая защита обеих: **свои ключи неприкосновенны**. Кандидат в минус, входящий в текст любого
активного ключа, отбрасывается — минус-слово «ноутбук» на аккаунте с ключом «купить ноутбук» вырубило
бы весь трафик. Это код-гард, а не пожелание в промпте; брендозащита (`clients.store`) — про то же,
но для другого слоя.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORD = re.compile(r"[^\w]+", re.UNICODE)
MIN_TOKEN_LEN = 3  # «в», «и», «на» — не сигнал, а шум


def norm(text: str) -> str:
    """Нормализованный текст запроса/ключа: нижний регистр, пунктуация → пробел, схлопнутые пробелы.
    Match-type скобок/кавычек в `keyword.text` нет — Google отдаёт голый текст."""
    return _WORD.sub(" ", (text or "").lower()).strip()


def tokens(text: str) -> list[str]:
    return [t for t in norm(text).split() if t]


def _contains(hay: list[str], needle: list[str]) -> bool:
    """Входит ли последовательность токенов needle в hay (подряд)."""
    n = len(needle)
    if not n or n > len(hay):
        return False
    return any(hay[i : i + n] == needle for i in range(len(hay) - n + 1))


@dataclass
class HarvestItem:
    """Конвертящий запрос, которого НЕТ в ключах: его и добавлять (точным соответствием)."""

    term: str
    campaign: str
    ad_group: str  # группа, где запрос принёс больше всего денег — туда и класть ключ
    clicks: int
    cost: float
    conversions: float


@dataclass
class NgramItem:
    """Слово/пара слов, системно сливающая бюджет: 0 конверсий во ВСЕХ запросах, где встречается."""

    text: str
    n: int
    terms: int  # в скольких РАЗНЫХ запросах встретилась (1 — не система, а частный случай)
    clicks: int
    cost: float


def _term_rows(terms) -> list[tuple[list[str], object]]:
    return [(tokens(getattr(t, "search_term", "") or ""), t) for t in (terms or [])]


def _cost(t) -> float:
    return float(getattr(getattr(t, "metrics", None), "cost", 0.0) or 0.0)


def _conv(t) -> float:
    return float(getattr(getattr(t, "metrics", None), "conversions", 0.0) or 0.0)


def _clicks(t) -> int:
    return int(getattr(getattr(t, "metrics", None), "clicks", 0) or 0)


def harvest(
    terms: list, keywords: list[str], *, min_conv: float = 1.0, top_n: int = 5
) -> list[HarvestItem]:
    """Запросы с конверсиями, которых нет среди активных ключей аккаунта → кандидаты «в плюс».

    Сравниваем с ключами ВСЕГО аккаунта, а не только своей группы: запрос, уже покрытый ключом в
    соседней группе, добавлять нельзя — получим ровно ту каннибализацию, которую сами же и флажим.
    Один и тот же запрос из разных групп схлопываем; «куда класть» = группа с наибольшим расходом."""
    known = {norm(k) for k in keywords or [] if norm(k)}
    agg: dict[str, HarvestItem] = {}
    best_cost: dict[str, float] = {}
    for t in terms or []:
        text = norm(getattr(t, "search_term", "") or "")
        if not text or text in known or _conv(t) < min_conv:
            continue
        cost, item = _cost(t), agg.get(text)
        if item is None:
            agg[text] = HarvestItem(
                term=text,
                campaign=getattr(t, "campaign", "") or "",
                ad_group=getattr(t, "ad_group", "") or "",
                clicks=_clicks(t),
                cost=round(cost, 2),
                conversions=round(_conv(t), 2),
            )
            best_cost[text] = cost
            continue
        item.clicks += _clicks(t)
        item.cost = round(item.cost + cost, 2)
        item.conversions = round(item.conversions + _conv(t), 2)
        if cost > best_cost[text]:  # адрес берём у самой дорогой группы, метрики — суммарные
            item.campaign = getattr(t, "campaign", "") or ""
            item.ad_group = getattr(t, "ad_group", "") or ""
            best_cost[text] = cost
    out = sorted(agg.values(), key=lambda i: (-i.conversions, -i.cost))
    return out[: max(0, int(top_n))]


def waste_ngrams(
    terms: list,
    keywords: list[str],
    *,
    min_cost: float = 10.0,
    min_terms: int = 2,
    top_n: int = 5,
    max_n: int = 2,
) -> list[NgramItem]:
    """N-граммы (1..max_n слов), которые встречаются только в НЕконвертящих запросах и стоят денег.

    Три гарда против ложного минус-слова (минус-слово — необратимая для трафика вещь):
      1. n-грамма, входящая в текст ЛЮБОГО активного ключа, исключается — иначе зарежем свой ключ;
      2. хоть одна конверсия в любом запросе с этой n-граммой → не кандидат (порог 0, не «мало»);
      3. n-грамма должна встретиться минимум в `min_terms` РАЗНЫХ запросах — единичный случай уже
         ловит `wasteful_search_term`, а «система» — это про повторяемость.
    Из вложенных берём КОРОТКУЮ («своими» вместо «своими руками»): она перекрывает длинную."""
    kw_tokens = [tokens(k) for k in keywords or [] if tokens(k)]
    rows = _term_rows(terms)
    stat: dict[tuple[str, ...], dict] = {}
    for toks, t in rows:
        seen: set[tuple[str, ...]] = set()
        for n in range(1, max(1, int(max_n)) + 1):
            for i in range(len(toks) - n + 1):
                g = tuple(toks[i : i + n])
                if any(len(w) < MIN_TOKEN_LEN for w in g) or g in seen:
                    continue
                seen.add(g)
                s = stat.setdefault(g, {"terms": 0, "clicks": 0, "cost": 0.0, "conv": 0.0})
                s["terms"] += 1
                s["clicks"] += _clicks(t)
                s["cost"] += _cost(t)
                s["conv"] += _conv(t)
    cands: list[NgramItem] = []
    for g, s in stat.items():
        if s["conv"] > 0 or s["cost"] < min_cost or s["terms"] < min_terms:
            continue
        if any(_contains(kt, list(g)) for kt in kw_tokens):  # гард 1: не резать свой ключ
            continue
        cands.append(
            NgramItem(
                text=" ".join(g),
                n=len(g),
                terms=int(s["terms"]),
                clicks=int(s["clicks"]),
                cost=round(float(s["cost"]), 2),
            )
        )
    picked: list[NgramItem] = []
    for c in sorted(
        cands, key=lambda i: (i.n, -i.cost)
    ):  # короткие вперёд: они перекрывают длинные
        c_toks = c.text.split()
        if any(_contains(c_toks, p.text.split()) for p in picked):
            continue
        picked.append(c)
        if len(picked) >= max(0, int(top_n)):
            break
    return picked
