"""§19: страна/язык таргетинга — единый резолв для Discover и генерации текстов.

Зачем: §19.4.2 требует Discover «с учётом ГЕО и языка кампании», а §19.5 — тексты на языке
АУДИТОРИИ (для кампании на Кению это английский, а НЕ язык интерфейса менеджера). Раньше подбор
шёл с дефолтным гео Украины и языком интерфейса (ru) — отсюда нерелевантные русские ключи/тексты.

Здесь три соответствия, все advisory (SDK не трогаем):
  • название страны (RU/EN/native) → ISO alpha-2;
  • ISO страны → Google geoTargetConstant id (= 2000 + ISO-3166 numeric: Кения 404→2404, Украина
    804→2804) — для GenerateKeywordIdeas;
  • ISO страны → основной язык поисковой аудитории (для seed-подбора и RSA-текстов).

Таблица компактная (популярные страны). Неизвестная страна ⇒ без гео-ограничения (глобальные
идеи лучше, чем идеи чужой страны) и язык по фоллбэку (интерфейс/en), а не молчаливый ru.
"""

from __future__ import annotations

# Название страны (casefold, RU/EN/native) → ISO 3166-1 alpha-2.
_COUNTRY_ALIASES: dict[str, str] = {
    "кения": "KE",
    "kenya": "KE",
    "украина": "UA",
    "україна": "UA",
    "ukraine": "UA",
    "россия": "RU",
    "russia": "RU",
    "беларусь": "BY",
    "belarus": "BY",
    "казахстан": "KZ",
    "kazakhstan": "KZ",
    "сша": "US",
    "usa": "US",
    "united states": "US",
    "америка": "US",
    "великобритания": "GB",
    "uk": "GB",
    "united kingdom": "GB",
    "англия": "GB",
    "германия": "DE",
    "germany": "DE",
    "польша": "PL",
    "poland": "PL",
    "турция": "TR",
    "turkey": "TR",
    "türkiye": "TR",
    "оаэ": "AE",
    "uae": "AE",
    "united arab emirates": "AE",
    "эмираты": "AE",
    "египет": "EG",
    "egypt": "EG",
    "нигерия": "NG",
    "nigeria": "NG",
    "юар": "ZA",
    "south africa": "ZA",
    "танзания": "TZ",
    "tanzania": "TZ",
    "уганда": "UG",
    "uganda": "UG",
    "гана": "GH",
    "ghana": "GH",
    "индия": "IN",
    "india": "IN",
    "канада": "CA",
    "canada": "CA",
    "франция": "FR",
    "france": "FR",
    "испания": "ES",
    "spain": "ES",
    "италия": "IT",
    "italy": "IT",
    "молдова": "MD",
    "молдавия": "MD",
    "moldova": "MD",
    "грузия": "GE",
    "georgia": "GE",
    "азербайджан": "AZ",
    "azerbaijan": "AZ",
    "армения": "AM",
    "armenia": "AM",
    "узбекистан": "UZ",
    "uzbekistan": "UZ",
}

# ISO alpha-2 → Google geoTargetConstant country id (= 2000 + ISO-3166 numeric).
_ISO_TO_GEO_ID: dict[str, int] = {
    "KE": 2404,
    "UA": 2804,
    "RU": 2643,
    "BY": 2112,
    "KZ": 2398,
    "US": 2840,
    "GB": 2826,
    "DE": 2276,
    "PL": 2616,
    "TR": 2792,
    "AE": 2784,
    "EG": 2818,
    "NG": 2566,
    "ZA": 2710,
    "TZ": 2834,
    "UG": 2800,
    "GH": 2288,
    "IN": 2356,
    "CA": 2124,
    "FR": 2250,
    "ES": 2724,
    "IT": 2380,
    "MD": 2498,
    "GE": 2268,
    "AZ": 2031,
    "AM": 2051,
    "UZ": 2860,
}

