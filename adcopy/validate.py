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


# ── Релевантность: вхождение ключей в заголовки (§10 / claude-ads G35) ────────────────
# Порог покрытия — ЕДИНЫЙ для генерации (гейт: ниже → догенерируем заголовки с ключами) и для
# аудита существующих объявлений (чек rsa_keyword_coverage_low). Два числа разъехались бы —
# бот советовал бы одно, а генерировал другое.
MIN_KEYWORD_COVERAGE = 0.5


def tok_match(a: str, b: str) -> bool:
    """Совпадение токенов терпимо к словоформам (RU-инфлексия): точное ИЛИ общий префикс ≥4 симв.
    (ноутбук/ноутбука/ноутбуки → покрыто). Без этого точный матч часто ложно занижал покрытие."""
    if a == b:
        return True
    return len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a))


def keyword_coverage(headlines: list[str], keywords: list[str]) -> float:
    """§10: доля заголовков, покрывающих ХОТЯ БЫ один токен ключевого слова (см. tok_match — терпит
    словоформы). Отдельно от длины (её считает validate/rsa_len — golden rule #4). Пустые ключи/
    заголовки → 1.0 (нечего мерить, не флагаем). Токен ≥3 символов — отсекаем шум/стоп-слова.

    Живёт здесь, а не в adcopy.generate: релевантность, как и длину, считает КОД — и аудит
    (`audit.engine`) обязан считать её ТЕМ ЖЕ кодом, не импортируя генератор с его LLM-клиентом."""
    kw_tokens = {
        t for kw in keywords for t in re.split(r"[^0-9a-zа-яё]+", kw.casefold()) if len(t) >= 3
    }
    if not kw_tokens or not headlines:
        return 1.0

    def _covered(h: str) -> bool:
        htoks = {t for t in re.split(r"[^0-9a-zа-яё]+", h.casefold()) if t}
        return any(tok_match(ht, kt) for ht in htoks for kt in kw_tokens)

    hit = sum(1 for h in headlines if _covered(h))
    return round(hit / len(headlines), 2)


def find_duplicates(items: list[str]) -> list[tuple[int, str]]:
    """D5: повторяющиеся элементы набора (casefold + strip). Возвращает [(1-based индекс, текст)]
    ВТОРЫХ и последующих вхождений (первое — не дубль). Google Ads отклоняет RSA с одинаковыми
    заголовками/описаниями (asset может повторяться, но в одном объявлении дубли не проходят) —
    ловим ДО создания, чтобы не тратить черновик на серверный отказ."""
    seen: set[str] = set()
    dupes: list[tuple[int, str]] = []
    for i, t in enumerate(items, 1):
        key = (t or "").strip().casefold()
        if key in seen:
            dupes.append((i, t))
        else:
            seen.add(key)
    return dupes


# ── §3-assets: лимиты длины ассетов-расширений (КОД, code points, кириллица=1) ────
# Считаем тем же rsa_len (кириллица=1, CJK=2). Источник лимитов — справка Google Ads (v24).
ASSET_LIMITS = {
    "sitelink_text": 25,  # link_text
    "sitelink_desc": 35,  # description1/description2
    "callout": 25,  # callout_text
    "snippet_value": 25,  # каждое значение Structured Snippet
    "business_name": 25,
    "promotion_target": 20,
    "price_header": 25,
    "price_desc": 25,
}

# Канонический АНГЛИЙСКИЙ список заголовков Structured Snippet (иначе API → HEADER_NOT_FOUND).
# Локализация — в values, header — строго из этого набора.
STRUCTURED_SNIPPET_HEADERS = frozenset(
    {
        "Amenities",
        "Brands",
        "Courses",
        "Degree programs",
        "Destinations",
        "Featured hotels",
        "Insurance coverage",
        "Models",
        "Neighborhoods",
        "Service catalog",
        "Shows",
        "Show types",
        "Styles",
        "Types",
    }
)


def asset_len_ok(text: str, kind: str) -> tuple[bool, int]:
    """(укладывается_ли, длина) для текста ассета-расширения. kind — ключ ASSET_LIMITS.
    Длину считает КОД (rsa_len: кириллица=1, CJK=2) — зеркалит RSA-дисциплину."""
    if kind not in ASSET_LIMITS:
        raise ValueError(f"неизвестный тип ассет-текста: {kind}")
    n = rsa_len(text)
    return n <= ASSET_LIMITS[kind], n


def assert_asset_len(text: str, kind: str) -> str:
    ok, n = asset_len_ok(text, kind)
    if not ok:
        raise ValueError(f"{kind} превышает лимит {ASSET_LIMITS[kind]}: {n} симв. — «{text}»")
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


# ── §10: наличие призыва к действию (CTA) — advisory-эвристика (КОД, не только промпт) ──
# Мультиязычный лексикон СТЕМОВ императивов/призывов (RU/EN/UK). Стемы (не полные слова), т.к. RU/UK
# спрягаются: «куп» ловит купи/купить/купите/купуй. Сопоставление — по началу токена (startswith),
# регистронезависимо. Best practice §10: хотя бы ОДИН CTA в наборе объявления (не в каждом тексте).
_CTA_STEMS = frozenset(
    {
        # EN
        "buy",
        "order",
        "shop",
        "get",
        "call",
        "book",
        "try",
        "learn",
        "discover",
        "save",
        "download",
        "start",
        "find",
        "compare",
        "request",
        "contact",
        "explore",
        "join",
        "grab",
        "claim",
        "visit",
        "subscribe",
        "sign",
        "register",
        "apply",
        "choose",
        "hurry",
        # RU (стемы)
        "куп",
        "закаж",
        "заказ",
        "звони",
        "узна",
        "получ",
        "попроб",
        "оформ",
        "выбер",
        "выбир",
        "жми",
        "успе",
        "скач",
        "подпи",
        "начн",
        "перейд",
        "закаж",
        "приход",
        "брон",
        "сравн",
        "звоните",
        "оставь",
        "регистр",
        "получи",
        # UK (стемы)
        "замов",
        "купуй",
        "дізна",
        "отрим",
        "спробу",
        "телефону",
        "обер",
        "почн",
        "приєдн",
        "завантаж",
        "зателефону",
    }
)


def has_cta(text: str) -> bool:
    """Есть ли в тексте призыв к действию (по лексикону стемов). Advisory-эвристика (§10)."""
    for w in _WORD_RE.findall((text or "").lower()):
        if w in _CTA_STEMS or any(w.startswith(stem) for stem in _CTA_STEMS if len(stem) >= 3):
            return True
    return False


def any_cta(texts: list[str]) -> bool:
    """Есть ли CTA хотя бы в одном тексте набора (best practice §10 — минимум один призыв)."""
    return any(has_cta(t) for t in texts if isinstance(t, str))
