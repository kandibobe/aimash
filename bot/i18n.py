"""Каркас локализации RU/EN (ТЗ §4). Инкрементальная миграция: bot/texts.py продолжает
работать как есть; t() берёт перевод из CATALOG, а для НЕ-мигрированных ключей мостит к
texts.<KEY>. Так можно переводить сообщения по одному, не ломая RU.

Хранилище языка — in-memory (теряется при рестарте, как _CAMP_CACHE — для предпочтения это ок;
дефолт RU). Колонку UserSettings.language + миграцию вводим позже (после «остывания» горячих
файлов параллельного процесса), чтобы не конфликтовать по db/models.py и head Alembic.
"""

from __future__ import annotations

from bot import texts

LANGS = ("ru", "en")
DEFAULT_LANG = "ru"

# Мигрированные сообщения. Ключ → {lang: текст}. RU задаём ЛИТЕРАЛАМИ (а не texts.X), чтобы
# каталог не падал на импорте, если параллельный процесс переименует константу в texts.py.
CATALOG: dict[str, dict[str, str]] = {
    "executing": {"ru": "⏳ Выполняю…", "en": "⏳ Working…"},
    "rejected": {"ru": "❌ Отменено", "en": "❌ Cancelled"},
    "stale": {"ru": "Черновик не найден или устарел", "en": "Draft not found or expired."},
    "no_proposal": {
        "ru": "Нет активного черновика для отмены.",
        "en": "No active draft to cancel.",
    },
    "lang_pick": {"ru": "🌐 Выбери язык интерфейса:", "en": "🌐 Choose interface language:"},
    "lang_set": {"ru": "🌐 Язык интерфейса: русский.", "en": "🌐 Interface language: English."},
}

_CHAT_LANG: dict[int, str] = {}


def normalize_lang(lang: str | None) -> str:
    return lang if lang in LANGS else DEFAULT_LANG


def get_lang(chat_id: int) -> str:
    return _CHAT_LANG.get(chat_id, DEFAULT_LANG)


def set_lang(chat_id: int, lang: str | None) -> str:
    norm = normalize_lang(lang)
    _CHAT_LANG[chat_id] = norm
    return norm


def t(key: str, lang: str = DEFAULT_LANG, /, **kw: object) -> str:
    """Перевод по ключу. Приоритет: CATALOG[key][lang] → CATALOG[key][RU] → texts.<KEY> (мост) → key.
    Если переданы kw — применяется .format(**kw) (совместимо с texts.X.format(...))."""
    lang = normalize_lang(lang)
    entry = CATALOG.get(key)
    if entry is not None:
        s = entry.get(lang) or entry.get(DEFAULT_LANG) or key
    else:
        s = getattr(texts, key.upper(), key)  # мост к не-мигрированным RU-константам
    return s.format(**kw) if kw else s
