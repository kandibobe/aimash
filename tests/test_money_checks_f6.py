"""Ф6: денежные чеки каталога claude-ads — G37 (задушенная цель CPA), G05 (бренд + не-бренд в одной
кампании), G50/G51/G52 (расширения), G12 (КМС на поисковой кампании), G11 (гео «присутствие ИЛИ
интерес»).

Что пинуется (всё — про ложные срабатывания, они дороже пропусков):
1. G37 молчит, пока факт. CPA не превысил цель ВДВОЕ и пока конверсий меньше tcpa_min_conv: «CPA» по
   одной случайной конверсии — не факт, а деление на единицу.
2. G05 молчит БЕЗ профиля клиента: бренд угадывать нечем, а ложно назвать брендовым чужой ключ —
   значит посоветовать разрезать кампанию пополам без причины. Спящие ключи (0 показов) не в счёт.
3. Ассеты считаются на ТРЁХ уровнях привязки: ситилинк, привязанный на АККАУНТ, работает во всех
   кампаниях — не учесть его = написать «ситилинков нет» аккаунту, где они есть.
4. Семья assets пока весит 0 (ребаланс — Ф8): чеки работают и видны, но балл не двигают.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402
from reports.queries import (  # noqa: E402
    Breakdown,
    CampaignAssetsRow,
    CampaignSettingsRow,
    KeywordInventoryRow,
    Metrics,
)

from audit.engine import build_audit  # noqa: E402
from audit.render import finding_text  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


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


def _bidding(name: str, target_cpa: float | None):
    return SimpleNamespace(
        campaign_id="1", name=name, strategy_type="MAXIMIZE_CONVERSIONS", target_cpa=target_cpa
    )


def _ids(res) -> set[str]:
    return {f.check_id for f in res.findings}


def _kw(campaign: str, text: str, *, impressions: int = 100, cost: float = 5.0):
    return KeywordInventoryRow(
        campaign=campaign,
        ad_group="g1",
        keyword=text,
        match_type="EXACT",
        metrics=Metrics(
            impressions=impressions, clicks=10, cost_micros=int(cost * 1_000_000), conversions=0.0
        ),
    )


# ── G37: цель CPA много ниже фактической ─────────────────────────────────────────────
def test_target_cpa_too_low_fires_only_on_real_gap_and_enough_conversions():
    # Факт. CPA = 300/10 = 30 при цели 10 → цель втрое ниже: Google душит показы.
    res = build_audit(_report([("Поиск", 300.0, 10.0)]), bidding=[_bidding("Поиск", 10.0)])
    assert "target_cpa_too_low" in _ids(res)
    f = next(f for f in res.findings if f.check_id == "target_cpa_too_low")
    assert f.at_risk == 0.0  # это НЕ потраченные впустую деньги, а не потраченные вовсе
    assert f.facts["actual_cpa"] == 30.0 and f.facts["target_cpa"] == 10.0
    txt = finding_text(f, "ru", "USD")
    assert "30" in txt and "10" in txt

    # Разрыв меньше двукратного (факт 15 при цели 10) → цель рабочая, молчим.
    ok = build_audit(_report([("Поиск", 150.0, 10.0)]), bidding=[_bidding("Поиск", 10.0)])
    assert "target_cpa_too_low" not in _ids(ok)

    # Разрыв огромный, но конверсий 2 (< tcpa_min_conv=3) → «фактический CPA» это шум, не факт.
    noisy = build_audit(_report([("Поиск", 300.0, 2.0)]), bidding=[_bidding("Поиск", 10.0)])
    assert "target_cpa_too_low" not in _ids(noisy)

    # Цели нет вовсе (стратегия без tCPA) → нечего сравнивать.
    no_target = build_audit(_report([("Поиск", 300.0, 10.0)]), bidding=[_bidding("Поиск", None)])
    assert "target_cpa_too_low" not in _ids(no_target)


# ── G05: бренд и не-бренд в одной кампании ───────────────────────────────────────────
def test_brand_nonbrand_mixed_needs_profile_and_live_keywords():
    inv = [
        _kw("Общая", "aimash", cost=30.0),  # брендовый
        _kw("Общая", "купить ноутбук"),
        _kw("Общая", "ноутбук цена"),
        _kw("Общая", "ноутбук доставка"),
    ]
    rep = _report([("Общая", 45.0, 3.0)])

    res = build_audit(rep, keyword_inventory=inv, brand_terms={"aimash"})
    assert "brand_nonbrand_mixed" in _ids(res)
    f = next(f for f in res.findings if f.check_id == "brand_nonbrand_mixed")
    assert f.facts["brand_kw"] == 1 and f.facts["other_kw"] == 3
    assert f.facts["brand_share"] == 67  # 30 из 45 расхода на ключах — бренд
    assert "aimash" in finding_text(f, "ru", "USD")

    # Профиля клиента нет → бренд-токенов нет → чек МОЛЧИТ (не гадаем по названию кампании).
    assert "brand_nonbrand_mixed" not in _ids(build_audit(rep, keyword_inventory=inv))

    # Небрендовых ключей меньше порога (2 < 3) → это брендовая кампания с хвостом, не «свалка».
    thin = build_audit(rep, keyword_inventory=inv[:3], brand_terms={"aimash"})
    assert "brand_nonbrand_mixed" not in _ids(thin)

    # Небрендовые ключи есть, но БЕЗ показов — они ни с кем бюджет не делят.
    sleeping = [inv[0]] + [_kw("Общая", k.keyword, impressions=0) for k in inv[1:]]
    assert "brand_nonbrand_mixed" not in _ids(
        build_audit(rep, keyword_inventory=sleeping, brand_terms={"aimash"})
    )


# ── G50/G51/G52: расширения ──────────────────────────────────────────────────────────
def _assets(name: str, *, sitelinks: int, callouts: int, snippets: int, channel: str = "SEARCH"):
    return CampaignAssetsRow(
        campaign_id="1",
        campaign=name,
        channel_type=channel,
        sitelinks=sitelinks,
        callouts=callouts,
        snippets=snippets,
    )


def test_assets_thin_fires_per_type_and_bites_score_within_family_cap():
    rep = _report([("Поиск", 100.0, 5.0)])
    res = build_audit(rep, campaign_assets=[_assets("Поиск", sitelinks=2, callouts=0, snippets=0)])
    assert {"assets_sitelinks_thin", "assets_callouts_thin", "assets_no_snippets"} <= _ids(res)
    # Ф8: семья assets ожила (вес 3). Три info-находки: 0.4 × NONMONEY 0.5 = 0.2 каждая → Σ 0.6 →
    # штраф 3 × 0.6 = 1.8. Балл двигается, но потолок семьи (3) не пробивается — расширения это
    # упущенный CTR, а не сожжённые деньги.
    assert res.families["assets"]["penalty"] == 1.8
    assert res.families["assets"]["at_risk"] == 0.0
    assert res.score == build_audit(rep).score - 2  # round(100−1.8) = 98
    f = next(f for f in res.findings if f.check_id == "assets_sitelinks_thin")
    assert f.advice_operation == "add_sitelinks" and not f.one_tap  # кнопки нет: это не one-tap
    assert "Расширения" in finding_text(f, "ru", "USD")

    # Полный набор расширений → тишина.
    full = build_audit(rep, campaign_assets=[_assets("Поиск", sitelinks=6, callouts=4, snippets=2)])
    assert not (
        {"assets_sitelinks_thin", "assets_callouts_thin", "assets_no_snippets"} & _ids(full)
    )

    # Не SEARCH (Display/PMax) → расширения работают иначе, не флажим.
    display = build_audit(
        rep,
        campaign_assets=[_assets("Поиск", sitelinks=0, callouts=0, snippets=0, channel="DISPLAY")],
    )
    assert not (
        {"assets_sitelinks_thin", "assets_callouts_thin", "assets_no_snippets"} & _ids(display)
    )

    # Кампания-«копейка» (расход ниже assets_min_spend) → шум, а не находка.
    cheap = build_audit(
        _report([("Тест", 3.0, 0.0)]),
        campaign_assets=[_assets("Тест", sitelinks=0, callouts=0, snippets=0)],
    )
    assert not (
        {"assets_sitelinks_thin", "assets_callouts_thin", "assets_no_snippets"} & _ids(cheap)
    )


def test_account_level_assets_count_for_every_campaign():
    """Фетчер складывает три уровня привязки: аккаунт-уровневый ситилинк действует во ВСЕХ кампаниях.
    Проверяем именно сборку счётчиков в fetch_campaign_assets (SDK — фейковый)."""
    from reports.queries import fetch_campaign_assets

    def _row(**kw):
        return SimpleNamespace(**kw)

    def _ft(name: str):
        return SimpleNamespace(name=name)

    class _Svc:
        def search(self, customer_id: str, query: str):
            if "FROM customer_asset" in query:
                return [_row(customer_asset=_row(field_type=_ft("SITELINK"))) for _ in range(4)]
            if "FROM campaign_asset" in query:
                return [
                    _row(
                        campaign=_row(id=1),
                        campaign_asset=_row(field_type=_ft("CALLOUT")),
                    )
                ]
            if "FROM ad_group_asset" in query:
                return [
                    _row(
                        campaign=_row(id=1),
                        ad_group_asset=_row(field_type=_ft("CALLOUT")),
                    )
                ]
            return [
                _row(
                    campaign=_row(id=1, name="Поиск", advertising_channel_type=_ft("SEARCH")),
                )
            ]

    class _Client:
        def get_service(self, _name: str):
            return _Svc()

    with allowed_ids(DRAFT_ACCOUNT_ID):  # замок чтения — фетчер обязан его проходить, не обходить
        rows = fetch_campaign_assets(_Client(), DRAFT_ACCOUNT_ID)
    assert len(rows) == 1
    r = rows[0]
    assert r.sitelinks == 4  # все четыре — с уровня АККАУНТА, на кампании их нет
    assert r.callouts == 2  # кампания + группа
    assert r.snippets == 0
    # …и чек про ситилинки с такими данными молчать обязан (иначе — ложное «ситилинков нет»).
    res = build_audit(_report([("Поиск", 100.0, 5.0)]), campaign_assets=rows)
    assert "assets_sitelinks_thin" not in _ids(res)
    assert "assets_no_snippets" in _ids(res)


# ── Ф6.2 · G12/G11: настройки, которые ЖГУТ деньги ───────────────────────────────────
def _settings(
    name: str,
    *,
    channel: str = "SEARCH",
    content: bool = False,
    geo: str = "PRESENCE",
    content_cost: float = 0.0,
    content_conv: float = 0.0,
    outside_cost: float = 0.0,
    outside_conv: float = 0.0,
):
    return CampaignSettingsRow(
        campaign_id="1",
        campaign=name,
        channel_type=channel,
        search_network=False,
        content_network=content,
        geo_target_type=geo,
        content_cost=content_cost,
        content_conversions=content_conv,
        outside_cost=outside_cost,
        outside_conversions=outside_conv,
    )


def test_display_on_search_flags_burned_money_not_the_checkbox():
    """G12 — находка только там, где КМС УЖЕ съел деньги и не дал конверсий. Включённая галочка без
    расхода — не потеря; конвертящий КМС — не слив (GR8: галочка ≠ факт)."""
    rep = _report([("Поиск", 100.0, 5.0)])
    res = build_audit(rep, campaign_settings=[_settings("Поиск", content=True, content_cost=40.0)])
    f = next(f for f in res.findings if f.check_id == "display_on_search_campaign")
    assert f.at_risk == 40.0 and f.family == "waste" and f.spend_segment == "Поиск"
    assert f.at_risk <= res.total_spend  # расход по сети ⊂ расход кампании
    assert "КМС" in finding_text(f, "ru", "USD")

    # Галочка включена, но КМС ничего не потратил → нечего терять, молчим.
    quiet = build_audit(rep, campaign_settings=[_settings("Поиск", content=True, content_cost=0.0)])
    assert "display_on_search_campaign" not in _ids(quiet)

    # КМС тратит и КОНВЕРТИТ → это не слив, а канал. Молчим.
    works = build_audit(
        rep,
        campaign_settings=[_settings("Поиск", content=True, content_cost=40.0, content_conv=3.0)],
    )
    assert "display_on_search_campaign" not in _ids(works)

    # Кампания и есть медийная (DISPLAY) → КМС там по определению, не находка.
    display = build_audit(
        rep,
        campaign_settings=[_settings("Поиск", channel="DISPLAY", content=True, content_cost=40.0)],
    )
    assert "display_on_search_campaign" not in _ids(display)

    # Настройки не прочитаны (фетчер упал) → чек молчит, а не пишет «всё чисто».
    assert "display_on_search_campaign" not in _ids(build_audit(rep))


def test_geo_interest_waste_measures_money_of_people_outside_target():
    """G11 — «присутствие ИЛИ интерес» флажим ТОЛЬКО с деньгами клиентов, физически вне таргета
    (user_location_view.targeting_location=FALSE). Нет типа гео (нет данных) → молчим.

    Эпоха 5: деньги вне таргета в headline кладёт geo_no_conv (те же клики, другой срез) ⇒ здесь
    at_risk=0, но балл штрафуется ПРОПОРЦИОНАЛЬНО (score_intensity = доля расхода вне таргета) —
    иначе «Под риском» задваивался бы (сегменты «geo::кампания::регион» и «кампания» не пересекались,
    и дедуп их складывал). Цифра в тексте карточки — из facts, она не пропала."""
    rep = _report([("Поиск", 100.0, 5.0)])
    res = build_audit(
        rep,
        campaign_settings=[_settings("Поиск", geo="PRESENCE_OR_INTEREST", outside_cost=50.0)],
    )
    f = next(f for f in res.findings if f.check_id == "geo_interest_waste")
    assert f.family == "geo"
    assert f.at_risk == 0.0 and f.spend_segment is None  # деньги — за geo_no_conv
    assert f.facts["outside_cost"] == 50.0  # …но факт не потерян
    assert f.score_intensity == 0.5  # 50 из 100 расхода: штраф по доле, не фикс NONMONEY (0.5≠факт)
    assert "ВНЕ ваших регионов" in finding_text(f, "ru", "USD")

    # Тот же слив, но кампания вдесятеро больше ⇒ болезнь та же, а вес её меньше: интенсивность
    # ПАДАЕТ (фикс NONMONEY_INTENSITY этого различить не мог бы).
    big = build_audit(
        _report([("Поиск", 1000.0, 50.0)]),
        campaign_settings=[_settings("Поиск", geo="PRESENCE_OR_INTEREST", outside_cost=50.0)],
    )
    assert (
        next(f for f in big.findings if f.check_id == "geo_interest_waste").score_intensity == 0.05
    )

    # Гео-тип «присутствие» → настройка верная, вне-таргетный расход (если и есть) — погрешность гео.
    ok = build_audit(rep, campaign_settings=[_settings("Поиск", geo="PRESENCE", outside_cost=50.0)])
    assert "geo_interest_waste" not in _ids(ok)

    # «Интересующиеся» конвертят → у онлайн-бизнеса настройка законна. Молчим.
    works = build_audit(
        rep,
        campaign_settings=[
            _settings("Поиск", geo="PRESENCE_OR_INTEREST", outside_cost=50.0, outside_conv=2.0)
        ],
    )
    assert "geo_interest_waste" not in _ids(works)

    # Google не вернул тип гео → «» ⇒ чек молчит (нет данных ≠ «настроено верно»).
    unknown = build_audit(rep, campaign_settings=[_settings("Поиск", geo="", outside_cost=50.0)])
    assert "geo_interest_waste" not in _ids(unknown)


def test_fetch_campaign_settings_sums_only_content_and_outside_target():
    """Фетчер: расход по КМС берётся ТОЛЬКО из строк CONTENT, «интересный» расход — ТОЛЬКО из строк
    targeting_location=FALSE. Иначе чек припишет кампании весь её расход и соврёт в разы."""
    from reports.queries import fetch_campaign_settings

    def _row(**kw):
        return SimpleNamespace(**kw)

    def _m(cost: float, conv: float = 0.0):
        return _row(cost_micros=int(cost * 1_000_000), conversions=conv)

    class _Svc:
        def search(self, customer_id: str, query: str):
            if "segments.ad_network_type" in query:
                return [
                    _row(
                        campaign=_row(id=1),
                        segments=_row(ad_network_type=_row(name="SEARCH")),
                        metrics=_m(90.0, 4.0),
                    ),
                    _row(
                        campaign=_row(id=1),
                        segments=_row(ad_network_type=_row(name="CONTENT")),
                        metrics=_m(30.0, 0.0),
                    ),
                ]
            if "FROM user_location_view" in query:
                return [
                    _row(
                        campaign=_row(id=1),
                        user_location_view=_row(targeting_location=True),
                        metrics=_m(100.0, 4.0),
                    ),
                    _row(
                        campaign=_row(id=1),
                        user_location_view=_row(targeting_location=False),
                        metrics=_m(20.0, 0.0),
                    ),
                ]
            return [
                _row(
                    campaign=_row(
                        id=1,
                        name="Поиск",
                        advertising_channel_type=_row(name="SEARCH"),
                        network_settings=_row(
                            target_search_network=False, target_content_network=True
                        ),
                        geo_target_type_setting=_row(
                            positive_geo_target_type=_row(name="PRESENCE_OR_INTEREST")
                        ),
                    )
                )
            ]

    class _Client:
        def get_service(self, _name: str):
            return _Svc()

    with allowed_ids(DRAFT_ACCOUNT_ID):  # замок чтения — фетчер обязан его проходить
        rows = fetch_campaign_settings(
            _Client(), DRAFT_ACCOUNT_ID, SimpleNamespace(gaql_between=lambda: "X")
        )
    assert len(rows) == 1
    r = rows[0]
    assert r.content_network is True and r.geo_target_type == "PRESENCE_OR_INTEREST"
    assert r.content_cost == 30.0 and r.content_conversions == 0.0  # SEARCH-строка НЕ в счёт
    assert r.outside_cost == 20.0 and r.outside_conversions == 0.0  # targeted-строка НЕ в счёт

    # …и оба чека на этих данных срабатывают на реальные деньги.
    res = build_audit(_report([("Поиск", 120.0, 4.0)]), campaign_settings=rows)
    assert {"display_on_search_campaign", "geo_interest_waste"} <= _ids(res)
    # ТОЧНОЕ значение, а не «≤ расхода»: сливы КМС (30) и «интересующихся» (20) — РАЗНЫЕ срезы одних
    # и тех же денег кампании, складывать их нельзя. Неравенство «≤ 120» пропустило бы и 50 (двойной
    # счёт), и 0 (потерянные деньги) — пинуем ровно 30: КМС в headline, гео-тип штрафует лишь балл.
    assert res.at_risk == 30.0 and res.total_spend == 120.0


def test_geo_interest_waste_does_not_double_count_geo_no_conv_money():
    """Регрессия (ревизия волны): гео-слив НЕ задваивается. geo_no_conv (регионы без конверсий) и
    geo_interest_waste (гео-тип «присутствие ИЛИ интерес») смотрят на ОДНИ И ТЕ ЖЕ клики людей вне
    таргета — но разными срезами (регион vs кампания), поэтому их spend_segment не пересекались и
    _dedup_at_risk их СКЛАДЫВАЛ: сожгли 300, «Под риском» показывал 600.

    Контракт: деньги в headline кладёт РОВНО ОДИН чек (geo_no_conv), второй штрафует только балл."""
    from reports.queries import GeoWasteRow

    rep = _report([("Поиск", 1000.0, 10.0)])
    geo_rows = [  # регион вне таргета: 300 сожжено, 0 конверсий
        GeoWasteRow(
            campaign="Поиск",
            region="Мухосранск",
            metrics=Metrics(impressions=500, clicks=50, cost_micros=300_000_000, conversions=0.0),
        )
    ]
    res = build_audit(
        rep,
        geo_waste=geo_rows,
        campaign_settings=[_settings("Поиск", geo="PRESENCE_OR_INTEREST", outside_cost=300.0)],
    )
    assert {"geo_no_conv", "geo_interest_waste"} <= _ids(res)
    assert res.at_risk == 300.0  # ровно сожжённое, НЕ 600
