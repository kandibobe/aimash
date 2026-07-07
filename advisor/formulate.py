"""Advisory-LLM: гладкая формулировка УЖЕ выбранных и ранжированных кодом рекомендаций.

Образец keywords/cluster.py: модель получает ТОЛЬКО готовые факт-строки (без порогов, без выбора
действий) и лишь переписывает их живее, СОХРАНЯЯ смысл/числа/названия. Она НЕ решает, что
рекомендовать (это сделал advisor.rules), и не может предложить авто-действие. При любом сбое/
рассинхроне длины — детерминированный fallback на render (fail-closed на формулировку, fed-open —
фича не падает, но и не выдумывает).
"""

from __future__ import annotations

import json
import re

from advisor import render
from agent.router import chat

_DIGITS_RE = re.compile(r"\d+")


def _facts_preserved(base_line: str, llm_line: str, rec) -> bool:
    """КОД проверяет (golden rule #4, домен = «чужие деньги»), что LLM-перепись НЕ исказила факты:
    имя кампании и ВСЕ числовые токены из детерминированной строки должны присутствовать в переписи.
    Иначе строку откатываем на render (не показываем выдуманные модели суммы/имена)."""
    campaign = (getattr(rec, "facts", None) or {}).get("campaign")
    if campaign and str(campaign) not in llm_line:
        return False
    base_nums = set(_DIGITS_RE.findall(base_line))
    llm_nums = set(_DIGITS_RE.findall(llm_line))
    return base_nums.issubset(llm_nums)


_SYSTEM = (
    "Ты — маркетолог по контекстной рекламе. Тебе дают СПИСОК готовых подсказок по аккаунту. "
    "Перепиши КАЖДУЮ живее и понятнее, СТРОГО сохраняя смысл, все числа, проценты и названия "
    "кампаний без изменений. НЕ добавляй новых советов, НЕ предлагай менять бюджет/ставки "
    "автоматически, НЕ обещай что-то сделать сам. Верни СТРОГО JSON-массив строк той же длины "
    "({n}), в том же порядке, без markdown и пояснений."
)


def _parse(content: str, n: int) -> list[str] | None:
    """JSON-массив строк ТОЙ ЖЕ длины n → список; иначе None (→ fallback)."""
    s = content or ""
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list) or len(data) != n:
        return None
    out = [str(x).strip() for x in data]
    return out if all(out) else None


async def phrase(recs, lang: str = "ru", *, client_context: str = "") -> list[str]:
    """Вернуть список текстов рекомендаций (в порядке recs). База — детерминированный render; LLM
    лишь переписывает. Пустой список / сбой LLM / рассинхрон → база (fallback).

    client_context (2.11, §20.6) — компактный профиль клиента (бренд/услуги/гео) ТОЛЬКО для тона
    и терминологии ниши: модель НЕ получает права добавлять советы или менять числа — fact-guard
    _facts_preserved по-прежнему откатывает любое искажение на render (решения остаются в rules)."""
    base = [render.render_recommendation(r, lang) for r in recs]
    if not base:
        return base
    system = _SYSTEM.format(n=len(base))
    if client_context:
        system += (
            "\nКонтекст клиента (ТОЛЬКО для тона и терминологии; НЕ добавляй новых советов и не "
            f"меняй числа/названия): {client_context[:600]}"
        )
    try:
        msg = await chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(base, ensure_ascii=False)},
            ],
            role="copy",
            temperature=0.4,
        )
        parsed = _parse(getattr(msg, "content", "") or "", len(base))
        if not parsed:
            return base
        # Per-line факт-гард: искажённые моделью числа/имя кампании → откат строки на render.
        return [
            parsed[i] if _facts_preserved(base[i], parsed[i], recs[i]) else base[i]
            for i in range(len(base))
        ]
    except Exception:  # noqa: BLE001 — advisory, не критично; всегда есть детерминированный fallback
        return base
