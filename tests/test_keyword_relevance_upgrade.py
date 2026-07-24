"""Апгрейд релевантности keyword/минус-слов (2026-07): смысловые задачи на роли `keywords`,
профиль/бренд-защита минус-слов, использование вердиктов релевантности (rank/сводка/экспорт),
покрытие ключей в RSA. Чистая логика + маршрутизация ролей (без сети/LLM/БД)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keywords.cluster as C  # noqa: E402
import keywords.filter as F  # noqa: E402
import keywords.seeds as S  # noqa: E402
from adcopy.generate import _keyword_coverage, _tok_match  # noqa: E402
from keywords.cluster import Cluster, _drop_protected, _neg_system, rank_clusters  # noqa: E402
from keywords.export import _headers, _relevance_cell, build_workbook  # noqa: E402


def _stub_chat(capture: dict, content: str):
    """Async-заглушка router.chat: пишет role в capture, отдаёт message с .content."""

    async def _chat(messages, *, role="parsing", **kw):
        capture["role"] = role
        return SimpleNamespace(content=content)

    return _chat


# ── Маршрутизация: смысловые keyword-задачи идут на роль `keywords` (не parsing) ──
async def test_seeds_use_keywords_role(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(S, "chat", _stub_chat(cap, '["купить телефон","смартфон"]'))
    out = await S.generate_seed_keywords(topic="телефоны")
    assert out and cap["role"] == "keywords"


async def test_filter_relevance_uses_keywords_role(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(F, "chat", _stub_chat(cap, '["k2"]'))  # контракт: только НЕрелевантные
    rel = await F.filter_relevance(texts=["k1", "k2"], topic="t", profile="бизнес X")
    assert cap["role"] == "keywords"
    assert rel == {"k1": True, "k2": False}


async def test_negatives_and_clustering_use_keywords_role(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(C, "chat", _stub_chat(cap, '["бесплатно","скачать"]'))
    negs = await C.suggest_negative_keywords("тема", ["идея1"], profile="о клиенте")
    assert negs and cap["role"] == "keywords"

    cap2: dict = {}
    monkeypatch.setattr(
        C, "chat", _stub_chat(cap2, '[{"name":"A","intent":"коммерческий","keywords":["идея1"]}]')
    )
    cl = await C.cluster_keywords(["идея1"])
    assert cl and cap2["role"] == "keywords"


# ── Минус-слова: language-aware примеры + защита бренда/услуг ──
def test_neg_system_language_aware():
    assert "бесплатно" in _neg_system("ru")
    assert "free" in _neg_system("en") and "бесплатно" not in _neg_system("en")
    # неизвестный язык — без языко-специфичных примеров (нет RU-биаса)
    assert "бесплатно" not in _neg_system("sw") and "free" not in _neg_system("sw")


def test_drop_protected_brand_and_services():
    protected = {"toyota", "ремонт"}
    negs = ["бу", "toyota отзывы", "ремонт своими руками", "фото"]
    assert _drop_protected(negs, protected) == ["бу", "фото"]  # бренд/услуга не уходят в минус
    assert _drop_protected(negs, set()) == negs  # пустой protected → passthrough


# ── Вердикты релевантности реально используются: rank + сводка + экспорт ──
def test_rank_clusters_downranks_off_topic():
    a = Cluster(name="A", intent="транзакционный", keywords=["k1", "k2"])
    b = Cluster(name="B", intent="транзакционный", keywords=["k3"])
    by = {"k1": 1000, "k2": 1000, "k3": 500}
    ranked = rank_clusters([a, b], by, off_topic={"k1", "k2"})  # A целиком off-topic → тонет
    assert ranked[0].name == "B"
    # без off_topic A выше (больший объём)
    assert rank_clusters([a, b], by)[0].name == "A"


def test_export_relevance_column_conditional():
    assert "Релевантность" in _headers("", True)
    assert "Релевантность" not in _headers("", False)
    assert len(_headers("")) == 11  # обратная совместимость (без вердиктов)
    assert _relevance_cell({"k": False}, "k") == "низкая"
    assert _relevance_cell({"k": False}, "other") == "релевантно"  # fail-open


def test_build_workbook_adds_relevance_only_when_given():
    from ads.keyword_plan import KeywordIdea

    ideas = [KeywordIdea(text="k1", avg_monthly_searches=10, competition="LOW")]
    clusters = [Cluster(name="A", keywords=["k1"])]
    wb = build_workbook(clusters, ideas, relevance={"k1": False})
    ws = wb.active
    header_row = [c.value for c in next(r for r in ws.iter_rows() if r[0].value == "Кластер")]
    assert "Релевантность" in header_row
    # без relevance — колонки нет
    wb2 = build_workbook(clusters, ideas)
    ws2 = wb2.active
    header_row2 = [c.value for c in next(r for r in ws2.iter_rows() if r[0].value == "Кластер")]
    assert "Релевантность" not in header_row2


# ── Покрытие ключей в RSA (advisory, терпит словоформы) ──
def test_keyword_coverage_word_forms():
    assert _tok_match("ноутбука", "ноутбук")  # словоформа — префикс ≥4
    assert not _tok_match("кот", "код")  # разные короткие
    # 2 из 3 заголовков содержат словоформу ключа «ноутбук»
    cov = _keyword_coverage(["купить ноутбук", "доставка", "ремонт ноутбука"], ["ноутбук"])
    assert cov == round(2 / 3, 2)
    assert _keyword_coverage([], ["x"]) == 1.0  # нет заголовков → не флагаем
    assert _keyword_coverage(["a"], []) == 1.0  # нет ключей → не флагаем
