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
    "киргизия": "KG",
    "кыргызстан": "KG",
    "kyrgyzstan": "KG",
    "таджикистан": "TJ",
    "tajikistan": "TJ",
    "туркменистан": "TM",
    "turkmenistan": "TM",
    "китай": "CN",
    "china": "CN",
    "япония": "JP",
    "japan": "JP",
    "корея": "KR",
    "южная корея": "KR",
    "south korea": "KR",
    "индонезия": "ID",
    "indonesia": "ID",
    "вьетнам": "VN",
    "vietnam": "VN",
    "таиланд": "TH",
    "thailand": "TH",
    "филиппины": "PH",
    "philippines": "PH",
    "малайзия": "MY",
    "malaysia": "MY",
    "сингапур": "SG",
    "singapore": "SG",
    "пакистан": "PK",
    "pakistan": "PK",
    "бангладеш": "BD",
    "bangladesh": "BD",
    "иран": "IR",
    "iran": "IR",
    "ирак": "IQ",
    "iraq": "IQ",
    "израиль": "IL",
    "israel": "IL",
    "саудовская аравия": "SA",
    "saudi arabia": "SA",
    "катар": "QA",
    "qatar": "QA",
    "кувейт": "KW",
    "kuwait": "KW",
    "бразилия": "BR",
    "brazil": "BR",
    "аргентина": "AR",
    "argentina": "AR",
    "мексика": "MX",
    "mexico": "MX",
    "чили": "CL",
    "chile": "CL",
    "колумбия": "CO",
    "colombia": "CO",
    "перу": "PE",
    "peru": "PE",
    "австралия": "AU",
    "australia": "AU",
    "новая зеландия": "NZ",
    "new zealand": "NZ",
    "нидерланды": "NL",
    "голландия": "NL",
    "netherlands": "NL",
    "бельгия": "BE",
    "belgium": "BE",
    "швейцария": "CH",
    "switzerland": "CH",
    "австрия": "AT",
    "austria": "AT",
    "швеция": "SE",
    "sweden": "SE",
    "норвегия": "NO",
    "norway": "NO",
    "дания": "DK",
    "denmark": "DK",
    "финляндия": "FI",
    "finland": "FI",
    "ирландия": "IE",
    "ireland": "IE",
    "португалия": "PT",
    "portugal": "PT",
    "греция": "GR",
    "greece": "GR",
    "чехия": "CZ",
    "czech republic": "CZ",
    "czechia": "CZ",
    "словакия": "SK",
    "slovakia": "SK",
    "венгрия": "HU",
    "hungary": "HU",
    "румыния": "RO",
    "romania": "RO",
    "болгария": "BG",
    "bulgaria": "BG",
    "хорватия": "HR",
    "croatia": "HR",
    "сербия": "RS",
    "serbia": "RS",
    "словения": "SI",
    "slovenia": "SI",
    "литва": "LT",
    "lithuania": "LT",
    "латвия": "LV",
    "latvia": "LV",
    "эстония": "EE",
    "estonia": "EE",
    "марокко": "MA",
    "morocco": "MA",
    "эфиопия": "ET",
    "ethiopia": "ET",
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

