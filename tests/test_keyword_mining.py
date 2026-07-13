"""Ф4: майнинг поисковых запросов — сбор урожая, n-граммы, каннибализация, нулевые ключи. Офлайн.

Что здесь пинуется (и почему именно это):
1. Обратный ход к «в минус»: конвертящий запрос БЕЗ своего ключа — предложить «в плюс». Раньше бот
   умел только минусовать; заработанные вслепую запросы так и оставались широкими.
2. Минус-слово — необратимая для трафика вещь, поэтому три гарда: не резать СВОЙ ключ, ноль конверсий
   (а не «мало»), система (≥2 разных запроса). Каждый гард — свой тест.
3. Деньги n-грамм НЕ идут в at_risk: их уже посчитали чеки запросов/ключей. Задвоение «Под риском» —
   ровно та ошибка, которую чинила эпоха 4; тест закрывает класс.
4. Нулевые ключи требуют ОТДЕЛЬНОГО фетчера: выборка «топ по расходу» теряет их первыми, ведь расхода
   у них по определению нет.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402
from reports import queries as Q  # noqa: E402
from reports.queries import Breakdown, Metrics, SearchTermRow  # noqa: E402

from audit.engine import ONE_TAP_OPS, build_audit  # noqa: E402
from audit.render import finding_text  # noqa: E402
from audit.terms import harvest, waste_ngrams  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


def _report(cost: float = 500.0, conv: float = 20.0):
    totals = Metrics(
        impressions=9000, clicks=500, cost_micros=int(cost * 1_000_000), conversions=conv
    )
    rows = [(("Search", "ENABLED"), Metrics(impressions=9000, clicks=500, conversions=conv))]
    return SimpleNamespace(
        customer_id="123",
        totals=totals,
        breakdowns=[Breakdown("campaign", "Кампании", ["Кампания", "Статус"], rows)],
        currency="USD",
    )


def _term(text: str, *, cost: float, conv: float = 0.0, clicks: int = 5, ad_group: str = "Общая"):
    return SearchTermRow(
        search_term=text,
        campaign="Search",
        ad_group=ad_group,
        keyword="ноутбук",
        match_type="BROAD",
        metrics=Metrics(
            impressions=100, clicks=clicks, cost_micros=int(cost * 1_000_000), conversions=conv
        ),
    )


def _kw(
    text: str,
    *,
    impressions: int = 500,
    cost: float = 10.0,
    ad_group: str = "Общая",
    match: str = "PHRASE",
    campaign: str = "Search",
):
    return Q.KeywordInventoryRow(
        campaign=campaign,
        ad_group=ad_group,
        keyword=text,
        match_type=match,
        metrics=Metrics(
            impressions=impressions, clicks=10, cost_micros=int(cost * 1_000_000), conversions=1.0
        ),
    )


def _ids(res) -> set[str]:
    return {f.check_id for f in res.findings}


def _find(res, cid: str):
    return next(f for f in res.findings if f.check_id == cid)


# ── Сбор урожая: обратный ход к «в минус» ────────────────────────────────────────────
def test_harvest_offers_converting_terms_that_have_no_keyword():
    terms = [
        _term("ноутбук asus rog", cost=40.0, conv=3.0),  # конвертит, ключа нет → «в плюс»
        _term("ноутбук", cost=90.0, conv=5.0),  # уже есть ключом → НЕ предлагать
        _term("ноутбук бесплатно", cost=30.0, conv=0.0),  # без конверсий → не урожай
    ]
    items = harvest(terms, ["ноутбук", "купить ноутбук"])
    assert [i.term for i in items] == ["ноутбук asus rog"]
    assert items[0].conversions == 3.0 and items[0].ad_group == "Общая"


def test_harvest_ignores_terms_already_covered_by_a_keyword_in_another_group():
    """Ключ есть в СОСЕДНЕЙ группе → добавлять нельзя: получим ровно ту каннибализацию, что флажим."""
    terms = [_term("ноутбук asus", cost=50.0, conv=4.0, ad_group="Общая")]
    assert harvest(terms, ["ноутбук asus"], min_conv=1.0) == []


def test_harvest_check_is_advice_not_a_button_and_does_not_touch_the_score():
    inv = [_kw("ноутбук")]
    terms = [_term("ноутбук asus rog", cost=40.0, conv=3.0)]
    res = build_audit(_report(), search_terms=terms, keyword_inventory=inv)
    f = _find(res, "keyword_harvest")
    assert f.advice_operation == "add_keywords"
    assert f.suggested_operation is None and f.suggested_operation not in ONE_TAP_OPS
    assert f.score_intensity == 0.0  # возможность, не дефект: балл не штрафуем
    assert f.at_risk == 0.0
    txt = finding_text(f, "ru", "USD")
    assert "«ноутбук asus rog»" in txt and "точным соответствием" in txt
    assert "exact" in finding_text(f, "en", "USD").lower()


# ── N-граммы: три гарда против ложного минус-слова ───────────────────────────────────
def test_ngram_waste_flags_systemic_money_burners():
    terms = [
        _term("ноутбук бесплатно", cost=8.0),
        _term("скачать ноутбук бесплатно", cost=7.0),
        _term("ноутбук бесплатно скачать драйвер", cost=6.0),
    ]
    grams = waste_ngrams(terms, ["ноутбук"], min_cost=10.0, min_terms=2)
    texts = [g.text for g in grams]
    assert "бесплатно" in texts  # 21 у.е. в трёх запросах, 0 конверсий
    assert grams[0].terms == 3


def test_ngram_never_proposes_a_word_from_our_own_keywords():
    """🔒 Гард №1. «ноутбук» стоит денег и «не конвертит» в этих запросах — но это НАШ ключ:
    минус на него вырубил бы весь трафик аккаунта. Такой кандидат обязан быть отброшен."""
    terms = [_term("ноутбук даром", cost=30.0), _term("купить ноутбук даром", cost=30.0)]
    texts = [g.text for g in waste_ngrams(terms, ["купить ноутбук"], min_cost=10.0, min_terms=2)]
    assert "ноутбук" not in texts and "купить ноутбук" not in texts
    assert texts == ["даром"]  # а само мусорное слово — предлагается


def test_ngram_spares_words_that_convert_anywhere():
    """🔒 Гард №2: одна конверсия в ЛЮБОМ запросе со словом → не кандидат. Порог — ноль, не «мало»."""
    terms = [
        _term("ноутбук дёшево", cost=50.0, conv=0.0),
        _term("дёшево ноутбук купить", cost=50.0, conv=1.0),  # ← слово всё-таки приносит деньги
    ]
    assert [g.text for g in waste_ngrams(terms, ["ноутбук"], min_cost=10.0, min_terms=2)] == []


def test_ngram_needs_a_pattern_not_a_single_query():
    """🔒 Гард №3: единичный дорогой запрос — это wasteful_search_term, а не «система»."""
    terms = [_term("ноутбук своими руками", cost=99.0)]
    assert waste_ngrams(terms, ["ноутбук"], min_cost=10.0, min_terms=2) == []


def test_ngram_prefers_the_shorter_covering_word():
    """Из вложенных берём короткую: «вакансии» перекрывает «вакансии москва» — второе избыточно."""
    terms = [
        _term("вакансии москва ноутбук", cost=20.0),
        _term("вакансии москва сборка", cost=20.0),
    ]
    texts = [g.text for g in waste_ngrams(terms, ["ноутбук"], min_cost=10.0, min_terms=2, top_n=5)]
    assert "вакансии" in texts and "вакансии москва" not in texts


def test_ngram_money_never_double_counts_at_risk():
    """🔒 Расход n-грамм уже учтён wasteful_search_term/keyword. Положив его в at_risk, мы бы
    задвоили «Под риском» — та же ошибка, что чинила эпоха 4. at_risk=0, деньги — в фактах."""
    terms = [_term("ноутбук бесплатно", cost=60.0), _term("ноутбук бесплатно скачать", cost=60.0)]
    res = build_audit(_report(), search_terms=terms, keyword_inventory=[_kw("ноутбук")])
    f = _find(res, "ngram_waste")
    assert f.at_risk == 0.0 and f.facts["cost"] == 120.0
    assert f.suggested_operation is None  # n-грамма пересекает кампании → не one-tap
    assert f.advice_operation == "add_negative_keywords"
    assert res.at_risk <= float(res.report.totals.cost) if hasattr(res, "report") else True
    assert "«бесплатно»" in finding_text(f, "ru", "USD")
    assert finding_text(f, "en", "USD")


def test_mining_checks_stay_silent_without_the_keyword_list():
    """Ключи не прочитаны (None) → мы не знаем, что уже покрыто и что своё: молчим, а не гадаем."""
    terms = [_term("ноутбук бесплатно", cost=60.0), _term("ноутбук бесплатно скачать", cost=60.0)]
    got = _ids(build_audit(_report(), search_terms=terms, keyword_inventory=None))
    assert "ngram_waste" not in got and "keyword_harvest" not in got


# ── Каннибализация и нулевые ключи ───────────────────────────────────────────────────
def test_cannibalization_flags_same_keyword_same_match_in_two_groups():
    inv = [
        _kw("ноутбук asus", ad_group="Ноутбуки", cost=40.0),
        _kw("ноутбук asus", ad_group="Asus", cost=25.0),  # тот же текст + тот же тип → конкурируют
        _kw("ноутбук acer", ad_group="Acer"),
    ]
    f = _find(build_audit(_report(), keyword_inventory=inv), "keyword_cannibalization")
    assert f.facts["keyword"] == "ноутбук asus" and f.facts["cost"] == 65.0
    assert len(f.facts["places"]) == 2
    assert finding_text(f, "ru", "USD") and finding_text(f, "en", "USD")


def test_cannibalization_ignores_different_match_types_and_sleeping_duplicates():
    """BROAD + EXACT одного текста — нормальная структура. Дубль без показов никому не мешает."""
    normal = [
        _kw("ноутбук asus", ad_group="Ноутбуки", match="BROAD"),
        _kw("ноутбук asus", ad_group="Asus", match="EXACT"),
    ]
    sleeping = [
        _kw("ноутбук asus", ad_group="Ноутбуки"),
        _kw("ноутбук asus", ad_group="Asus", impressions=0),
    ]
    assert "keyword_cannibalization" not in _ids(build_audit(_report(), keyword_inventory=normal))
    assert "keyword_cannibalization" not in _ids(build_audit(_report(), keyword_inventory=sleeping))


def test_zero_impression_keywords_need_both_share_and_count():
    many_zero = [_kw(f"ключ {i}", impressions=0) for i in range(24)] + [_kw("живой ключ")]
    f = _find(build_audit(_report(), keyword_inventory=many_zero), "zero_impression_keywords")
    assert f.facts["count"] == 24 and f.facts["pct"] == 96
    assert finding_text(f, "ru", "USD") and finding_text(f, "en", "USD")
    # Маленький аккаунт: доля та же, но 5 нулевых из 6 — норма молодой кампании, не находка.
    small = [_kw(f"ключ {i}", impressions=0) for i in range(5)] + [_kw("живой ключ")]
    assert "zero_impression_keywords" not in _ids(build_audit(_report(), keyword_inventory=small))


# ── Фетчер: нули не отсекаются выборкой ──────────────────────────────────────────────
class _Client:
    def __init__(self):
        self.query = ""

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self

    def search(self, *, customer_id, query):
        self.query = query
        return [
            SimpleNamespace(
                campaign=SimpleNamespace(name="Search"),
                ad_group=SimpleNamespace(name="Общая"),
                ad_group_criterion=SimpleNamespace(
                    keyword=SimpleNamespace(
                        text="ноутбук", match_type=SimpleNamespace(name="PHRASE")
                    )
                ),
                metrics=SimpleNamespace(
                    impressions=0, clicks=0, cost_micros=0, conversions=0.0, conversions_value=0.0
                ),
            )
        ]


def test_fetch_keyword_inventory_does_not_order_by_cost():
    """🔒 Смысл отдельного фетчера: `ORDER BY cost` выбросил бы нулевые ключи первыми — а они и есть
    предмет чека. Проверяем сам запрос, а не только распарсенный результат."""
    client = _Client()
    with allowed_ids(DRAFT_ACCOUNT_ID):
        rows = Q.fetch_keyword_inventory(
            client, DRAFT_ACCOUNT_ID, SimpleNamespace(gaql_between=lambda: "X")
        )
    assert "ORDER BY" not in client.query
    assert "ad_group_criterion.status = 'ENABLED'" in client.query
    assert rows[0].keyword == "ноутбук" and rows[0].metrics.impressions == 0
