"""§19.4: офлайн-тесты keyword-конвейера — seeds (LLM), relevance filter (LLM), ingest (парсер).

LLM подменяется заглушкой; парсер чист. Деньги/валидность ключей считает КОД (golden rule #4).
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keywords.filter as KF  # noqa: E402
import keywords.seeds as KS  # noqa: E402
from keywords.ingest import (  # noqa: E402
    DEFAULT_MATCH_TYPE,
    parse_keywords_text,
    parse_match_type_instruction,
)


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _chat(content):
    async def _c(messages, **kw):
        return SimpleNamespace(content=content)

    return _c


# ── seeds ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_generate_seeds_parses_json_array():
    content = json.dumps(["used cars kenya", "second hand cars", "used cars kenya"])  # дубль
    with patched(KS, "chat", _chat(content)):
        seeds = await KS.generate_seed_keywords(topic="used cars", n=10)
    assert seeds == ["used cars kenya", "second hand cars"]  # дедуп, порядок


@pytest.mark.asyncio
async def test_generate_seeds_fallback_on_bad_json():
    with patched(KS, "chat", _chat("не json")):
        seeds = await KS.generate_seed_keywords(topic="доставка цветов киев", n=5)
    assert seeds  # эвристика из темы → не пусто


@pytest.mark.asyncio
async def test_generate_seeds_empty_input():
    with patched(KS, "chat", _chat("[]")):
        seeds = await KS.generate_seed_keywords(topic="", profile="")
    assert seeds == []


# ── relevance filter ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_filter_relevance_marks_and_failopen():
    content = json.dumps({"used cars nairobi": True, "free car games": False})
    with patched(KF, "chat", _chat(content)):
        verdicts = await KF.filter_relevance(
            texts=["used cars nairobi", "free car games", "car rental"], topic="used cars"
        )
    assert verdicts["used cars nairobi"] is True
    assert verdicts["free car games"] is False
    assert verdicts["car rental"] is True  # отсутствует в ответе → fail-open (релевантно)


# ── ingest ──────────────────────────────────────────────────────────────────────────
def test_parse_keywords_markers_and_default():
    text = '[used cars], "second hand", cheap cars'
    out = parse_keywords_text(text)
    by = {k.text: k.match_type for k in out}
    assert by["used cars"] == "exact"
    assert by["second hand"] == "phrase"
    assert by["cheap cars"] == DEFAULT_MATCH_TYPE  # phrase


def test_parse_match_type_instruction():
    assert (
        parse_match_type_instruction("используй фразовое соответствие для всего списка") == "phrase"
    )
    assert parse_match_type_instruction("use exact match") == "exact"
    assert parse_match_type_instruction("просто список без инструкции") is None


def test_parse_keywords_global_instruction_applies():
    text = "используй точное соответствие\nused cars\nsecond hand cars"
    out = parse_keywords_text(text)
    texts = [k.text for k in out]
    # инструкция-строка НЕ попадает в ключи (раньше «used cars», «second hand cars» — exact)
    assert "used cars" in texts and "second hand cars" in texts
    assert all("соответств" not in t for t in texts)  # инструкция отброшена
    assert {k.match_type for k in out} == {"exact"}
    assert len(out) == 2  # ровно 2 ключа, без мусорной инструкции


def test_global_instruction_beats_screen_default():
    """C3 (§19.4.1): «используй широкое соответствие для всего списка» побеждает default экрана.
    Раньше default (в визарде он задан ВСЕГДА) перекрывал инструкцию → §19.4.1 не работал в визарде."""
    out = parse_keywords_text(
        "используй широкое соответствие для всего списка\nused cars\ncheap cars",
        default_match_type="exact",  # дефолт визарда с Этапа 1
    )
    assert {k.match_type for k in out} == {"broad"}


def test_per_keyword_marker_still_beats_global_instruction():
    out = parse_keywords_text(
        'используй широкое соответствие\n[used cars]\n"second hand"\ncheap cars',
        default_match_type="exact",
    )
    by = {k.text: k.match_type for k in out}
    assert by["used cars"] == "exact"  # маркер сильнее инструкции
    assert by["second hand"] == "phrase"
    assert by["cheap cars"] == "broad"  # инструкция сильнее default экрана


def test_screen_default_used_when_no_instruction():
    out = parse_keywords_text("used cars\ncheap cars", default_match_type="exact")
    assert {k.match_type for k in out} == {"exact"}  # прежнее поведение сохранено


def test_filter_coerce_string_false():
    import keywords.filter as _KF

    assert _KF._coerce_bool("false") is False
    assert _KF._coerce_bool("no") is False
    assert _KF._coerce_bool("нет") is False
    assert _KF._coerce_bool("true") is True
    assert _KF._coerce_bool(True) is True
    assert _KF._coerce_bool(0) is False
    assert _KF._coerce_bool("maybe") is None  # неразборчивое → None (fail-open у вызывающего)


@pytest.mark.asyncio
async def test_filter_relevance_string_false_marks_irrelevant():
    content = json.dumps({"free car games": "false", "used cars": "yes"})
    with patched(KF, "chat", _chat(content)):
        v = await KF.filter_relevance(texts=["free car games", "used cars"], topic="cars")
    assert v["free car games"] is False  # строковый 'false' НЕ стал True
    assert v["used cars"] is True
