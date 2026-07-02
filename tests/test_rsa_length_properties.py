"""Property-based тесты счётчика длины RSA (golden rule #4: длину считает КОД, кириллица=1, CJK=2,
по Unicode code points). Hypothesis перебирает краевые входы, которые примерами не покрыть:
эмодзи, комбинирующиеся диакритики, zero-width, границы CJK-диапазонов, смешанные скрипты.

Дополняет (не дублирует) tests/test_cc_display_path.py и tests/test_adcopy_generate.py, где
проверены конкретные примеры. Здесь — инварианты чистых функций adcopy/validate.py и усечения
display-path по ШИРИНЕ.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hypothesis import assume, given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from adcopy.display_path import _PATH_LIMIT, _truncate_width  # noqa: E402
from adcopy.validate import LIMITS, char_width, rsa_len, validate  # noqa: E402

# Три диапазона двойной ширины из adcopy.validate.char_width (единый источник истины).
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3040, 0x30FF), (0xAC00, 0xD7A3))


def _is_cjk(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


# ── char_width: строго {1,2}; ==2 РОВНО для CJK-диапазонов, иначе ==1 ─────────────
@given(st.integers(min_value=0, max_value=0x10FFFF))
def test_char_width_is_one_or_two_and_two_iff_cjk(cp):
    assume(not (0xD800 <= cp <= 0xDFFF))  # суррогаты — не самостоятельные символы
    w = char_width(chr(cp))
    assert w in (1, 2)
    assert (w == 2) == _is_cjk(cp), f"U+{cp:04X}: ширина {w}, ожидалась {2 if _is_cjk(cp) else 1}"


def test_cjk_range_boundaries_exact():
    """Ровно на границах диапазонов — ширина 2; на 1 code point за границей — ширина 1."""
    for lo, hi in _CJK_RANGES:
        assert char_width(chr(lo)) == 2, f"нижняя граница U+{lo:04X} должна быть width 2"
        assert char_width(chr(hi)) == 2, f"верхняя граница U+{hi:04X} должна быть width 2"
        assert char_width(chr(lo - 1)) == 1, (
            f"U+{lo - 1:04X} (перед диапазоном) должна быть width 1"
        )
        assert char_width(chr(hi + 1)) == 1, f"U+{hi + 1:04X} (после диапазона) должна быть width 1"


# ── rsa_len: аддитивность и «кириллица = 1 code point» ───────────────────────────
@given(st.text(), st.text())
def test_rsa_len_is_additive(a, b):
    assert rsa_len(a + b) == rsa_len(a) + rsa_len(b)


_CYRILLIC = st.text(alphabet="абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")


@given(_CYRILLIC)
def test_cyrillic_counts_as_one_codepoint_each(t):
    assert rsa_len(t) == len(t)  # кириллица = 1 (а не 2 по UTF-8 байтам)


# ── validate ↔ rsa_len ↔ LIMITS: единая связка для всех типов ─────────────────────
@given(st.text(), st.sampled_from(list(LIMITS)))
def test_validate_agrees_with_rsa_len_and_limit(t, kind):
    ok, n = validate(t, kind)
    assert n == rsa_len(t)
    assert ok == (n <= LIMITS[kind])


def test_empty_string_valid_for_all_kinds():
    for kind in LIMITS:
        ok, n = validate("", kind)
        assert ok and n == 0


def test_boundary_exactly_at_limit_and_one_over():
    """На лимите — ок, лимит+1 — нет (кириллица width=1, systematic boundary для каждого типа)."""
    for kind, limit in LIMITS.items():
        assert validate("а" * limit, kind)[0] is True, f"{kind}: ровно {limit} должно проходить"
        assert validate("а" * (limit + 1), kind)[0] is False, f"{kind}: {limit + 1} должно падать"


# ── display-path: усечение по ШИРИНЕ никогда не превышает лимит ───────────────────
@given(st.text())
def test_truncate_width_never_exceeds_path_limit(s):
    assert rsa_len(_truncate_width(s)) <= _PATH_LIMIT


@given(st.text())
def test_truncate_width_is_prefix_and_keeps_short_inputs(s):
    out = _truncate_width(s)
    assert s.startswith(out)  # усечение — всегда префикс (не переставляет символы)
    if rsa_len(s) <= _PATH_LIMIT:
        assert out == s  # то, что и так влезает, не режется
