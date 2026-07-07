"""D5 (удобство 2026-07): дедуп RSA-набора (защита от DUPLICATE_ASSET Google Ads).

find_duplicates (casefold+strip) + _validate_rsa_inputs ловит дубли ДО claim (defense-in-depth,
любой путь create_rsa), а rsa_list_edited показывает их дружелюбной ошибкой (менеджер правит).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adcopy.validate import find_duplicates  # noqa: E402


def test_find_duplicates_casefold_and_strip():
    assert find_duplicates(["Купить", "купить", "  КУПИТЬ  "]) == [(2, "купить"), (3, "  КУПИТЬ  ")]
    assert find_duplicates(["A", "B", "C"]) == []  # уникальные — нет дублей
    assert find_duplicates([]) == []


def _valid_headlines():
    return ["Заголовок один", "Заголовок два", "Заголовок три"]


def _valid_descriptions():
    return ["Описание первое достаточно длинное", "Описание второе тоже"]


def test_validate_rsa_inputs_rejects_duplicate_headline():
    from ads.mutations import _validate_rsa_inputs

    hs = ["Купить сейчас", "Купить СЕЙЧАС", "Заголовок три"]  # дубль (регистр)
    with pytest.raises(ValueError, match="дубли заголовков"):
        _validate_rsa_inputs(hs, _valid_descriptions(), "https://example.com", None, None)


def test_validate_rsa_inputs_rejects_duplicate_description():
    from ads.mutations import _validate_rsa_inputs

    ds = ["Одно и то же описание", "одно и то же описание"]  # дубль
    with pytest.raises(ValueError, match="дубли описаний"):
        _validate_rsa_inputs(_valid_headlines(), ds, "https://example.com", None, None)


def test_validate_rsa_inputs_passes_unique_set():
    from ads.mutations import _validate_rsa_inputs

    # уникальный валидный набор — не бросает
    _validate_rsa_inputs(
        _valid_headlines(), _valid_descriptions(), "https://example.com", None, None
    )
