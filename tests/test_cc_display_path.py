"""Офлайн-тесты §19.5.1: авто display path (2 сегмента ≤15, кириллица=1, golden rule #4)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adcopy.display_path import build_display_path  # noqa: E402
from adcopy.validate import LIMITS, rsa_len  # noqa: E402


def test_segments_from_keyword_and_geo():
    p1, p2 = build_display_path(
        "https://x.co/used",
        ["used cars nairobi", "second hand cars kenya"],
        {"geo_locations": ["Nairobi"], "campaign_name": "Kenya Cars"},
    )
    assert p1 and rsa_len(p1) <= LIMITS["path"]
    assert p2 and rsa_len(p2) <= LIMITS["path"]
    assert p1 != p2


def test_cyrillic_counts_as_one_not_two():
    # «Поддержанные» = 12 кириллических букв → 12 (НЕ 24); влезает в лимит 15 целиком
    p1, _ = build_display_path("u", ["поддержанные автомобили"], {"geo_locations": ["Кения"]})
    assert p1 == "Поддержанные"
    assert rsa_len(p1) == 12


def test_long_first_word_truncated_to_limit():
    # одно слово длиннее 15 code points → режется по code points, не падает
    p1, _ = build_display_path("u", ["супердлинноеназваниекатегории"], {})
    assert rsa_len(p1) <= LIMITS["path"]
    assert p1  # не пустой


def test_empty_inputs_give_empty_segments():
    p1, p2 = build_display_path("https://x.co", [], {"geo_locations": [], "campaign_name": ""})
    assert p1 == ""
    assert p2 == ""


def test_no_duplicate_second_segment():
    # один ключ и нет гео → второй сегмент не дублирует первый
    p1, p2 = build_display_path("u", ["авто"], {"geo_locations": []})
    assert p1 == "Авто"
    assert p2 == ""


def test_cjk_first_word_truncated_by_width_not_codepoints():
    # CJK считается шириной 2 → 15-лимит ⇒ ≤7-8 иероглифов, НЕ 15 code points (golden rule #4)
    p1, _ = build_display_path("u", ["品" * 20], {})
    assert rsa_len(p1) <= LIMITS["path"]  # раньше было бы 30 (15 code points × 2)