# §7: полная таблица ISO-3166-1 alpha-2 → numeric. geoTargetConstant страны = 2000 + numeric
# (сверено: UA 804→2804, DE 276→2276, US 840→2840). Даёт geo_id для ЛЮБОЙ страны без ручного списка.
_ISO_ALPHA2_TO_NUMERIC: dict[str, int] = {
    "AD": 20,
    "AE": 784,
    "AF": 4,
    "AG": 28,
    "AI": 660,
    "AL": 8,
    "AM": 51,
    "AO": 24,
    "AR": 32,
    "AT": 40,
    "AU": 36,
    "AW": 533,
    "AZ": 31,
    "BA": 70,
    "BB": 52,
    "BD": 50,
    "BE": 56,
    "BF": 854,
    "BG": 100,
    "BH": 48,
    "BI": 108,
    "BJ": 204,
    "BM": 60,
    "BN": 96,
    "BO": 68,
    "BR": 76,
    "BS": 44,
    "BT": 64,
    "BW": 72,
    "BY": 112,
    "BZ": 84,
    "CA": 124,
    "CD": 180,
    "CF": 140,
    "CG": 178,
    "CH": 756,
    "CI": 384,
    "CL": 152,
    "CM": 120,
    "CN": 156,
    "CO": 170,
    "CR": 188,
    "CU": 192,
    "CV": 132,
    "CY": 196,
    "CZ": 203,
    "DE": 276,
    "DJ": 262,
    "DK": 208,
    "DM": 212,
    "DO": 214,
    "DZ": 12,
    "EC": 218,
    "EE": 233,
    "EG": 818,
    "ER": 232,
    "ES": 724,
    "ET": 231,
    "FI": 246,
    "FJ": 242,
    "FM": 583,
    "FR": 250,
    "GA": 266,
    "GB": 826,
    "GD": 308,
    "GE": 268,
    "GH": 288,
    "GM": 270,
    "GN": 324,
    "GQ": 226,
    "GR": 300,
    "GT": 320,
    "GW": 624,
    "GY": 328,
    "HK": 344,
    "HN": 340,
    "HR": 191,
    "HT": 332,
    "HU": 348,
    "ID": 360,
    "IE": 372,
    "IL": 376,
    "IN": 356,
    "IQ": 368,
    "IR": 364,
    "IS": 352,
    "IT": 380,
    "JM": 388,
    "JO": 400,
    "JP": 392,
    "KE": 404,
    "KG": 417,
    "KH": 116,
    "KI": 296,
    "KM": 174,
    "KN": 659,
    "KR": 410,
    "KW": 414,
    "KZ": 398,
    "LA": 418,
    "LB": 422,
    "LC": 662,
    "LI": 438,
    "LK": 144,
    "LR": 430,
    "LS": 426,
    "LT": 440,
    "LU": 442,
    "LV": 428,
    "LY": 434,
    "MA": 504,
    "MC": 492,
    "MD": 498,
    "ME": 499,
    "MG": 450,
    "MK": 807,
    "ML": 466,
    "MM": 104,
    "MN": 496,
    "MR": 478,
    "MT": 470,
    "MU": 480,
    "MV": 462,
    "MW": 454,
    "MX": 484,
    "MY": 458,
    "MZ": 508,
    "NA": 516,
    "NE": 562,
    "NG": 566,
    "NI": 558,
    "NL": 528,
    "NO": 578,
    "NP": 524,
    "NZ": 554,
    "OM": 512,
    "PA": 591,
    "PE": 604,
    "PG": 598,
    "PH": 608,
    "PK": 586,
    "PL": 616,
    "PT": 620,
    "PY": 600,
    "QA": 634,
    "RO": 642,
    "RS": 688,
    "RU": 643,
    "RW": 646,
    "SA": 682,
    "SB": 90,
    "SC": 690,
    "SD": 729,
    "SE": 752,
    "SG": 702,
    "SI": 705,
    "SK": 703,
    "SL": 694,
    "SN": 686,
    "SO": 706,
    "SR": 740,
    "SS": 728,
    "ST": 678,
    "SV": 222,
    "SY": 760,
    "SZ": 748,
    "TD": 148,
    "TG": 768,
    "TH": 764,
    "TJ": 762,
    "TL": 626,
    "TM": 795,
    "TN": 788,
    "TO": 776,
    "TR": 792,
    "TT": 780,
    "TW": 158,
    "TZ": 834,
    "UA": 804,
    "UG": 800,
    "US": 840,
    "UY": 858,
    "UZ": 860,
    "VC": 670,
    "VE": 862,
    "VN": 704,
    "VU": 548,
    "WS": 882,
    "YE": 887,
    "ZA": 710,
    "ZM": 894,
    "ZW": 716,
}


# K: страны, которые Google Ads / Keyword Planner НЕ обслуживает (реклама приостановлена для РФ/РБ).
# geoTargetConstant валиден ПО ФОРМЕ, но запрос идей с таким гео падает «The input has an invalid
# value». Отфильтровываем их ПЕРЕД SDK-вызовом (подбор идёт без этих стран) и честно сообщаем юзеру.
NON_SERVICEABLE_GEO_IDS: frozenset[int] = frozenset({2643, 2112})  # RU (Россия), BY (Беларусь)


