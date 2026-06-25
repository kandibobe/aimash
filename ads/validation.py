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
