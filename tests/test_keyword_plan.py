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


def test_returns_top_by_volume_not_first_n():
    from ads.keyword_plan import generate_keyword_ideas

    # API отдаёт по релевантности (ёмкие — в конце); limit=2 → вернуться должны 2 самых ёмких,
    # а не первые два. Это покрывает фикс «сортировать пул, потом обрезать», а не наоборот.
    rows = [_row("a", 10), _row("b", 20), _row("c", 9000), _row("d", 8000)]
    client = _Client(rows)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        ideas = generate_keyword_ideas(client, DRAFT_ACCOUNT_ID, seeds=["s"], limit=2)
    assert [i.text for i in ideas] == ["c", "d"]  # топ-2 по объёму, не первые два из API


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
    # A1: нейтральные дефолты — без языка/гео (language="", geo_ids=()) → глобальный подбор без
    # биаса (раньше молча подставлялись ru + Украина). Язык/гео задаёт вызывающий явно.
    assert req.language == ""  # без languageConstant
    assert req.geo_target_constants == []  # без geoTargetConstants


# ── K: RU/BY не обслуживаются Keyword Planner — отфильтровываются перед SDK-запросом ──
def test_ru_geo_dropped_request_runs_without_geo():
    """RU (2643) не обслуживается → запрос строится БЕЗ гео (не «invalid value»), не падает."""
    from ads.keyword_plan import generate_keyword_ideas

    client = _Client([_row("байкал", 100)])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        ideas = generate_keyword_ideas(client, DRAFT_ACCOUNT_ID, seeds=["туры"], geo_ids=(2643,))
    assert [i.text for i in ideas] == ["байкал"]  # подбор отработал
    assert client.svc.last_req.geo_target_constants == []  # RU выкинут → без гео (глобально)


def test_ru_dropped_but_ua_kept():
    from ads.keyword_plan import generate_keyword_ideas

    client = _Client([_row("x", 1)])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        generate_keyword_ideas(client, DRAFT_ACCOUNT_ID, seeds=["s"], geo_ids=(2643, 2804, 2112))
    # осталась только Украина (2804); RU (2643) и BY (2112) выкинуты
    assert client.svc.last_req.geo_target_constants == ["geoTargetConstants/2804"]


def test_geo_helpers():
    from ads.geo import drop_non_serviceable_geo, has_non_serviceable_geo

    kept, dropped = drop_non_serviceable_geo((2643, 2804, 2112, 2840))
    assert kept == (2804, 2840) and set(dropped) == {2643, 2112}
    assert has_non_serviceable_geo((2643,)) is True
    assert has_non_serviceable_geo((2804,)) is False
    assert has_non_serviceable_geo(()) is False


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


# ── Ретрай транзиентных ошибок (классификация унифицирована с core.resilience) ─────
class _FlakySvc:
    """SVC, который бросает заданные исключения на первых вызовах, затем отдаёт rows."""

    def __init__(self, rows, errors):
        self.rows = rows
        self.errors = list(errors)
        self.calls = 0
        self.last_req = None

    def generate_keyword_ideas(self, request=None):
        self.last_req = request
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return list(self.rows)


class _FlakyClient(_Client):
    def __init__(self, rows, errors):
        super().__init__(rows)
        self.svc = _FlakySvc(rows, errors)


def test_no_internal_retry_single_attempt():
    """2.9: внутренний retry-loop УДАЛЁН — любая ошибка (даже транзиентная) уходит наружу с
    ПЕРВОЙ попытки. Backoff/повторы держит единый core.resilience.run_ads_read_call на вызывающей
    стороне (раньше был двойной ретрай: до _RETRIES × ADS_MAX_ATTEMPTS попыток, а time.sleep
    внутреннего цикла не отменялся внешним asyncio.timeout)."""
    import ads.keyword_plan as kp

    client = _FlakyClient([_row("x", 5)], errors=[Exception("RATE_EXCEEDED: slow down")])
    with allowed_ids(DRAFT_ACCOUNT_ID), pytest.raises(Exception, match="RATE_EXCEEDED"):
        kp.generate_keyword_ideas(client, DRAFT_ACCOUNT_ID, seeds=["s"])
    assert client.svc.calls == 1  # ровно одна попытка — без внутреннего backoff

    client2 = _FlakyClient([_row("x", 5)], errors=[ValueError("INVALID_ARGUMENT: bad seed")])
    with allowed_ids(DRAFT_ACCOUNT_ID), pytest.raises(ValueError):
        kp.generate_keyword_ideas(client2, DRAFT_ACCOUNT_ID, seeds=["s"])
    assert client2.svc.calls == 1

    assert not hasattr(kp, "_is_retryable")  # гард: локальный классификатор не возродился


# ── Сезонность (§7): пик месяца из monthly_search_volumes ───────────────────────
def _metrics_with_months(months):
    return SimpleNamespace(
        avg_monthly_searches=100,
        competition=SimpleNamespace(name="HIGH"),
        competition_index=50,
        low_top_of_page_bid_micros=0,
        high_top_of_page_bid_micros=0,
        average_cpc_micros=0,
        monthly_search_volumes=months,
    )


def test_seasonality_peak_month_parsed():
    from ads.keyword_plan import _idea

    months = [
        SimpleNamespace(year=2025, month=SimpleNamespace(name="JANUARY"), monthly_searches=100),
        SimpleNamespace(year=2025, month=SimpleNamespace(name="DECEMBER"), monthly_searches=900),
        SimpleNamespace(year=2025, month=SimpleNamespace(name="JULY"), monthly_searches=300),
    ]
    idea = _idea(SimpleNamespace(text="ёлка", keyword_idea_metrics=_metrics_with_months(months)))
    assert idea.peak_month == "дек 2025"  # макс. объём — декабрь


