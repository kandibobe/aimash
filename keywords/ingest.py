"""§19.4.1: разбор переданных менеджером ключевых слов (текст/файл) + тип соответствия.

Офлайн (без сети/SDK). Поддерживает:
- вставку текстом: по строке или через запятую;
- per-keyword маркеры типа соответствия: [точное] → exact, "фразовое" → phrase, иначе default;
- глобальную инструкцию о типе («используй фразовое соответствие для всего списка»).
Файлы (XLSX/CSV/текст) бот превращает в текст через core.ingest и подаёт сюда (одна фраза в строке).

Длину/валидность каждого ключа считает КОД (ads.validation.assert_keyword_ok через normalize).
"""

from __future__ import annotations

from dataclasses import dataclass

from ads.validation import assert_keyword_ok

MATCH_TYPES = ("broad", "phrase", "exact")
DEFAULT_MATCH_TYPE = "phrase"  # §19.4: по умолчанию Phrase

# Слова-признаки типа соответствия в свободной инструкции (RU/EN).
_MT_HINTS = {
    "broad": ("broad", "широк"),
    "phrase": ("phrase", "фраз"),
    "exact": ("exact", "точн"),
}


@dataclass
class KeywordInput:
    text: str
    match_type: str  # broad | phrase | exact


def parse_match_type_instruction(text: str) -> str | None:
    """Вытащить тип соответствия из инструкции («используй точное соответствие …»). None — не задан.
    Срабатывает только если есть слово «соответств»/«match» рядом — иначе можно поймать ложно."""
    low = (text or "").casefold()
    if "соответств" not in low and "match" not in low:
        return None
    for mt, hints in _MT_HINTS.items():
        if any(h in low for h in hints):
            return mt
    return None


def _strip_markers(token: str) -> tuple[str, str | None]:
    """Снять per-keyword маркеры: [exact] / "phrase". Возвращает (текст, тип|None)."""
    t = token.strip()
    if len(t) >= 2 and t[0] == "[" and t[-1] == "]":
        return t[1:-1].strip(), "exact"
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1].strip(), "phrase"
    return t, None


def _tokens(text: str) -> list[str]:
    """Разбить ввод на токены-ключи: по строкам; строку без маркеров — ещё и по запятым."""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if ("[" in line or '"' in line) and "," not in line:
            out.append(line)  # строка-маркер целиком
        else:
            out.extend(p for p in line.split(",") if p.strip())
    return out


def parse_keywords_text(text: str, *, default_match_type: str | None = None) -> list[KeywordInput]:
    """Текст → список KeywordInput с типом соответствия. Глобальная инструкция (если есть в тексте)
    и default перекрываются per-keyword маркерами. Невалидные ключи (через assert_keyword_ok)
    отбрасываются. Дедуп по (text, match_type)."""
    global_mt = parse_match_type_instruction(text)
    base_mt = default_match_type or global_mt or DEFAULT_MATCH_TYPE
    if base_mt not in MATCH_TYPES:
        base_mt = DEFAULT_MATCH_TYPE
    # Строки-инструкции о типе соответствия — НЕ ключи: выкидываем их перед токенизацией, иначе
    # «используй точное соответствие …» попало бы в список как мусорный ключ.
    cleaned = "\n".join(
        ln for ln in (text or "").splitlines() if parse_match_type_instruction(ln) is None
    )
    out: list[KeywordInput] = []
    seen: set[tuple[str, str]] = set()
    for tok in _tokens(cleaned):
        body, marker = _strip_markers(tok)
        if not body:
            continue
        try:
            clean = assert_keyword_ok(body)
        except Exception:  # noqa: BLE001 — мусорный/слишком длинный ключ молча пропускаем
            continue
        mt = marker or base_mt
        key = (clean, mt)
        if key not in seen:
            seen.add(key)
            out.append(KeywordInput(text=clean, match_type=mt))
    return out