def drop_non_serviceable_geo(geo_ids) -> tuple[tuple[int, ...], list[int]]:
    """(kept, dropped): убрать необслуживаемые страны (RU/BY) из набора geo-таргетов Keyword Planner."""
    kept: list[int] = []
    dropped: list[int] = []
    for gid in geo_ids or ():
        (dropped if int(gid) in NON_SERVICEABLE_GEO_IDS else kept).append(int(gid))
    return tuple(kept), dropped


def has_non_serviceable_geo(geo_ids) -> bool:
    """True, если среди geo-таргетов есть необслуживаемая страна (RU/BY) — повод показать пометку."""
    return any(int(g) in NON_SERVICEABLE_GEO_IDS for g in (geo_ids or ()))


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
    "AT": "de",
    "CH": "de",
    "PL": "pl",
    "TR": "tr",
    "EG": "ar",
    "SA": "ar",
    "QA": "ar",
    "KW": "ar",
    "IQ": "ar",
    "MA": "ar",
    "FR": "fr",
    "ES": "es",
    "MX": "es",
    "AR": "es",
    "CL": "es",
    "CO": "es",
    "PE": "es",
    "IT": "it",
    "PT": "pt",
    "BR": "pt",
    "NL": "nl",
    "BE": "nl",
    "JP": "ja",
    "KR": "ko",
    "CN": "zh",
    "TW": "zh",
    "HK": "zh",
    "SE": "sv",
    "NO": "no",
    "DK": "da",
    "FI": "fi",
    "IE": "en",
    "AU": "en",
    "NZ": "en",
    "SG": "en",
    "PK": "en",
    "PH": "en",
    "GR": "el",
    "CZ": "cs",
    "SK": "sk",
    "HU": "hu",
    "RO": "ro",
    "BG": "bg",
    "HR": "hr",
    "RS": "sr",
    "SI": "sl",
    "LT": "lt",
    "LV": "lv",
    "EE": "et",
    "TH": "th",
    "VN": "vi",
    "ID": "id",
    "IL": "he",
    "IR": "fa",
    "KG": "ru",
    "TJ": "ru",
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
    "pt": "pt",
    "portuguese": "pt",
    "португальский": "pt",
    "nl": "nl",
    "dutch": "nl",
    "нидерландский": "nl",
    "голландский": "nl",
    "ja": "ja",
    "japanese": "ja",
    "японский": "ja",
    "ko": "ko",
    "korean": "ko",
    "корейский": "ko",
    "zh": "zh",
    "chinese": "zh",
    "китайский": "zh",
    "sv": "sv",
    "swedish": "sv",
    "шведский": "sv",
    "da": "da",
    "danish": "da",
    "датский": "da",
    "fi": "fi",
    "finnish": "fi",
    "финский": "fi",
    "no": "no",
    "nb": "nb",
    "norwegian": "no",
    "норвежский": "no",
    "cs": "cs",
    "czech": "cs",
    "чешский": "cs",
    "sk": "sk",
    "slovak": "sk",
    "словацкий": "sk",
    "sl": "sl",
    "slovenian": "sl",
    "словенский": "sl",
    "hr": "hr",
    "croatian": "hr",
    "хорватский": "hr",
    "sr": "sr",
    "serbian": "sr",
    "сербский": "sr",
    "bg": "bg",
    "bulgarian": "bg",
    "болгарский": "bg",
    "ro": "ro",
    "romanian": "ro",
    "румынский": "ro",
    "hu": "hu",
    "hungarian": "hu",
    "венгерский": "hu",
    "el": "el",
    "greek": "el",
    "греческий": "el",
    "he": "he",
    "hebrew": "he",
    "иврит": "he",
    "hi": "hi",
    "hindi": "hi",
    "хинди": "hi",
    "id": "id",
    "indonesian": "id",
    "индонезийский": "id",
    "th": "th",
    "thai": "th",
    "тайский": "th",
    "vi": "vi",
    "vietnamese": "vi",
    "вьетнамский": "vi",
    "fa": "fa",
    "persian": "fa",
    "farsi": "fa",
    "персидский": "fa",
    "ur": "ur",
    "urdu": "ur",
    "урду": "ur",
    "ca": "ca",
    "catalan": "ca",
    "каталанский": "ca",
    "et": "et",
    "estonian": "et",
    "эстонский": "et",
    "lv": "lv",
    "latvian": "lv",
    "латышский": "lv",
    "lt": "lt",
    "lithuanian": "lt",
    "литовский": "lt",
    "kk": "kk",
    "kazakh": "kk",
    "казахский": "kk",
    "az": "az",
    "azerbaijani": "az",
    "азербайджанский": "az",
    "ka": "ka",
    "georgian": "ka",
    "грузинский": "ka",
    "hy": "hy",
    "armenian": "hy",
    "армянский": "hy",
}

