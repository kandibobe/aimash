"""Валидация длины рекламных текстов RSA. Чистый stdlib (без внешних зависимостей).

Длину считает КОД, не модель. Кириллица = 1 символ (двойная ширина только у CJK).
Считаем по Unicode code points (не по UTF-8 байтам).

Здесь же — эвристики редакторской политики Google Ads (КАПС/пунктуация/повторы): раньше они
жили ТОЛЬКО в промпте; теперь их считает КОД (как длину), advisory-предупреждением до запуска.
"""

from __future__ import annotations

import re

LIMITS = {"headline": 30, "description": 90, "path": 15}

# Состав RSA-объявления (Google Ads): 3–15 заголовков, 2–4 описания. Единый источник истины
# для схем (agent.tools.schemas), мутации (ads.mutations) и курации (adcopy.session).
RSA_MIN_HEADLINES, RSA_MAX_HEADLINES = 3, 15
RSA_MIN_DESCRIPTIONS, RSA_MAX_DESCRIPTIONS = 2, 4


def char_width(ch: str) -> int:
    """1 для латиницы/кириллицы; 2 только для CJK (как считает Google Ads)."""
    o = ord(ch)
    cjk = (
        0x4E00 <= o <= 0x9FFF  # CJK Unified Ideographs
        or 0x3040 <= o <= 0x30FF  # Hiragana / Katakana
        or 0xAC00 <= o <= 0xD7A3  # Hangul
    )
    return 2 if cjk else 1


def rsa_len(text: str) -> int:
    return sum(char_width(c) for c in text)


def validate(text: str, kind: str) -> tuple[bool, int]:
    """Возвращает (укладывается_ли, посчитанная_длина). kind: headline|description|path."""
    if kind not in LIMITS:
        raise ValueError(f"неизвестный тип текста: {kind}")
    n = rsa_len(text)
    return n <= LIMITS[kind], n


def assert_valid(text: str, kind: str) -> str:
    ok, n = validate(text, kind)
    if not ok:
        raise ValueError(f"{kind} превышает лимит {LIMITS[kind]}: {n} символов — «{text}»")
    return text


# ── Редакторская политика (advisory, НЕ хард-блок): КОД, а не промпт ──────────────
# Юникод-aware «слово» — серия букв без цифр/символов (\d и _ исключены); так isupper() для
# кириллицы/латиницы работает одинаково, а «B2B»/«01001» не считаются капсом (цифры рвут слово).
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_PUNCT_RUN_RE = re.compile(r"[!?]{2,}")  # «!!», «?!», «!!!» — серия из ≥2 знаков
_REPEAT_CHAR_RE = re.compile(r"([^\W\d_])\1\1", re.UNICODE)  # 3+ одинаковых буквы подряд

# Аббревиатуры ≤3 букв (SEO, USA, ЦБ) — норма, не «капс». Капсом считаем слово ≥4 заглавных букв.
ALL_CAPS_MIN_LEN = 4
MAX_EXCLAMATIONS = 1  # Google: не больше одного «!» в тексте (и не в заголовке)


def moderation_issues(text: str) -> list[str]:
    """Эвристики редакторской политики Google Ads — advisory (НЕ блокирует, редактура нечёткая).
    Возвращает список кодов-замечаний (пусто = чисто). Кириллице/латинице — единообразно (isupper):

    - ``all_caps``    — слово ≥4 заглавных букв (кричащий КАПС; аббревиатуры ≤3 не трогаем);
    - ``excess_punct``— серия «!?» подряд ИЛИ больше одного «!» суммарно;
    - ``repeat_char`` — 3+ одинаковых буквы подряд («Saaale», «ооочень»);
    - ``repeat_word`` — два одинаковых слова подряд («deal deal»)."""
    t = text or ""
    issues: list[str] = []
    words = _WORD_RE.findall(t)

    if any(len(w) >= ALL_CAPS_MIN_LEN and w.isupper() for w in words):
        issues.append("all_caps")
    if _PUNCT_RUN_RE.search(t) or t.count("!") > MAX_EXCLAMATIONS:
        issues.append("excess_punct")
    if _REPEAT_CHAR_RE.search(t):
        issues.append("repeat_char")
    lowered = [w.casefold() for w in words]
    if any(len(a) >= 2 and a == b for a, b in zip(lowered, lowered[1:])):
        issues.append("repeat_word")
    return issues


def count_flagged(texts: list[str]) -> int:
    """Сколько строк из списка имеют ≥1 редакторское замечание. Нестроки игнорируем (дакт-фейки
    в тестах передают счётчики, а не тексты) — фильтр по isinstance, чтобы не падать на них."""
    return sum(1 for t in texts if isinstance(t, str) and moderation_issues(t))
