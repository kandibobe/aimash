"""Офлайн-тесты keywords/cluster.py (ТЗ §7): нормализация интента, парсинг кластеров/минус-слов,
приоритезация, fallback кластеризации. Чистые функции — без сети; LLM подменяется заглушкой.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keywords.cluster as C  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _fake_chat(content: str | None = None, *, raises: bool = False):
    async def _chat(messages, **kwargs):
        if raises:
            raise RuntimeError("LLM down")
        return SimpleNamespace(content=content)

    return _chat


# ── normalize_intent: КОД сводит ответ модели к таксономии §7 (5 значений) ────────
def test_normalize_intent_synonyms_and_unknown():
    assert C.normalize_intent("transactional") == "транзакционный"
    assert C.normalize_intent("  Коммерческий ") == "коммерческий"
    # P2 (живой тест 2026-07-06): бренды/конкуренты — СВОЯ категория, не навигационный
    assert C.normalize_intent("brand") == "брендовый"
    assert C.normalize_intent("branded") == "брендовый"
    assert C.normalize_intent("конкурентный") == "брендовый"
    assert C.normalize_intent("competitor") == "брендовый"
    assert C.normalize_intent("информационный") == "информационный"
    assert C.normalize_intent("чтотоещё") == ""  # неизвестное → пусто (не показываем мусор)
    assert C.normalize_intent("") == ""


def test_intent_taxonomy_consistency_guards():
    """Класс-гард «добавил категорию — забыл карту»: каждая категория INTENTS нормализуется в
    себя, имеет вес приоритезации и упомянута в промпте кластеризации."""
    assert all(C.normalize_intent(x) == x for x in C.INTENTS)
    assert set(C._INTENT_WEIGHT) == set(C.INTENTS)
    assert all(x in C._SYSTEM for x in C.INTENTS)


def test_branded_cluster_ranks_between_commercial_and_navigational():
    """P2: вес «брендовый» между коммерческим и навигационным (конкурентные бренды — горячий
    спрос). Одинаковый объём ⇒ порядок по весу интента."""
    clusters = [
        C.Cluster(name="Nav", intent="навигационный", keywords=["a"]),
        C.Cluster(name="Brand", intent="брендовый", keywords=["b"]),
        C.Cluster(name="Comm", intent="коммерческий", keywords=["c"]),
    ]
    ranked = C.rank_clusters(clusters, {"a": 100, "b": 100, "c": 100})
    assert [c.name for c in ranked] == ["Comm", "Brand", "Nav"]


# ── _parse: только реальные ключи, дедуп между группами, leftover → «Прочее» ──────
def test_parse_keeps_only_real_keywords_and_dedups():
    valid = ["купить телефон", "смартфон цена", "обзор смартфона"]
    content = json.dumps(
        [
            {
                "name": "Покупка",
                "intent": "transactional",
                "keywords": ["Купить Телефон", "выдумка"],
            },
            {
                "name": "Сравнение",
                "intent": "commercial",
                "keywords": ["смартфон цена", "купить телефон"],
            },
        ],
        ensure_ascii=False,
    )
    clusters = C._parse(content, valid)
    # «выдумка» отброшена (не из valid); «купить телефон» только в первой группе (дедуп);
    # casefold-возврат к исходному написанию.
    g0 = next(c for c in clusters if c.name == "Покупка")
    assert g0.keywords == ["купить телефон"] and g0.intent == "транзакционный"
    g1 = next(c for c in clusters if c.name == "Сравнение")
    assert g1.keywords == ["смартфон цена"]  # дубль «купить телефон» убран
    # неразложенный «обзор смартфона» → «Прочее»
    leftover = next(c for c in clusters if c.name == "Прочее")
    assert leftover.keywords == ["обзор смартфона"]


def test_parse_bad_json_and_non_list_return_empty():
    assert C._parse("не json вовсе", ["a"]) == []
    assert C._parse(json.dumps({"not": "list"}), ["a"]) == []
    assert C._parse("", ["a"]) == []


# ── rank_clusters: priority = объём × вес интента; «Прочее» всегда в конце ─────────
def test_rank_clusters_orders_by_priority_leftover_last():
    info = C.Cluster(name="Инфо", intent="информационный", keywords=["как выбрать"])
    trans = C.Cluster(name="Покупка", intent="транзакционный", keywords=["купить"])
    leftover = C.Cluster(name="Прочее", intent="", keywords=["прочий ключ"])
    by_text = {"как выбрать": 1000, "купить": 1000, "прочий ключ": 100000}
    ranked = C.rank_clusters([info, leftover, trans], by_text)
    # транзакционный (вес 1.0) выше информационного (0.3) при равном объёме; «Прочее» — последним
    # несмотря на огромный объём (технические группы вниз).
    assert [c.name for c in ranked] == ["Покупка", "Инфо", "Прочее"]
    assert trans.priority == 1000.0 and info.priority == 300.0


# ── _parse_neg: дедуп, нижний регистр, лимит ──────────────────────────────────────
def test_parse_neg_dedup_lowercase_limit():
    content = json.dumps(["Бесплатно", "бесплатно", "СКАЧАТЬ", "вакансии", "отзывы"])
    assert C._parse_neg(content, limit=3) == ["бесплатно", "скачать", "вакансии"]
    assert C._parse_neg("мусор", 10) == []


# ── cluster_keywords: fallback на «Все ключи» при сбое/пустом; усечение → «Прочее» ─
async def test_cluster_keywords_fallback_on_llm_failure():
    with patched(C, "chat", _fake_chat(raises=True)):
        clusters = await C.cluster_keywords(["цветы", "розы", "цветы"], "ru")
    assert len(clusters) == 1
    assert clusters[0].name == "Все ключи"
    assert clusters[0].keywords == ["цветы", "розы"]  # дедуп + порядок


async def test_cluster_keywords_empty_input():
    with patched(C, "chat", _fake_chat(raises=True)):
        assert await C.cluster_keywords(["", "   "], "ru") == []


async def test_cluster_keywords_parses_llm_and_marks_overflow():
    valid_json = json.dumps(
        [{"name": "Группа", "intent": "коммерческий", "keywords": ["a", "b"]}], ensure_ascii=False
    )
    big = [f"k{i}" for i in range(C.MAX_CLUSTER_INPUT + 5)] + ["a", "b"]
    with patched(C, "chat", _fake_chat(valid_json)):
        clusters = await C.cluster_keywords(big, "ru")
    # вход усекли до MAX_CLUSTER_INPUT → остаток отдельной группой (без «тихой» потери ключей)
    assert any(c.name.startswith("Прочее (сверх лимита)") for c in clusters)


# ── suggest_negative_keywords: пустой ввод → []; парс ответа LLM ──────────────────
async def test_suggest_negatives_empty_input_returns_empty():
    with patched(C, "chat", _fake_chat(raises=True)):
        assert await C.suggest_negative_keywords("", [], language="ru") == []


async def test_suggest_negatives_parses_llm():
    with patched(C, "chat", _fake_chat(json.dumps(["бесплатно", "скачать"]))):
        out = await C.suggest_negative_keywords("доставка цветов", ["купить букет"], language="ru")
    assert out == ["бесплатно", "скачать"]
