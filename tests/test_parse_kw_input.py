"""Регрессия бага подбора ключей: URL + слова через пробел (без запятой) → `URL is malformed`.

`_parse_kw_input` теперь извлекает URL подстрокой (до первого пробела), остаток — сиды через
запятую/перенос (многословный сид остаётся ОДНИМ). См. bot/main.py:_parse_kw_input.
"""

from __future__ import annotations

from bot.main import _parse_kw_input


def test_url_then_space_separated_words_is_the_reported_bug():
    # Точный ввод из отчёта пользователя: раньше весь стринг уходил в url → Google Ads отвергал.
    seeds, url = _parse_kw_input("https://rozetka.com.ua интернет магазин")
    assert url == "https://rozetka.com.ua"
    assert seeds == ["интернет магазин"]


def test_comma_separated_seeds_no_url():
    seeds, url = _parse_kw_input("купить телефон, смартфон цена")
    assert url is None
    assert seeds == ["купить телефон", "смартфон цена"]


def test_url_with_commas_and_newline():
    seeds, url = _parse_kw_input("https://ex.com, доставка цветов\nбукет роз")
    assert url == "https://ex.com"
    assert seeds == ["доставка цветов", "букет роз"]


def test_no_url_single_multiword_seed():
    seeds, url = _parse_kw_input("интернет магазин")
    assert url is None
    assert seeds == ["интернет магазин"]


def test_url_trailing_dot_stripped():
    seeds, url = _parse_kw_input("https://ex.com.")
    assert url == "https://ex.com"
    assert seeds == []


def test_empty_input():
    seeds, url = _parse_kw_input("")
    assert url is None
    assert seeds == []
