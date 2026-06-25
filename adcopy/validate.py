"""Валидация длины рекламных текстов RSA. Чистый stdlib (без внешних зависимостей).

Длину считает КОД, не модель. Кириллица = 1 символ (двойная ширина только у CJK).
Считаем по Unicode code points (не по UTF-8 байтам).
"""

from __future__ import annotations

LIMITS = {"headline": 30, "description": 90, "path": 15}


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