def test_seasonality_empty_when_no_volumes():
    from ads.keyword_plan import _idea

    idea = _idea(SimpleNamespace(text="x", keyword_idea_metrics=_metrics_with_months([])))
    assert idea.peak_month == ""  # ряда нет (типично на тест-аккаунте)
    assert idea.monthly == []  # §7: полная кривая тоже пуста


def test_seasonality_full_curve_captured_and_ordered():
    """§7: полная 12-мес кривая сохраняется на KeywordIdea (раньше отбрасывалась) — в календарном
    порядке, month_num по ИМЕНИ enum (не по числовому значению proto)."""
    from ads.keyword_plan import _idea

    months = [
        SimpleNamespace(year=2025, month=SimpleNamespace(name="DECEMBER"), monthly_searches=900),
        SimpleNamespace(year=2025, month=SimpleNamespace(name="JANUARY"), monthly_searches=100),
        SimpleNamespace(year=2025, month=SimpleNamespace(name="JULY"), monthly_searches=300),
    ]
    idea = _idea(SimpleNamespace(text="ёлка", keyword_idea_metrics=_metrics_with_months(months)))
    # отсортировано по (year, month_num): январь(1) → июль(7) → декабрь(12)
    assert idea.monthly == [(2025, 1, 100), (2025, 7, 300), (2025, 12, 900)]
    assert idea.peak_month == "дек 2025"  # производное согласовано с кривой


def test_seasonality_sparkline_shape():
    from ads.keyword_plan import seasonality_sparkline

    assert seasonality_sparkline([]) == ""  # пусто
    assert seasonality_sparkline([(2025, 1, 0), (2025, 2, 0)]) == ""  # нулевой ряд
    spark = seasonality_sparkline([(2025, 1, 10), (2025, 2, 100), (2025, 3, 55)])
    assert len(spark) == 3
    assert spark[1] == "█"  # пик — самый высокий блок
    assert spark[0] < spark[1]  # рост к пику (по unicode-порядку блоков)


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


# ── §7: таксономия интента / приоритезация / минус-слова ──────────────────────────
def test_normalize_intent_maps_to_tz_taxonomy():
    from keywords.cluster import INTENTS, normalize_intent

    assert (
        normalize_intent("Брендовый") == "брендовый"
    )  # P2 (живой тест 2026-07-06): бренды/конкуренты — СВОЯ категория
    assert normalize_intent("commercial") == "коммерческий"
    assert normalize_intent("ТРАНЗАКЦИОННЫЙ") == "транзакционный"
    assert normalize_intent("информационный") == "информационный"
    assert normalize_intent("чёрт-знает-что") == ""  # неизвестное → пусто (не мусорный лейбл)
    assert set(INTENTS) == {
        "транзакционный",
        "коммерческий",
        "информационный",
        "навигационный",
        "брендовый",
    }


async def test_parse_normalizes_model_intent(monkeypatch):
    from keywords import cluster as cl

    async def _ok(messages, **k):
        return SimpleNamespace(content='[{"name":"Бренд","intent":"брендовый","keywords":["a"]}]')

    monkeypatch.setattr(cl, "chat", _ok)
    clusters = await cl.cluster_keywords(["a"], "ru")
    assert clusters[0].intent == "брендовый"  # P2: «брендовый» — своя категория таксономии


def test_rank_clusters_orders_by_volume_and_intent():
    from keywords.cluster import Cluster, rank_clusters

    trans = Cluster(name="Купить", intent="транзакционный", keywords=["a", "b"])
    info = Cluster(name="Как", intent="информационный", keywords=["c", "d"])
    leftover = Cluster(name="Прочее", intent="", keywords=["e"])
    by_text = {"a": 100, "b": 100, "c": 100, "d": 100, "e": 10_000}
    ranked = rank_clusters([info, leftover, trans], by_text)
    # транзакционный (200×1.0=200) выше информационного (200×0.3=60); «Прочее» всегда в конце
    assert [c.name for c in ranked] == ["Купить", "Как", "Прочее"]
    assert ranked[0].priority == 200.0 and ranked[1].priority == 60.0


async def test_suggest_negative_keywords_parses_and_fallbacks(monkeypatch):
    from keywords import cluster as cl

    async def _ok(messages, **k):
        return SimpleNamespace(content='["бесплатно","скачать","Бесплатно"]')  # дубль по регистру

    monkeypatch.setattr(cl, "chat", _ok)
    neg = await cl.suggest_negative_keywords("доставка цветов", ["купить цветы"], language="ru")
    assert neg == ["бесплатно", "скачать"]  # нижний регистр + дедуп

    async def _bad(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(cl, "chat", _bad)
    assert await cl.suggest_negative_keywords("x", ["y"]) == []  # advisory → пустой fallback


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


def test_export_workbook_has_negatives_sheet():
    from ads.keyword_plan import KeywordIdea
    from keywords.cluster import Cluster
    from keywords.export import build_workbook

    wb = build_workbook(
        [Cluster(name="Группа", keywords=["alpha"])],
        [KeywordIdea("alpha", 100, "HIGH", 80)],
        negatives=["бесплатно", "скачать"],
    )
    assert "Минус-слова" in wb.sheetnames  # §7: отдельный лист предложенных минус-слов
    vals = [str(c.value) for row in wb["Минус-слова"].iter_rows() for c in row]
    assert "бесплатно" in vals and "скачать" in vals


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