# ISO страны → основной язык поисковой аудитории (ISO 639-1) для подбора/текстов.
_ISO_TO_LANGUAGE: dict[str, str] = {
    "KE": "en",
    "UG": "en",
    "TZ": "en",
    "NG": "en",
    "ZA": "en",
    "GH": "en",
    "US": "en",
    "GB": "en",
    "CA": "en",
    "IN": "en",
    "AE": "en",
    "GE": "en",
    "UA": "uk",
    "RU": "ru",
    "BY": "ru",
    "KZ": "ru",
    "MD": "ru",
    "AM": "ru",
    "AZ": "ru",
    "UZ": "ru",
    "DE": "de",
    "PL": "pl",
    "TR": "tr",
    "EG": "ar",
    "FR": "fr",
    "ES": "es",
    "IT": "it",
}

# Название языка (RU/EN, casefold) → ISO 639-1 (для языка из описания менеджера).
_LANG_NAME_TO_ISO: dict[str, str] = {
    "en": "en",
    "english": "en",
    "английский": "en",
    "ru": "ru",
    "russian": "ru",
    "русский": "ru",
    "uk": "uk",
    "ukrainian": "uk",
    "украинский": "uk",
    "українська": "uk",
    "sw": "sw",
    "swahili": "sw",
    "суахили": "sw",
    "de": "de",
    "german": "de",
    "немецкий": "de",
    "fr": "fr",
    "french": "fr",
    "французский": "fr",
    "es": "es",
    "spanish": "es",
    "испанский": "es",
    "ar": "ar",
    "arabic": "ar",
    "арабский": "ar",
    "tr": "tr",
    "turkish": "tr",
    "турецкий": "tr",
    "pl": "pl",
    "polish": "pl",
    "польский": "pl",
    "it": "it",
    "italian": "it",
    "итальянский": "it",
}

# ISO языка → человекочитаемое имя, понятное ads.mutations._LANG_NAME_IDS (крит. языка кампании).
_LANG_ISO_TO_NAME: dict[str, str] = {"en": "English", "ru": "Russian", "uk": "Ukrainian"}

# Языки, для которых KeywordPlanIdeaService имеет константу в keyword_plan.LANGUAGE_IDS.
_KW_IDEA_LANGS = frozenset({"ru", "uk", "en"})


def country_iso(name_or_code: str | None) -> str | None:
    """Название страны или ISO-код → ISO alpha-2 (upper). Неизвестное → None."""
    if not name_or_code:
        return None
    s = str(name_or_code).strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return _COUNTRY_ALIASES.get(s.casefold())


def lang_iso(name_or_code: str | None) -> str | None:
    """Название/код языка → ISO 639-1 (lower). Неизвестное → None."""
    if not name_or_code:
        return None
    return _LANG_NAME_TO_ISO.get(str(name_or_code).strip().casefold())


def geo_id_for_country(iso: str | None) -> int | None:
    """ISO страны → geoTargetConstant id (для Discover). Неизвестное → None."""
    return _ISO_TO_GEO_ID.get((iso or "").upper()) if iso else None


def language_for_country(iso: str | None) -> str | None:
    """ISO страны → основной язык аудитории (ISO 639-1). Неизвестное → None."""
    return _ISO_TO_LANGUAGE.get((iso or "").upper()) if iso else None


def language_name(iso: str | None) -> str | None:
    """ISO языка → имя для критерия языка кампании (ads.mutations). Неподдержанное → None."""
    return _LANG_ISO_TO_NAME.get((iso or "").lower()) if iso else None


def keyword_ideas_lang(lang: str | None) -> str:
    """Язык подбора (Discover) — только из поддержанных keyword_plan.LANGUAGE_IDS (ru/uk/en).
    Неподдержанный (напр. sw/de) → 'en' (английский), а НЕ молчаливый 'ru'."""
    low = (lang or "").strip().lower()
    return low if low in _KW_IDEA_LANGS else "en"


def resolve_country(settings: dict) -> str | None:
    """ISO страны кампании: из geo_country_code, иначе из первого распознанного geo_locations."""
    iso = country_iso(settings.get("geo_country_code"))
    if iso:
        return iso
    for loc in settings.get("geo_locations") or []:
        iso = country_iso(loc)
        if iso:
            return iso
    return None


def geo_ids_for_settings(settings: dict) -> tuple[int, ...]:
    """geoTargetConstant id(ы) кампании для Discover. Неизвестная страна → () (без гео-ограничения:
    глобальные идеи лучше, чем идеи чужой страны)."""
    gid = geo_id_for_country(resolve_country(settings))
    return (gid,) if gid else ()
