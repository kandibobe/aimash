"""Ф7: Performance Max — восемь чеков. Пинуем ровно то, из-за чего PMax-аудиты обычно врут.

1. ДЕНЬГИ НЕ СУММИРУЮТСЯ. Расход запроса ⊂ расход группы активов ⊂ расход кампании — одни и те же
   деньги с трёх сторон. У обоих денежных чеков spend_segment = ИМЯ КАМПАНИИ ⇒ _dedup_at_risk берёт
   МАКСИМУМ. Дать им разные сегменты = вернуть дефект двойного счёта, ради которого бампали эпоху 4.
2. Группа-нахлебник флажится ТОЛЬКО внутри конвертящей кампании: если не конвертит вся кампания —
   это spend_no_conv, а не проблема группы (дублировать чужую находку нельзя).
3. AVERAGE у ad_strength НЕ находка (это половина живых групп; флажим только POOR/NO_ADS).
4. «Нет видео» — про СВОИ видео: авто-видео Google (source=AUTOMATICALLY_CREATED) не считается, иначе
   чек промолчит ровно там, где Google собрал видео из картинок вместо рекламодателя.
5. «Нет сигналов» = ни тем, ни аудиторий, ни товарного фида: у retail-PMax сигнал — сам фид.
6. «Мало конверсий» — только про ФРАГМЕНТАЦИЮ (≥2 кампании, каждая ниже порога). Одна кампания ниже
   порога — это «мало спроса», лекарства нет, молчим.
7. GR8: pmax_* не прочитан (None) → вся семья молчит; PMax в аккаунте нет ([]) → тоже молчит.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reports.queries import (  # noqa: E402
    Breakdown,
    Metrics,
    PmaxAssetGroupRow,
    PmaxCampaignRow,
    PmaxSearchTermRow,
)

from audit.engine import ONE_TAP_OPS, build_audit  # noqa: E402
from audit.render import finding_text  # noqa: E402


def _report(campaigns: list[tuple[str, float, float]]):
    """campaigns = [(имя, расход, конверсии)]."""
    rows = [
        (
            (name, "ENABLED"),
            Metrics(
                impressions=1000,
                clicks=100,
                cost_micros=int(cost * 1_000_000),
                conversions=conv,
            ),
        )
        for name, cost, conv in campaigns
    ]
    totals = Metrics(
        impressions=1000 * len(campaigns),
        clicks=100 * len(campaigns),
        cost_micros=int(sum(c[1] for c in campaigns) * 1_000_000),
        conversions=sum(c[2] for c in campaigns),
    )
    return SimpleNamespace(
        customer_id="123",
        totals=totals,
        breakdowns=[Breakdown("campaign", "Кампании", ["Кампания", "Статус"], rows)],
        currency="USD",
    )


def _camp(
    name: str = "PMax",
    *,
    cid: str = "1",
    cost: float = 500.0,
    conv: float = 10.0,
    clicks: int = 200,
    days: int = 30,
    negatives: int = 5,
    brand_exclusions: int = 0,
    merchant_id: str = "",
    terms: list[PmaxSearchTermRow] | None = None,
) -> PmaxCampaignRow:
    return PmaxCampaignRow(
        campaign_id=cid,
        campaign=name,
        cost=cost,
        clicks=clicks,
        conversions=conv,
        days=days,
        negatives=negatives,
        brand_exclusions=brand_exclusions,
        merchant_id=merchant_id,
        search_terms=list(terms or []),
    )


def _group(
    name: str = "AG1",
    *,
    campaign: str = "PMax",
    cid: str = "1",
    gid: str = "10",
    cost: float = 100.0,
    conv: float = 0.0,
    clicks: int = 50,
    ad_strength: str = "GOOD",
    videos: int = 1,
    videos_auto: int = 0,
    search_themes: int = 3,
    audiences: int = 1,
    action_items: list[tuple[str, int]] | None = None,
) -> PmaxAssetGroupRow:
    return PmaxAssetGroupRow(
        campaign_id=cid,
        campaign=campaign,
        asset_group_id=gid,
        asset_group=name,
        ad_strength=ad_strength,
        cost=cost,
        clicks=clicks,
        conversions=conv,
        headlines=5,
        descriptions=3,
        images=10,
        logos=2,
        videos=videos,
        videos_auto=videos_auto,
        search_themes=search_themes,
        audiences=audiences,
        action_items=list(action_items or []),
    )


def _term(text: str, cost: float, conv: float = 0.0, clicks: int = 10) -> PmaxSearchTermRow:
    return PmaxSearchTermRow(search_term=text, cost=cost, clicks=clicks, conversions=conv)


def _ids(res) -> set[str]:
    return {f.check_id for f in res.findings}


def _one(res, check_id: str):
    return next(f for f in res.findings if f.check_id == check_id)


# ── GR8: нет данных ≠ нет проблем ────────────────────────────────────────────────────
def test_pmax_family_silent_without_data():
    """Не прочитано (None) и «PMax в аккаунте нет» ([]) — оба варианта молчат, а не выдумывают."""
    for camps, groups in ((None, None), ([], [])):
        res = build_audit(
            _report([("PMax", 500.0, 10.0)]),
            pmax_campaigns=camps,
            pmax_asset_groups=groups,
            brand_terms={"aimash"},
        )
        assert not [f for f in res.findings if f.check_id.startswith("pmax_")]


# ── Деньги: запрос PMax ──────────────────────────────────────────────────────────────
def test_pmax_search_term_waste_is_one_tap_exact_negative():
    """Расход ≥ kw_min_spend, клики есть, конверсий нет → минус-слово точным соответствием в один тап
    (PMax принимает минусы на уровне кампании). Показы без кликов деньги не жгли — молчим."""
    camp = _camp(
        terms=[
            _term("бесплатно скачать", 40.0),
            _term("дёшево", 30.0, clicks=0),  # кликов нет → не находка
            _term("купить aimash", 25.0, conv=3.0),  # конвертит → не находка
            _term("копейки", 1.0),  # ниже порога kw_min_spend
        ]
    )
    res = build_audit(_report([("PMax", 500.0, 10.0)]), pmax_campaigns=[camp], pmax_asset_groups=[])
    f = _one(res, "pmax_search_term_waste")
    assert len([x for x in res.findings if x.check_id == "pmax_search_term_waste"]) == 1
    assert f.at_risk == 40.0 and f.facts["search_term"] == "бесплатно скачать"
    assert f.suggested_operation == "add_negative_keywords" and f.suggested_operation in ONE_TAP_OPS
    assert f.evidence["keyword"] == "бесплатно скачать" and f.evidence["match_type"] == "exact"
    # Сегмент денег — КАМПАНИЯ (не запрос): иначе сложится с расходом группы активов.
    assert f.spend_segment == "PMax"
    assert "бесплатно скачать" in finding_text(f, "ru", "USD")


# ── Деньги: группа активов ───────────────────────────────────────────────────────────
def test_pmax_asset_group_no_conv_only_inside_converting_campaign():
    """Группа жжёт бюджет кампании, которая в целом конвертит → находка (деньги = расход групп).
    Если не конвертит ВСЯ кампания — это spend_no_conv, чек молчит (не дублируем чужую находку)."""
    groups = [
        _group("Мёртвая", cost=120.0, conv=0.0, clicks=60),
        _group("Спящая", gid="11", cost=5.0, conv=0.0, clicks=3),  # ниже pmax_min_spend
        _group("Без кликов", gid="12", cost=50.0, conv=0.0, clicks=0),  # показы, но не клики
        _group("Рабочая", gid="13", cost=200.0, conv=10.0),
    ]
    res = build_audit(
        _report([("PMax", 500.0, 10.0)]),
        pmax_campaigns=[_camp(conv=10.0)],
        pmax_asset_groups=groups,
    )
    f = _one(res, "pmax_asset_group_no_conv")
    assert f.at_risk == 120.0 and f.facts["groups"] == 1 and f.facts["examples"] == ["Мёртвая"]
    assert f.suggested_operation is None  # групп активов через API не паузим — только прозой
    assert f.spend_segment == "PMax"

    dead = build_audit(
        _report([("PMax", 500.0, 0.0)]),
        pmax_campaigns=[_camp(conv=0.0)],
        pmax_asset_groups=groups,
    )
    assert "pmax_asset_group_no_conv" not in _ids(dead)


def test_pmax_money_never_double_counted():
    """КЛЮЧЕВОЙ инвариант Ф7: запрос ⊂ группа ⊂ кампания. Обе находки в одной кампании ⇒ в «под
    риском» попадает МАКСИМУМ, а не сумма (иначе 160 из 500 вместо 120 — деньги наружу, клиенту)."""
    res = build_audit(
        _report([("PMax", 500.0, 10.0)]),
        pmax_campaigns=[_camp(conv=10.0, terms=[_term("бесплатно", 40.0)])],
        pmax_asset_groups=[_group("Мёртвая", cost=120.0, conv=0.0, clicks=60)],
    )
    assert {"pmax_search_term_waste", "pmax_asset_group_no_conv"} <= _ids(res)
    assert res.at_risk == 120.0  # max(120, 40), НЕ 160
    assert res.at_risk <= res.total_spend


# ── Каннибализация бренда (G-PM3 + G07) ──────────────────────────────────────────────
def test_pmax_brand_cannibalization_needs_profile_and_flips_advice_on_exclusion():
    terms = [_term("aimash", 20.0, conv=5.0), _term("купить ноутбук", 80.0, conv=5.0)]
    rep = _report([("PMax", 500.0, 10.0)])

    silent = build_audit(rep, pmax_campaigns=[_camp(terms=terms)], pmax_asset_groups=[])
    assert "pmax_brand_cannibalization" not in _ids(silent)  # нет профиля → бренд угадывать нечем

    res = build_audit(
        rep, pmax_campaigns=[_camp(terms=terms)], pmax_asset_groups=[], brand_terms={"aimash"}
    )
    f = _one(res, "pmax_brand_cannibalization")
    assert f.at_risk == 0.0  # искажена атрибуция, деньги не сгорели
    assert f.facts["brand_share"] == 50 and f.facts["brand_excluded"] is False
    assert "включи исключение бренда" in finding_text(f, "ru", "USD").lower()

    excluded = build_audit(
        rep,
        pmax_campaigns=[_camp(terms=terms, brand_exclusions=1)],
        pmax_asset_groups=[],
        brand_terms={"aimash"},
    )
    g = _one(excluded, "pmax_brand_cannibalization")
    assert g.facts["brand_excluded"] is True
    assert "слишком узкий" in finding_text(g, "ru", "USD")

    # Одна брендовая конверсия из десяти — ниже обоих порогов (min_conv=3, share=30%) → молчим.
    tiny = build_audit(
        rep,
        pmax_campaigns=[_camp(terms=[_term("aimash", 5.0, conv=1.0)])],
        pmax_asset_groups=[],
        brand_terms={"aimash"},
    )
    assert "pmax_brand_cannibalization" not in _ids(tiny)


# ── Сила объявлений: приговор Google, а не наша «плотность ассетов» ───────────────────
def test_pmax_ad_strength_flags_poor_not_average():
    rep = _report([("PMax", 500.0, 10.0)])
    camps = [_camp()]
    poor = build_audit(
        rep,
        pmax_campaigns=camps,
        pmax_asset_groups=[
            _group(ad_strength="POOR", action_items=[("YOUTUBE_VIDEO", 1), ("HEADLINE", 3)])
        ],
    )
    f = _one(poor, "pmax_ad_strength_poor")
    assert f.facts["action_items"] == ["YOUTUBE_VIDEO×1", "HEADLINE×3"]
    assert "YOUTUBE_VIDEO×1" in finding_text(f, "ru", "USD")

    avg = build_audit(rep, pmax_campaigns=camps, pmax_asset_groups=[_group(ad_strength="AVERAGE")])
    assert "pmax_ad_strength_poor" not in _ids(avg)

    # Google не вернул оценку («») → молчим, а не считаем её плохой (GR8).
    unknown = build_audit(rep, pmax_campaigns=camps, pmax_asset_groups=[_group(ad_strength="")])
    assert "pmax_ad_strength_poor" not in _ids(unknown)


# ── Видео: авто-сгенерированное Google своим не считается ────────────────────────────
def test_pmax_no_video_ignores_auto_generated():
    rep = _report([("PMax", 500.0, 10.0)])
    camps = [_camp()]
    res = build_audit(
        rep, pmax_campaigns=camps, pmax_asset_groups=[_group(videos=0, videos_auto=2)]
    )
    f = _one(res, "pmax_no_video")
    assert f.facts["videos_auto"] == 2
    assert "худшее из возможных видео" in finding_text(f, "ru", "USD")

    own = build_audit(rep, pmax_campaigns=camps, pmax_asset_groups=[_group(videos=1)])
    assert "pmax_no_video" not in _ids(own)


# ── Сигналы: тема ИЛИ аудитория ИЛИ товарный фид ─────────────────────────────────────
def test_pmax_no_signals_requires_all_three_missing():
    rep = _report([("PMax", 500.0, 10.0)])
    bare = _group(search_themes=0, audiences=0)

    res = build_audit(rep, pmax_campaigns=[_camp()], pmax_asset_groups=[bare])
    assert "pmax_no_signals" in _ids(res)

    with_feed = build_audit(
        rep, pmax_campaigns=[_camp(merchant_id="555")], pmax_asset_groups=[bare]
    )
    assert "pmax_no_signals" not in _ids(with_feed)  # retail-PMax: сигнал — сам фид

    with_audience = build_audit(
        rep, pmax_campaigns=[_camp()], pmax_asset_groups=[_group(search_themes=0, audiences=2)]
    )
    assert "pmax_no_signals" not in _ids(with_audience)


# ── Минус-слова: считаем и аккаунтные списки ─────────────────────────────────────────
def test_pmax_no_negatives_counts_account_level_lists():
    rep = _report([("PMax", 500.0, 10.0)])
    res = build_audit(rep, pmax_campaigns=[_camp(negatives=0)], pmax_asset_groups=[])
    f = _one(res, "pmax_no_negatives")
    assert f.at_risk == 0.0 and f.severity == "info"  # деньги доказывает pmax_search_term_waste

    # Минус-лист привязан на АККАУНТ (фетчер уже сложил его в negatives) → защита есть, молчим.
    guarded = build_audit(rep, pmax_campaigns=[_camp(negatives=1)], pmax_asset_groups=[])
    assert "pmax_no_negatives" not in _ids(guarded)


# ── Обучение: фрагментация, а не бедность ────────────────────────────────────────────
def test_pmax_insufficient_conversions_is_about_fragmentation():
    rep = _report([("PMax A", 300.0, 8.0), ("PMax B", 200.0, 6.0)])
    two = [
        _camp("PMax A", cid="1", cost=300.0, conv=8.0),
        _camp("PMax B", cid="2", cost=200.0, conv=6.0),
    ]
    res = build_audit(rep, pmax_campaigns=two, pmax_asset_groups=[])
    f = _one(res, "pmax_insufficient_conversions")
    assert f.facts["campaigns"] == 2 and f.target_campaign is None
    assert "объедини" in finding_text(f, "ru", "USD").lower()

    # Одна кампания ниже порога — «мало спроса», а не ошибка настройки: лекарства нет, молчим.
    single = build_audit(
        _report([("PMax A", 300.0, 8.0)]),
        pmax_campaigns=[_camp("PMax A", cost=300.0, conv=8.0)],
        pmax_asset_groups=[],
    )
    assert "pmax_insufficient_conversions" not in _ids(single)

    # Одна из двух уже учится (≥30 конв/30д) → сигнал не «размазан», молчим.
    learning = build_audit(
        rep,
        pmax_campaigns=[two[0], _camp("PMax B", cid="2", cost=200.0, conv=35.0)],
        pmax_asset_groups=[],
    )
    assert "pmax_insufficient_conversions" not in _ids(learning)

    # Период 7 дней: 8 конв за неделю → 34 за месяц. Экстраполяция шумная — не судим (порог 14 дней).
    short = build_audit(
        rep,
        pmax_campaigns=[
            _camp("PMax A", cid="1", cost=300.0, conv=2.0, days=7),
            _camp("PMax B", cid="2", cost=200.0, conv=1.0, days=7),
        ],
        pmax_asset_groups=[],
    )
    assert "pmax_insufficient_conversions" not in _ids(short)
