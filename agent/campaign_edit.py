"""§19.8: разбор и применение правок финальной сводки кампании командой в свободной форме.

Поддержаны два класса правок:
1. «замена X на Y» («смени телефон с +X на +Y», «смени структурное описание „A“ на „B“») — КОД
   делает буквальную замену по текстовым полям черновика (объявление, display path, URL, ассеты),
   с ре-валидацией длин (кириллица=1) — невалидные после замены поля не трогаются.
2. правка настроек («поставь бюджет 60», «добавь город Найроби») — через agent.campaign_settings
   (вызывает bot.main, тут — только парсер замены).

Это правка ЧЕРНОВИКА, не мутация: SDK не трогается, confirmation_id не тратится; финальный
confirm-гейт (Создать черновик) покажет итог и спросит «да».
"""

from __future__ import annotations

import re

from adcopy.validate import ASSET_LIMITS, LIMITS, moderation_issues, rsa_len

# Ключи params ассета → тип лимита (ASSET_LIMITS) для ре-валидации длины при замене (§19.8).
_ASSET_KEY_KIND = {
    "link_text": "sitelink_text",
    "description1": "sitelink_desc",
    "description2": "sitelink_desc",
    "callouts": "callout",  # список → элементы получают этот kind
    "values": "snippet_value",  # список значений структурного описания
    "business_name": "business_name",
    "promotion_target": "promotion_target",
}

# «… с X на Y» / «… X на Y» / change|replace X to|with Y. Кавычки разных видов опциональны.
_Q = r"[«»\"'„“”]"
_PATTERNS = [
    re.compile(rf"\bс\s+{_Q}?(.+?){_Q}?\s+на\s+{_Q}?(.+?){_Q}?\s*$", re.IGNORECASE | re.DOTALL),
    re.compile(rf"{_Q}(.+?){_Q}\s+на\s+{_Q}(.+?){_Q}", re.IGNORECASE | re.DOTALL),
    re.compile(
        rf"\b(?:change|replace)\s+{_Q}?(.+?){_Q}?\s+(?:to|with)\s+{_Q}?(.+?){_Q}?\s*$",
        re.IGNORECASE | re.DOTALL,
    ),
]


def parse_replace_edit(text: str) -> tuple[str, str] | None:
    """Распознать команду замены «… X на Y» → (old, new). None — не команда замены."""
    t = (text or "").strip()
    for rx in _PATTERNS:
        m = rx.search(t)
        if m:
            old = m.group(1).strip(" «»\"'„“”")
            new = m.group(2).strip(" «»\"'„“”")
            if old and new and old != new:
                return old, new
    return None


def _replace_kind_ok(new_value: str, kind: str | None) -> bool:
    """Влезает ли значение после замены в лимит поля (None — без лимита: URL/телефон и т.п.).
    Лимит ищем в RSA-лимитах (headline/description/path) и в лимитах ассетов (callout/sitelink/…)."""
    if kind is None:
        return True
    limit = LIMITS.get(kind) or ASSET_LIMITS.get(kind)
    if limit is None:
        return True
    return rsa_len(new_value) <= limit


def _repl_in_str(s: str, old: str, new: str, kind: str | None) -> tuple[str, int]:
    """Заменить old→new в строке; откатить, если результат не влезает в лимит kind ИЛИ вносит
    НОВЫЕ политика-проблемы (§19.8: пере-верификация «длины, политики» и на ручной замене —
    CAPS/спам-символы ловятся не только на генерации). Уже существующие проблемы не блокируют
    замену (не ухудшаем — можно)."""
    if old not in s:
        return s, 0
    candidate = s.replace(old, new)
    if not _replace_kind_ok(candidate, kind):
        return s, 0  # после замены поле превысило лимит → не трогаем (golden rule #4)
    if kind in ("headline", "description") and not set(moderation_issues(candidate)) <= set(
        moderation_issues(s)
    ):
        return s, 0  # замена внесла КАПС/спам, которых не было → не трогаем (политики Google)
    return candidate, 1


def _repl_in_list(items: list, old: str, new: str, kind: str | None) -> int:
    n = 0
    for i, it in enumerate(items):
        if isinstance(it, str):
            items[i], c = _repl_in_str(it, old, new, kind)
            n += c
    return n


def _repl_in_obj(obj, old: str, new: str, kind: str | None = None) -> int:
    """Рекурсивная замена в структуре ассетов (assets.new params): str-листья с ре-валидацией длины
    по типу поля (kind наследуется в списках от родительского ключа: callouts→callout, values→…)."""
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_kind = _ASSET_KEY_KIND.get(k, None)
            if isinstance(v, str):
                obj[k], c = _repl_in_str(v, old, new, child_kind)
                n += c
            else:
                n += _repl_in_obj(v, old, new, child_kind)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                obj[i], c = _repl_in_str(v, old, new, kind)
                n += c
            else:
                n += _repl_in_obj(v, old, new, kind)
    return n


def apply_text_replace(state: dict, old: str, new: str) -> int:
    """Буквальная замена old→new по текстовым полям черновика (мутирует state на месте). Возвращает
    число применённых замен. Длины заголовков/описаний/path ре-валидируются (кириллица=1)."""
    n = 0
    ad = state.get("ad") or {}
    n += _repl_in_list(ad.get("headlines") or [], old, new, "headline")
    n += _repl_in_list(ad.get("descriptions") or [], old, new, "description")
    for key in ("final_url",):
        if isinstance(ad.get(key), str):
            ad[key], c = _repl_in_str(ad[key], old, new, None)
            n += c
    for key in ("path1", "path2"):
        if isinstance(ad.get(key), str):
            ad[key], c = _repl_in_str(ad[key], old, new, "path")
            n += c
    # ассеты (assets.new — список {family, params}); URL-опции
    n += _repl_in_obj(state.get("assets") or {}, old, new)
    n += _repl_in_obj(state.get("url_options") or {}, old, new)
    return n
