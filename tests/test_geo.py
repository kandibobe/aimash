"""§19: резолв страны/языка таргетинга (ads.geo) — Discover-гео и язык аудитории.

Гард против регресса, из-за которого кампания на Кению подбирала русские ключи с гео Украины:
страна → geoTargetConstant Кении (2404, НЕ Украина 2804), язык аудитории en (НЕ интерфейс ru).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads import geo  # noqa: E402


def test_country_iso_names_and_codes():
    assert geo.country_iso("Кения") == "KE"
    assert geo.country_iso("kenya") == "KE"
    assert geo.country_iso("Украина") == "UA"
    assert geo.country_iso("KE") == "KE"  # уже ISO-код
    assert geo.country_iso("выдуманнаястрана") is None
    assert geo.country_iso(None) is None


def test_geo_id_follows_2000_plus_iso_numeric():
    # Google geoTargetConstant страны = 2000 + ISO-3166 numeric.
    assert geo.geo_id_for_country("KE") == 2404  # Кения (404)
    assert geo.geo_id_for_country("UA") == 2804  # Украина (804)
    assert geo.geo_id_for_country("US") == 2840
    assert geo.geo_id_for_country("ZZ") is None


def test_language_for_country_is_audience_language():
    assert geo.language_for_country("KE") == "en"  # Кения ищет по-английски, НЕ по-русски
    assert geo.language_for_country("UA") == "uk"
    assert geo.language_for_country("RU") == "ru"


def test_keyword_ideas_lang_falls_back_to_en_not_ru():
    assert geo.keyword_ideas_lang("en") == "en"
    assert geo.keyword_ideas_lang("uk") == "uk"
    # неподдержанный Discover'ом язык → en (а НЕ молчаливый ru, как было багом)
    assert geo.keyword_ideas_lang("sw") == "en"
    assert geo.keyword_ideas_lang("de") == "en"
    assert geo.keyword_ideas_lang(None) == "en"


def test_lang_iso_from_name():
    assert geo.lang_iso("English") == "en"
    assert geo.lang_iso("русский") == "ru"
    assert geo.lang_iso("Swahili") == "sw"
    assert geo.lang_iso("клингонский") is None


def test_resolve_country_prefers_code_then_names():
    assert geo.resolve_country({"geo_country_code": "KE", "geo_locations": ["Найроби"]}) == "KE"
    # без кода — из названия локации
    assert geo.resolve_country({"geo_locations": ["Кения"]}) == "KE"
    assert geo.resolve_country({"geo_locations": ["Марс"]}) is None


def test_geo_ids_for_settings_kenya_not_ukraine():
    assert geo.geo_ids_for_settings({"geo_country_code": "KE"}) == (2404,)
    # неизвестная страна → пусто (глобально), НЕ дефолт-Украина
    assert geo.geo_ids_for_settings({"geo_locations": ["Атлантида"]}) == ()
