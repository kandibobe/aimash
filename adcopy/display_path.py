"""§19.5.1: авто-формирование Display Path (2 сегмента, ≤15 символов каждый) для RSA-объявления.

Display path — 2 видимых сегмента URL под доменом (domain.co.ke/Used-Cars/Nairobi). Не обязан
совпадать со структурой реального URL. Длину считает КОД (adcopy.validate, кириллица=1, CJK=2 —
golden rule #4; §19.5.1 «кириллица=2» — ошибка документа, игнорируем). Сегменты выводятся из
ключевых слов кампании и ГЕО; пустой результат допустим (path не обязателен).
"""

from __future__ import annotations

import re

from adcopy.validate import LIMITS, char_width, rsa_len

_PATH_LIMIT = LIMITS["path"]  # 15
# Юникод-«слово»: буквы/цифры без подчёркивания (кириллица/латиница одинаково).
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _truncate_width(s: str) -> str:
    """Усечь строку по ШИРИНЕ (rsa_len: кириллица=1, CJK=2 — golden rule #4), а НЕ по code points:
    иначе CJK-слово из 15 символов дало бы rsa_len 30 (вдвое больше лимита path)."""
    acc, width = "", 0
    for ch in s:
        cw = char_width(ch)
        if width + cw > _PATH_LIMIT:
            break
        acc += ch
        width += cw
    return acc


def _fit_segment(text: str) -> str:
    """Собрать сегмент пути из слов источника, укладываясь в лимит (rsa_len ≤15). Слова Title-case,
    соединяются дефисом, пока влезают; если первое слово длиннее лимита — режем ПО ШИРИНЕ."""
    words = _WORD_RE.findall(text or "")
    if not words:
        return ""
    out = ""
    for w in words:
        cand = f"{out}-{w}" if out else w
        cand = cand.title() if cand.isascii() else _title_unicode(cand)
        if rsa_len(cand) <= _PATH_LIMIT:
            out = cand
        elif not out:  # первое же слово не влезает → усечём по ширине (CJK=2, кириллица=1)
            out = _truncate_width(_title_unicode(w))
            break
        else:
            break
    return out


def _title_unicode(s: str) -> str:
    """Title-case, дружелюбный к кириллице (str.title даёт корректный регистр и для неё)."""
    return "-".join(p[:1].upper() + p[1:] for p in s.split("-") if p)


def build_display_path(
    url: str | None, keywords: list[str] | None, settings: dict | None
) -> tuple[str, str]:
    """Сформировать (path1, path2). path1 — из доминантного ключа/темы, path2 — из ГЕО или второго
    ключа. Оба валидируются adcopy.validate (≤15, кириллица=1). Пустые строки допустимы."""
    kws = [k.strip() for k in (keywords or []) if k and k.strip()]
    geo = ""
    topic = ""
    if settings:
        gl = settings.get("geo_locations") or []
        geo = (gl[0] if gl else "") or ""
        topic = (settings.get("campaign_name") or "") or ""
    seg1_src = kws[0] if kws else topic
    seg2_src = geo or (kws[1] if len(kws) > 1 else "")
    path1 = _fit_segment(seg1_src)
    path2 = _fit_segment(seg2_src)
    if path2 and path2 == path1:  # не дублируем сегмент
        path2 = ""
    return path1, path2
