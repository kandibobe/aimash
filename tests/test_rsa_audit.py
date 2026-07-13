"""Ф3: тексты объявлений — «используются ли ключевые слова» (прямой запрос заказчика). Офлайн.

Пинуем то, ради чего фаза сделана:
1. Фетчер поднимает РАБОТАЮЩИЕ RSA в аудит-контур целиком: тексты, пины, ad_strength, возраст —
   и ключи их групп (клон-флоу читал только первый RSA по имени кампании, без пинов и метрик).
2. Покрытие ключей считает ОДИН И ТОТ ЖЕ код (`adcopy.validate.keyword_coverage`) в аудите и в
   генерации: аудит не может требовать одного, а генератор выдавать другое.
3. Анти-ложные (правило Ф0): объявление без показов не судим; пин ОДНОЙ позиции — норма, флажим
   только полный пин 1+2+3; AVERAGE у ad_strength — не дефект; нет даты (age=-1) → «стало» молчит.
4. Генерация: низкое покрытие — ГЕЙТ (догенерация заголовков с ключами), а не только сигнал;
   что вернула модель, проверяет КОД (длина + вхождение ключа), а не сама модель.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adcopy.generate as G  # noqa: E402
from adcopy.validate import MIN_KEYWORD_COVERAGE, keyword_coverage  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402
from reports import queries as Q  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402

from audit.engine import ONE_TAP_OPS, build_audit  # noqa: E402
from audit.render import finding_text  # noqa: E402
from audit.thresholds import DEFAULT_AUDIT_THRESHOLDS  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _report(cost: float = 300.0, conv: float = 10.0):
    totals = Metrics(
        impressions=5000, clicks=300, cost_micros=int(cost * 1_000_000), conversions=conv
    )
    rows = [(("Search", "ENABLED"), Metrics(impressions=5000, clicks=300, conversions=conv))]
    return SimpleNamespace(
        customer_id="123",
        totals=totals,
        breakdowns=[Breakdown("campaign", "Кампании", ["Кампания", "Статус"], rows)],
        currency="USD",
    )


def _ad(
    *,
    headlines: list[str] | None = None,
    descriptions: list[str] | None = None,
    keywords: list[str] | None = None,
    pinned: set[str] | None = None,
    strength: str = "GOOD",
    age_days: int = 10,
    impressions: int = 1000,
    cost: float = 100.0,
    ad_group: str = "Ноутбуки",
    ad_group_id: str = "77",
) -> Q.RsaAdRow:
    pins = frozenset(pinned or ())
    return Q.RsaAdRow(
        campaign="Search",
        ad_group=ad_group,
        ad_group_id=ad_group_id,
        ad_id="1",
        headlines=headlines if headlines is not None else [f"Заголовок {i}" for i in range(1, 11)],
        descriptions=descriptions if descriptions is not None else ["Описание", "Ещё", "Третье"],
        pinned_headline_positions=pins,
        pinned_headlines=len(pins),
        ad_strength=strength,
        age_days=age_days,
        metrics=Metrics(
            impressions=impressions, clicks=50, cost_micros=int(cost * 1_000_000), conversions=2.0
        ),
        keywords=keywords or [],
    )


def _ids(res) -> set[str]:
    return {f.check_id for f in res.findings}


# ── Фетчер: RSA целиком (тексты + пины + сила + возраст + ключи группы) ───────────────
def _gaql_ad(with_date: bool = True):
    def _h(text, pinned=""):
        return SimpleNamespace(
            text=text, pinned_field=SimpleNamespace(name=pinned or "UNSPECIFIED")
        )

    rsa = SimpleNamespace(
        headlines=[_h("Купить ноутбук", "HEADLINE_1"), _h("Доставка"), _h("")],
        descriptions=[_h("Описание раз"), _h("Описание два")],
    )
    aga = SimpleNamespace(
        ad=SimpleNamespace(id=55, responsive_search_ad=rsa),
        ad_strength=SimpleNamespace(name="POOR"),
        start_date_time="2026-01-05 10:00:00" if with_date else "",
    )
    return SimpleNamespace(
        campaign=SimpleNamespace(name="Search"),
        ad_group=SimpleNamespace(id=77, name="Ноутбуки"),
        ad_group_ad=aga,
        metrics=SimpleNamespace(
            impressions=1000,
            clicks=50,
            cost_micros=100_000_000,
            conversions=2.0,
            conversions_value=0.0,
        ),
    )


def _gaql_kw():
    return SimpleNamespace(
        ad_group=SimpleNamespace(id=77),
        ad_group_criterion=SimpleNamespace(keyword=SimpleNamespace(text="ноутбук")),
    )


class _Client:
    """Фейковый GoogleAdsService: два запроса (объявления + ключи групп); по флагу отвергает запрос
    с датой старта — фетчер обязан повторить БЕЗ неё, а не потерять тексты и пины заодно."""

    def __init__(self, *, fail_date: bool = False, with_date: bool = True):
        self._fail, self._with_date = fail_date, with_date
        self.seen: list[str] = []

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self

    def search(self, *, customer_id, query):
        self.seen.append(query)
        if "start_date_time" in query:
            if self._fail:
                raise RuntimeError("field not selectable")
            return [_gaql_ad(with_date=True)]
        if "FROM ad_group_ad" in query:
            return [_gaql_ad(with_date=self._with_date)]
        return [_gaql_kw()]


def test_fetch_rsa_assets_reads_copy_pins_strength_and_group_keywords():
    client = _Client()
    with allowed_ids(DRAFT_ACCOUNT_ID):
        rows = Q.fetch_rsa_assets(
            client, DRAFT_ACCOUNT_ID, SimpleNamespace(gaql_between=lambda: "X")
        )
    assert len(rows) == 1
    a = rows[0]
    assert a.headlines == ["Купить ноутбук", "Доставка"]  # пустой текст не попал
    assert a.descriptions == ["Описание раз", "Описание два"]
    assert a.pinned_headline_positions == frozenset({"HEADLINE_1"}) and a.pinned_headlines == 1
    assert a.ad_strength == "POOR" and a.metrics.impressions == 1000
    assert a.keywords == ["ноутбук"]  # ключи ГРУППЫ подтянуты вторым запросом
    assert a.age_days >= 0


def test_fetch_rsa_assets_degrades_without_start_date():
    """Сервер отверг дату → тексты/пины/силу читаем ВСЁ РАВНО; age_days=-1 = «не знаем» (gr8)."""
    client = _Client(fail_date=True, with_date=False)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        rows = Q.fetch_rsa_assets(
            client, DRAFT_ACCOUNT_ID, SimpleNamespace(gaql_between=lambda: "X")
        )
    assert "start_date_time" not in client.seen[1]
    assert rows[0].headlines and rows[0].age_days == -1


# ── Чек покрытия ключей: ПРЯМОЙ ответ на «используются ли ключи в объявлениях» ────────
def test_keyword_coverage_check_flags_ads_that_ignore_their_own_keywords():
    ad = _ad(
        headlines=["Скидки до 50%", "Быстрая доставка", "Гарантия 2 года", "Купить ноутбук"],
        keywords=["ноутбук", "макбук"],
    )
    res = build_audit(_report(), rsa_ads=[ad])
    f = next(f for f in res.findings if f.check_id == "rsa_keyword_coverage_low")
    assert f.facts["worst_pct"] == 25 and f.facts["min_pct"] == int(MIN_KEYWORD_COVERAGE * 100)
    assert f.facts["missing"] == ["макбук"]  # ключ, которого нет НИ В ОДНОМ заголовке
    assert f.evidence["cost"] == 100.0  # деньги за спиной сигнала (ранг в advisor)
    assert f.advice_operation == "create_rsa" and f.suggested_operation is None
    assert f.suggested_operation not in ONE_TAP_OPS  # тексты пишет генератор → курация, не кнопка


def test_keyword_coverage_check_and_generation_share_one_threshold_and_one_code():
    """🔒 Аудит и генератор считают релевантность ОДНОЙ функцией и по ОДНОМУ порогу. Разъедься они —
    бот ругал бы за то, что сам же и сгенерировал."""
    assert DEFAULT_AUDIT_THRESHOLDS["rsa_kw_coverage_min"] == MIN_KEYWORD_COVERAGE
    ok = _ad(headlines=["Купить ноутбук", "Ноутбуки Asus"], keywords=["ноутбук"])
    assert keyword_coverage(ok.headlines, ok.keywords) >= MIN_KEYWORD_COVERAGE
    assert "rsa_keyword_coverage_low" not in _ids(build_audit(_report(), rsa_ads=[ok]))


def test_rsa_checks_ignore_ads_without_impressions():
    """Анти-ложные (Ф0): объявление без показов — не улика. Иначе аудит шумел бы на черновиках."""
    silent = _ad(headlines=["Скидки"], descriptions=["Одно"], keywords=["ноутбук"], impressions=0)
    assert not [f for f in build_audit(_report(), rsa_ads=[silent]).findings if f.family == "rsa"]


# ── Гигиена текстов: тонко / POOR / пины / выгорание ──────────────────────────────────
def test_thin_copy_poor_strength_and_full_pinning_are_flagged():
    ad = _ad(
        headlines=["Купить ноутбук", "Ноутбук Asus", "Ноутбуки в наличии"],  # 3 < 8 (G27)
        descriptions=["Ноутбуки со скидкой"],  # 1 < 3 (G28)
        keywords=["ноутбук"],
        pinned={"HEADLINE_1", "HEADLINE_2", "HEADLINE_3"},  # ротация мертва (G30)
        strength="POOR",  # оценка самого Google (G29)
    )
    got = _ids(build_audit(_report(), rsa_ads=[ad]))
    assert {
        "rsa_headlines_thin",
        "rsa_descriptions_thin",
        "rsa_ad_strength_poor",
        "rsa_overpinned",
    } <= got
    assert "rsa_keyword_coverage_low" not in got  # ключ есть во всех заголовках — не флагаем


def test_single_pin_and_average_strength_are_not_defects():
    """Пин ОДНОЙ позиции (бренд/юр. требование) и AVERAGE — нормальная жизнь, а не дефект."""
    ad = _ad(pinned={"HEADLINE_1"}, strength="AVERAGE")
    got = _ids(build_audit(_report(), rsa_ads=[ad]))
    assert "rsa_overpinned" not in got and "rsa_ad_strength_poor" not in got


def test_stale_group_flagged_by_freshest_ad_and_silent_without_dates():
    old = _ad(age_days=200, ad_group_id="77", cost=80.0)
    fresh = _ad(age_days=5, ad_group_id="77", cost=20.0)  # в группе есть свежее → не «выгорела»
    assert "rsa_stale" not in _ids(build_audit(_report(), rsa_ads=[old, fresh]))
    assert "rsa_stale" in _ids(build_audit(_report(), rsa_ads=[old]))
    unknown = _ad(age_days=-1)  # дата не прочитана → молчим, «нет данных» ≠ «выгорело» (gr8)
    assert "rsa_stale" not in _ids(build_audit(_report(), rsa_ads=[unknown]))


def test_rsa_findings_render_ru_and_en():
    ad = _ad(
        headlines=["Скидки до 50%", "Быстрая доставка"],
        descriptions=["Одно"],
        keywords=["ноутбук"],
        pinned={"HEADLINE_1", "HEADLINE_2", "HEADLINE_3"},
        strength="POOR",
        age_days=200,
    )
    res = build_audit(_report(), rsa_ads=[ad])
    by_id = {f.check_id: f for f in res.findings}
    cov = finding_text(by_id["rsa_keyword_coverage_low"], "ru", "USD")
    assert "«ноутбук»" in cov and "Quality Score" in cov
    assert "keywords" in finding_text(by_id["rsa_keyword_coverage_low"], "en", "USD").lower()
    for cid in (
        "rsa_headlines_thin",
        "rsa_descriptions_thin",
        "rsa_ad_strength_poor",
        "rsa_overpinned",
        "rsa_stale",
    ):
        assert finding_text(by_id[cid], "ru", "USD") and finding_text(by_id[cid], "en", "USD")


# ── Генерация: покрытие стало ГЕЙТОМ ──────────────────────────────────────────────────
def _msg(content: str):
    return SimpleNamespace(content=content)


def _chat_script(*contents: str):
    calls: list[list[dict]] = []

    async def _chat(messages, **kwargs):
        i = min(len(calls), len(contents) - 1)
        calls.append(messages)
        return _msg(contents[i])

    return _chat, calls


async def test_generation_gate_replaces_headlines_that_miss_the_keyword():
    """Модель выдала заголовки без ключа → КОД догенерирует с ключом и заменит непокрытые."""
    first = json.dumps(
        {"headlines": ["Скидки до 50%", "Быстрая доставка", "Гарантия"], "descriptions": ["Опис"]},
        ensure_ascii=False,
    )
    repair = json.dumps({"items": ["Купить ноутбук", "Ноутбуки Asus"]}, ensure_ascii=False)
    fake, calls = _chat_script(first, repair)
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(
            G.CopyBrief(topic="ноутбуки", keywords=["ноутбук"], n_headlines=3, n_descriptions=1)
        )
    assert draft.coverage_repaired
    assert draft.keyword_coverage >= MIN_KEYWORD_COVERAGE
    assert "Купить ноутбук" in draft.headlines and "Скидки до 50%" not in draft.headlines
    assert len(draft.headlines) == 3  # заменили, а не раздули набор


async def test_generation_gate_never_trusts_the_model_on_length_or_relevance():
    """🔒 Ремонт релевантности — не лазейка: слишком длинный заголовок и заголовок БЕЗ ключа
    отбрасывает КОД (golden rule #4 распространён с длины на вхождение ключа)."""
    first = json.dumps(
        {"headlines": ["Скидки", "Доставка"], "descriptions": ["Опис"]}, ensure_ascii=False
    )
    repair = json.dumps(
        {
            "items": [
                "Ноутбуки по очень выгодной цене со скидкой сегодня",  # >30 симв. — длину считает КОД
                "Просто акция",  # без ключа — не решает задачу, отбрасываем
                "Ноутбук Asus",  # годный
            ]
        },
        ensure_ascii=False,
    )
    fake, _ = _chat_script(first, repair)
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(
            G.CopyBrief(topic="ноутбуки", keywords=["ноутбук"], n_headlines=2, n_descriptions=1)
        )
    assert "Ноутбук Asus" in draft.headlines
    assert not any(len(h) > 30 for h in draft.headlines)
    assert "Просто акция" not in draft.headlines


async def test_generation_gate_silent_without_keywords():
    """Ключей нет → мерить нечего: ремонт не запускаем, лишнего вызова модели не делаем."""
    content = json.dumps(
        {"headlines": ["Скидки", "Доставка"], "descriptions": ["Опис"]}, ensure_ascii=False
    )
    fake, calls = _chat_script(content)
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(G.CopyBrief(topic="ноутбуки", n_headlines=2, n_descriptions=1))
    assert len(calls) == 1 and not draft.coverage_repaired and draft.keyword_coverage == 1.0
