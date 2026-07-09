"""§19.4.2 (iii): AI-слой релевантности — помечает идеи ключей релевантно/нерелевантно бизнесу.

Только текст: модель видит ТОЛЬКО тексты идей и тему/профиль (метрики к ней НЕ уходят — golden
rule #4, их считает КОД). Это подсказка: финальное решение за менеджером (он редактирует таблицу).
Fail-open: при сбое/неизвестном — считаем релевантным (не теряем ключи молча, менеджер уберёт сам).
"""

from __future__ import annotations

import json

from agent.router import chat

_BATCH = 120  # сколько текстов отдаём модели за раз (как suggest_negative_keywords)

_SYSTEM = (
    "Ты — специалист по контекстной рекламе. По теме кампании и информации о клиенте определи, "
    "какие из присланных ключевых слов РЕЛЕВАНТНЫ бизнесу клиента, а какие нет (нецелевой трафик). "
    'Верни СТРОГО JSON-объект вида {"ключевое слово": true|false, …} без пояснений и markdown: '
    "true — релевантно, false — нерелевантно. Включи КАЖДОЕ присланное слово ровно один раз, "
    "написание не меняй."
)


def _parse(content: str, valid: list[str]) -> dict[str, bool]:
    s = content or ""
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    by_lower = {k.casefold(): k for k in valid}
    out: dict[str, bool] = {}
    for k, v in data.items():
        orig = by_lower.get(str(k).strip().casefold())
        if orig is None:
            continue
        coerced = _coerce_bool(v)
        if (
            coerced is not None
        ):  # неразборчивое → пропускаем (fail-open у вызывающего: .get(t, True))
            out[orig] = coerced
    return out


def _coerce_bool(v) -> bool | None:
    """Привести вердикт модели к bool. Строковые 'false'/'no'/'0'/'нет' → False (а НЕ True, как
    дал бы bool('false')); 'true'/'yes'/'1'/'да' → True; неразборчивое → None (вызывающий fail-open)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    sv = str(v).strip().casefold()
    if sv in {"false", "no", "0", "нет", "f", "n"}:
        return False
    if sv in {"true", "yes", "1", "да", "t", "y"}:
        return True
    return None


async def filter_relevance(
    *, texts: list[str], topic: str, profile: str = "", language: str = "ru"
) -> dict[str, bool]:
    """Вернуть {текст: релевантно?} по списку идей. Fail-open: отсутствующие/сбой → True (advisory).
    Дедупит вход по casefold, сохраняет исходное написание ключа."""
    uniq = list(dict.fromkeys(t.strip() for t in texts if t and t.strip()))
    if not uniq:
        return {}
    result: dict[str, bool] = {}
    for i in range(0, len(uniq), _BATCH):
        batch = uniq[i : i + _BATCH]
        ctx = (
            f"Язык: {language}.\nТема кампании: {topic}.\n"
            + (f"О клиенте:\n{profile[:1000]}\n" if (profile or "").strip() else "")
            + "Ключевые слова:\n"
            + "\n".join(batch)
        )
        try:
            msg = await chat(
                [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": ctx},
                ],
                role="keywords",
                temperature=0.2,
            )
            verdicts = _parse(getattr(msg, "content", "") or "", batch)
        except Exception:  # noqa: BLE001 — advisory; fail-open
            verdicts = {}
        for t in batch:
            result[t] = verdicts.get(t, True)  # неизвестное → релевантно (менеджер уберёт сам)
    return result
