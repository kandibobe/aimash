"""Офлайн-тесты keyword research (Фаза 3, БЛОК E, ТЗ §13): подбор идей + кластеризация.

Без живого Google Ads — KeywordPlanIdeaService подменяется фейком; LLM-кластеризация — через
monkeypatch `chat`. Проверяем: замок аккаунта (golden rule #9) ДО любого запроса; парсинг рядов
и micros→валюту считает КОД; выбор seed-типа; сортировку; кластеризацию (только реальные ключи,
fallback при сбое); экспорт .xlsx; регистрацию read-tool keyword_research.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


# ── Фейковый SDK-клиент (повторяет форму v24, что нужна generate_keyword_ideas) ────
def _row(text, searches, comp="HIGH", idx=50, low=0, high=0, cpc=0):
    m = SimpleNamespace(
        avg_monthly_searches=searches,
        competition=SimpleNamespace(name=comp),
        competition_index=idx,
        low_top_of_page_bid_micros=low,
        high_top_of_page_bid_micros=high,
        average_cpc_micros=cpc,
    )
    return SimpleNamespace(text=text, keyword_idea_metrics=m)


class _Seed:
    def __init__(self):
        self.url = ""
        self.keywords = []


class _Req:
    def __init__(self):
        self.customer_id = ""
        self.language = ""
        self.geo_target_constants = []
        self.keyword_plan_network = None
        self.keyword_seed = _Seed()
        self.url_seed = _Seed()
        self.keyword_and_url_seed = _Seed()


class _Svc:
    def __init__(self, rows):
        self.rows = rows
        self.last_req = None

    def generate_keyword_ideas(self, request=None):
        self.last_req = request
        return list(self.rows)


class _Client:
    def __init__(self, rows):
        self.svc = _Svc(rows)
        self.enums = SimpleNamespace(
            KeywordPlanNetworkEnum=SimpleNamespace(GOOGLE_SEARCH="GOOGLE_SEARCH")
        )

    def get_service(self, name):
        assert name == "KeywordPlanIdeaService"
        return self.svc

    def get_type(self, name):
        assert name == "GenerateKeywordIdeasRequest"
        return _Req()


# ── generate_keyword_ideas ────────────────────────────────────────────────────────
def test_parses_metrics_and_sorts_by_volume():
    from ads.keyword_plan import generate_keyword_ideas

    rows = [
        _row("малый", 100, "LOW", 10, low=1_000_000, high=2_000_000, cpc=500_000),
        _row("большой", 5000, "HIGH", 90),
    ]
    client = _Client(rows)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        ideas = generate_keyword_ideas(client, DRAFT_ACCOUNT_ID, seeds=["сид"])
    assert [i.text for i in ideas] == ["большой", "малый"]  # сорт по объёму убыв.
    small = ideas[1]
    assert small.avg_monthly_searches == 100
    assert small.competition == "LOW" and small.competition_index == 10
    assert small.low_bid == 1.0 and small.high_bid == 2.0 and small.avg_cpc == 0.5  # micros→валюта


def test_rejects_foreign_account_before_any_call():
    from ads.keyword_plan import generate_keyword_ideas

    client = _Client([_row("x", 1)])
    with allowed_ids(DRAFT_ACCOUNT_ID), pytest.raises(PermissionError):
        generate_keyword_ideas(client, "1234567890", seeds=["a"])
    assert client.svc.last_req is None  # замок сработал ДО обращения к SDK


def test_requires_seed_or_url():
    from ads.keyword_plan import generate_keyword_ideas

    client = _Client([])
    with allowed_ids(DRAFT_ACCOUNT_ID), pytest.raises(ValueError):
        generate_keyword_ideas(client, DRAFT_ACCOUNT_ID, seeds=[], url=None)


def test_seed_type_keyword_and_url():
    from ads.keyword_plan import generate_keyword_ideas

    client = _Client([_row("x", 1)])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        generate_keyword_ideas(client, DRAFT_ACCOUNT_ID, seeds=["a", "b"], url="https://x.io")
    req = client.svc.last_req
    assert req.keyword_and_url_seed.url == "https://x.io"
    assert list(req.keyword_and_url_seed.keywords) == ["a", "b"]
    assert req.keyword_plan_network == "GOOGLE_SEARCH"
    assert req.language == "languageConstants/1031"  # ru
    assert req.geo_target_constants == ["geoTargetConstants/2804"]  # Украина


def test_seed_type_keyword_only_and_url_only():
    from ads.keyword_plan import generate_keyword_ideas

    c1 = _Client([_row("x", 1)])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        generate_keyword_ideas(c1, DRAFT_ACCOUNT_ID, seeds=["a"])
    assert list(c1.svc.last_req.keyword_seed.keywords) == ["a"]
    assert c1.svc.last_req.url_seed.url == ""

    c2 = _Client([_row("x", 1)])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        generate_keyword_ideas(c2, DRAFT_ACCOUNT_ID, url="https://x.io")
    assert c2.svc.last_req.url_seed.url == "https://x.io"
    assert list(c2.svc.last_req.keyword_seed.keywords) == []


# ── Кластеризация ─────────────────────────────────────────────────────────────────
def test_cluster_parse_keeps_only_real_keywords_and_leftover():
    from keywords.cluster import _parse

    valid = ["доставка цветов", "купить букет", "розы"]
    content = (
        '[{"name":"Транзакц","intent":"транзакционный",'
        '"keywords":["доставка цветов","выдуманное","купить букет"]}]'
    )
    clusters = _parse(content, valid)
    trans = next(c for c in clusters if c.name == "Транзакц")
    assert set(trans.keywords) == {"доставка цветов", "купить букет"}  # «выдуманное» отброшено
    leftover = next(c for c in clusters if c.name == "Прочее")
    assert leftover.keywords == ["розы"]  # не разложенный ключ не теряется


def test_cluster_parse_dedupes_across_groups():
    from keywords.cluster import _parse

    content = '[{"name":"A","keywords":["x","y"]},{"name":"B","keywords":["x","z"]}]'  # x повторён
    clusters = _parse(content, ["x", "y", "z"])
    placed = [k for c in clusters for k in c.keywords]
    assert placed.count("x") == 1  # ключ ровно в одной группе


async def test_cluster_keywords_fallback_on_bad_model(monkeypatch):
    from keywords import cluster as cl

    async def _bad(*a, **k):
        return SimpleNamespace(content="это не json")

    monkeypatch.setattr(cl, "chat", _bad)
    clusters = await cl.cluster_keywords(["a", "b", "a"], "ru")
    assert len(clusters) == 1 and clusters[0].name == "Все ключи"
    assert clusters[0].keywords == ["a", "b"]  # дедуп входа сохранён


async def test_cluster_keywords_groups_from_model(monkeypatch):
    from keywords import cluster as cl

    async def _ok(messages, **k):
        return SimpleNamespace(
            content='[{"name":"Группа","intent":"информационный","keywords":["a","b"]}]'
        )

    monkeypatch.setattr(cl, "chat", _ok)
    clusters = await cl.cluster_keywords(["a", "b"], "ru")
    assert clusters[0].name == "Группа" and clusters[0].intent == "информационный"
    assert set(clusters[0].keywords) == {"a", "b"}


# ── Экспорт .xlsx ─────────────────────────────────────────────────────────────────
def test_export_build_workbook_contains_rows():
    from ads.keyword_plan import KeywordIdea
    from keywords.cluster import Cluster
    from keywords.export import build_workbook

    ideas = [
        KeywordIdea("alpha", 100, "HIGH", 80, 1.0, 2.0, 0.5),
        KeywordIdea("beta", 50, "LOW", 10),
    ]
    clusters = [Cluster(name="Группа", intent="инфо", keywords=["alpha", "beta"])]
    wb = build_workbook(clusters, ideas, seeds=["сид"], language="ru")
    values = [str(c.value) for row in wb.active.iter_rows() for c in row]
    assert "alpha" in values and "beta" in values and "Группа" in values


# ── Регистрация read-tool ─────────────────────────────────────────────────────────
def test_keyword_research_tool_registered():
    from agent.tools.schemas import READ_TOOLS, SCHEMAS, TOOLS, KeywordResearch

    assert "keyword_research" in READ_TOOLS
    assert SCHEMAS["keyword_research"] is KeywordResearch
    assert any(t["function"]["name"] == "keyword_research" for t in TOOLS)


def test_keyword_research_schema_normalizes_and_validates_url():
    from agent.tools.schemas import KeywordResearch

    k = KeywordResearch(seeds=["  цветы ", "", "  "], url=None, language="ru")
    assert k.seeds == ["цветы"]  # обрезка + выброс пустых
    with pytest.raises(Exception):
        KeywordResearch(seeds=["x"], url="ftp://bad")  # url только http/https
