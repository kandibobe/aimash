"""Ф6: денежные чеки каталога claude-ads — G37 (задушенная цель CPA), G05 (бренд + не-бренд в одной
кампании), G50/G51/G52 (расширения).

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


def test_assets_thin_fires_per_type_and_never_touches_score():
    rep = _report([("Поиск", 100.0, 5.0)])
    res = build_audit(rep, campaign_assets=[_assets("Поиск", sitelinks=2, callouts=0, snippets=0)])
    assert {"assets_sitelinks_thin", "assets_callouts_thin", "assets_no_snippets"} <= _ids(res)
    # Семья assets весит 0 до Ф8 → штраф ровно 0 (чек виден, балл не трогает).
    assert res.families["assets"]["penalty"] == 0.0
    assert res.score == build_audit(rep).score
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
