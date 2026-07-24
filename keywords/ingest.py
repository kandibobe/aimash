"""§19.4.1: разбор переданных менеджером ключевых слов (текст/файл) + тип соответствия.

Офлайн (без сети/SDK). Поддерживает:
- вставку текстом: по строке или через запятую;
- per-keyword маркеры типа соответствия: [точное] → exact, "фразовое" → phrase, иначе default;
- глобальную инструкцию о типе («используй фразовое соответствие для всего списка»);
- ТАБЛИЦУ из XLSX/CSV: «ключ | тип соответствия» (+ служебные колонки объёма/конкуренции) — тип
  берётся из колонки, заголовки и служебные ячейки в ключи НЕ превращаются (§19.4.1).
Файлы (XLSX/CSV/текст) бот превращает в текст через core.ingest и подаёт сюда.

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


def strip_match_markers(token: str) -> tuple[str, str | None]:
    """Снять per-keyword маркеры: [exact] / "phrase". Возвращает (текст, тип|None).
    Публичная: тем же способом маркеры снимает /addkeys (иначе `[ремонт]` уезжал в Google как есть
    → KEYWORD_HAS_INVALID_CHARS)."""
    t = token.strip()
    if len(t) >= 2 and t[0] == "[" and t[-1] == "]":
        return t[1:-1].strip(), "exact"
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1].strip(), "phrase"
    return t, None


_strip_markers = strip_match_markers  # внутреннее имя (совместимость)

# ── §19.4.1: ТАБЛИЧНЫЙ ввод (XLSX/CSV) ────────────────────────────────────────────
# core.ingest даёт файл текстом: лист → строка «# Имя листа», строка книги → «ячейка, ячейка».
# Раньше такие строки резались по запятой как список ключей → ВТОРАЯ КОЛОНКА И ЗАГОЛОВКИ
# становились «ключами»: файл 200×2 давал ~401 ключ, а «exact»/«Match type»/«# Keywords» уезжали
# в кампанию. Теперь строка с сервисной ячейкой (тип соответствия / число / заголовок) читается
# как ТАБЛИЧНАЯ: ключ — первая ячейка, тип — из колонки типа.
_MT_CELLS = {
    "broad": "broad",
    "широкое": "broad",
    "широкий": "broad",
    "широкое соответствие": "broad",
    "phrase": "phrase",
    "фразовое": "phrase",
    "фразовое соответствие": "phrase",
    "exact": "exact",
    "точное": "exact",
    "точный": "exact",
    "точное соответствие": "exact",
}
# Заголовки колонок (RU/EN) — не ключи. Первая ячейка из этого набора ⇒ строка-заголовок целиком.
_HEADER_CELLS = {
    "keyword",
    "keywords",
    "key",
    "match type",
    "match_type",
    "matchtype",
    "type",
    "volume",
    "competition",
    "relevant",
    "ключ",
    "ключи",
    "ключевое слово",
    "ключевые слова",
    "запрос",
    "тип",
    "тип соответствия",
    "объём",
    "объем",
    "конкуренция",
    "релевантно",
    "релевантность",
}


def _is_numeric_cell(cell: str) -> bool:
    """Ячейка — только число (объём/ставка из экспорта таблицы), а не ключ. «24 часа» — НЕ число."""
    t = cell.replace(" ", "").replace(" ", "").replace("%", "").replace(",", ".")
    if not t:
        return False
    try:
        float(t)
    except ValueError:
        return False
    return True


def _rows(text: str) -> list[tuple[str, str | None]]:
    """Ввод → [(ключ, тип|None)]. Три формы: список через запятую («ключ1, ключ2»), таблица
    («ключ, exact» / «ключ, 1000, HIGH» — из XLSX/CSV) и маркеры («[ключ]» / «"ключ"»).
    Служебные строки («# Лист1», заголовки колонок) отбрасываются."""
    out: list[tuple[str, str | None]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):  # «# Имя листа» из core.ingest._xlsx_to_text
            continue
        if ("[" in line or '"' in line) and "," not in line:
            out.append(strip_match_markers(line))  # строка-маркер целиком
            continue
        cells = [c.strip() for c in line.split(",") if c.strip()]
        if not cells:
            continue
        if cells[0].casefold() in _HEADER_CELLS:  # строка-заголовок таблицы
            continue
        tail = cells[1:]
        mt = next((_MT_CELLS[c.casefold()] for c in tail if c.casefold() in _MT_CELLS), None)
        service = mt is not None or any(
            c.casefold() in _HEADER_CELLS or _is_numeric_cell(c) for c in tail
        )
        if service:  # табличная строка: ключ — первая ячейка, остальные ячейки — служебные
            body, marker = strip_match_markers(cells[0])
            out.append((body, marker or mt))
        else:  # обычный список ключей через запятую
            out.extend(strip_match_markers(c) for c in cells)
    return out


def parse_keywords_text(text: str, *, default_match_type: str | None = None) -> list[KeywordInput]:
    """Текст → список KeywordInput с типом соответствия. Приоритет (§19.4.1, C3):
    per-keyword маркер `[exact]`/`"phrase"` > глобальная инструкция в самом тексте
    («используй широкое соответствие для всего списка») > default экрана > DEFAULT_MATCH_TYPE.

    Раньше default перекрывал глобальную инструкцию — а default в визарде задан ВСЕГДА
    (`_cc_default_match_type`), так что инструкция §19.4.1 не работала ровно в том флоу, для
    которого написана. Невалидные ключи (assert_keyword_ok) отбрасываются; дедуп по (text, mt)."""
    global_mt = parse_match_type_instruction(text)
    base_mt = global_mt or default_match_type or DEFAULT_MATCH_TYPE
    if base_mt not in MATCH_TYPES:
        base_mt = DEFAULT_MATCH_TYPE
    # Строки-инструкции о типе соответствия — НЕ ключи: выкидываем их перед токенизацией, иначе
    # «используй точное соответствие …» попало бы в список как мусорный ключ.
    cleaned = "\n".join(
        ln for ln in (text or "").splitlines() if parse_match_type_instruction(ln) is None
    )
    out: list[KeywordInput] = []
    seen: set[tuple[str, str]] = set()
    for body, marker in _rows(cleaned):
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