# ISO языка → человекочитаемое имя, понятное ads.mutations._LANG_NAME_IDS (крит. языка кампании).
_LANG_ISO_TO_NAME: dict[str, str] = {"en": "English", "ru": "Russian", "uk": "Ukrainian"}

# Языки Keyword Planner — единый источник keyword_plan.LANGUAGE_IDS (см. keyword_ideas_lang);
# отдельный зажим-frozenset убран (§7: любой язык из таблицы Google, не только ru/uk/en).


def country_iso(name_or_code: str | None) -> str | None:
    """Название страны или ISO-код → ISO alpha-2 (upper). Неизвестное → None."""
    if not name_or_code:
        return None
    s = str(name_or_code).strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return _COUNTRY_ALIASES.get(s.casefold())


def lang_iso(name_or_code: str | None) -> str | None:
    """Название/код языка → ISO 639-1 (lower). Прямой ISO-код, известный Google (keyword_plan.
    LANGUAGE_IDS), принимается как есть; иначе резолв имени. Неизвестное → None."""
    if not name_or_code:
        return None
    s = str(name_or_code).strip().casefold()
    hit = _LANG_NAME_TO_ISO.get(s)
    if hit:
        return hit
    from ads.keyword_plan import LANGUAGE_IDS  # прямой код (de/pt/ja/…), если Google его знает

    return s if s in LANGUAGE_IDS else None


def geo_id_for_country(iso: str | None) -> int | None:
    """ISO страны → geoTargetConstant id (для Discover). Точный оверрайд `_ISO_TO_GEO_ID`, иначе
    формула 2000 + ISO-3166 numeric (любая страна). Неизвестный ISO → None."""
    if not iso:
        return None
    u = iso.upper()
    hit = _ISO_TO_GEO_ID.get(u)
    if hit:
        return hit
    num = _ISO_ALPHA2_TO_NUMERIC.get(u)
    return 2000 + num if num else None


def language_for_country(iso: str | None) -> str | None:
    """ISO страны → основной язык аудитории (ISO 639-1). Неизвестное → None."""
    return _ISO_TO_LANGUAGE.get((iso or "").upper()) if iso else None


# Реверс алиасов: ISO alpha-2 → основное (первое = русское) имя страны. Первое вхождение на страну
# (dict хранит порядок вставки: у каждой страны RU-имя идёт первым).
_ISO_TO_COUNTRY_NAME: dict[str, str] = {}
for _nm, _iso in _COUNTRY_ALIASES.items():
    _ISO_TO_COUNTRY_NAME.setdefault(_iso, _nm)


def country_name(iso: str | None) -> str | None:
    """ISO alpha-2 → человекочитаемое имя страны (для ДОСБОРА geo_locations, когда пользователь
    назвал страну без города → таргетим страну целиком). С заглавной. Неизвестный ISO → None."""
    if not iso:
        return None
    nm = _ISO_TO_COUNTRY_NAME.get(iso.strip().upper())
    return nm.title() if nm else None


def language_name(iso: str | None) -> str | None:
    """ISO языка → имя для критерия языка кампании (ads.mutations). Неподдержанное → None."""
    return _LANG_ISO_TO_NAME.get((iso or "").lower()) if iso else None


def keyword_ideas_lang(lang: str | None) -> str:
    """Язык подбора (Discover) для keyword_plan.LANGUAGE_IDS. Известный ISO/имя → его ISO; пусто/'any'
    или неизвестный Google → '' (язык в запросе НЕ задаём → Google шире выбирает идеи). §7: любой язык,
    без зажима в ru/uk/en."""
    from ads.keyword_plan import LANGUAGE_IDS

    low = (lang or "").strip().lower()
    if low in ("", "any"):
        return ""
    iso = low if low in LANGUAGE_IDS else (lang_iso(low) or "")
    return iso if iso in LANGUAGE_IDS else ""


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
