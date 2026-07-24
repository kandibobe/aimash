"""Чистая валидация ВХОДНЫХ данных Google Ads (без SDK). Единый источник истины для
длины/формы ключевых слов — её считает КОД, а не модель (golden rule #4).

Импортируется и схемами агента (agent.tools.schemas — отклонить ДО кнопок подтверждения),
и слоем мутаций (ads.mutations — defense-in-depth у самой границы SDK). SDK сюда НЕ тянем,
чтобы парс-путь агента не зависел от google-ads.
"""

from __future__ import annotations

# Лимит Google Ads на keyword.text. Кириллица = 1 символ (Unicode code points, len()),
# двойная ширина только у CJK — но это лимит API в символах, а не в байтах.
MAX_KEYWORD_CHARS = 80
MAX_KEYWORD_WORDS = 10


def assert_keyword_ok(text: str) -> str:
    """Проверка одного ключевого слова: непустое, ≤80 символов, ≤10 слов. Кириллица = 1
    символ (len() по code points, НЕ по UTF-8 байтам). Возвращает обрезанный по краям текст.
    Это лимит для keyword.text и НЕ равен RSA-лимитам (30/90) — другая сущность."""
    t = text.strip()
    if not t:
        raise ValueError("пустое ключевое слово")
    if len(t) > MAX_KEYWORD_CHARS:
        raise ValueError(f"ключевое слово >{MAX_KEYWORD_CHARS} символов ({len(t)}): {t[:30]}…")
    if len(t.split()) > MAX_KEYWORD_WORDS:
        raise ValueError(f"ключевое слово >{MAX_KEYWORD_WORDS} слов: {t[:40]}…")
    return t


def normalize_keywords(keywords: list[str]) -> list[str]:
    """Нормализует список ключевых слов: валидирует каждое (assert_keyword_ok) и убирает
    точные дубли, сохраняя порядок. Внутренние пробелы НЕ схлопываем ('а  б' ≠ 'а б' для
    Google Ads). Бросает ValueError, если после нормализации список пуст."""
    out: list[str] = []
    seen: set[str] = set()
    for k in keywords:
        t = assert_keyword_ok(k)
        if t not in seen:
            seen.add(t)
            out.append(t)
    if not out:
        raise ValueError("список ключевых слов пуст после нормализации")
    return out


def dedup_keyword_pairs(keywords: list[str], match_types: list[str]) -> tuple[list[str], list[str]]:
    """§19.4.1 (смешанный список): нормализует ключи и дедуплицирует по ПАРЕ (текст, тип).

    Google Ads допускает один и тот же текст с РАЗНЫМИ типами соответствия в одной группе — это
    разные критерии. `normalize_keywords` дедупит только тексты: на смешанном списке она молча
    теряла бы второй критерий и рвала склейку 1:1 с `keyword_match_types` (в схеме это выливалось
    в ValueError «не совпадает по длине» — кнопка «Создать черновик» не работала вовсе).

    Единый источник истины для трёх call-site: схема тула, ads.mutations, визард §19.
    Возвращает списки равной длины; ValueError на рассинхроне длин или пустом результате.
    """
    if len(match_types) != len(keywords):
        raise ValueError(
            f"keyword_match_types ({len(match_types)}) не совпадает по длине с "
            f"keywords ({len(keywords)}) — типы соответствия должны идти 1:1 к ключам"
        )
    out_kw: list[str] = []
    out_mt: list[str] = []
    seen: set[tuple[str, str]] = set()
    for k, mt in zip(keywords, match_types):
        t = assert_keyword_ok(k)
        m = str(mt)
        pair = (t, m.strip().lower())  # тип регистронезависим: 'exact' и 'EXACT' — один критерий
        if pair not in seen:
            seen.add(pair)
            out_kw.append(t)
            out_mt.append(m)
    if not out_kw:
        raise ValueError("список ключевых слов пуст после нормализации")
    return out_kw, out_mt
