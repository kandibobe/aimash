"""Сбор данных для аудита (READ-ONLY) → build_audit. Живые чтения под таймаут/ретрай семафора
Google Ads (core.resilience.run_ads_read_call); каждое доп-чтение best-effort (сбой → None →
проверка деградирует мягко, аудит не падает). Резолв аккаунта/клиента делает bot-слой (как _do_read)
и передаёт сюда готовый client+cid.

⚠️ audit/ не импортирует ads.mutations / ads.service (инвариант test_audit_engine) — здесь только
read-слой (reports/ads.read) + core.resilience.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from audit.engine import AuditResult, build_audit


async def gather_audit(
    client: Any,
    customer_id: str,
    period,
    *,
    thresholds: dict | None = None,
    target_cpa: float | None = None,
) -> AuditResult:
    """Собрать отчёт + аудит-фетчеры (IS / optimization_score / конверсии / стратегии ставок /
    Heartbeat: модерация объявлений + статус аккаунта) и построить AuditResult. Доп-чтения
    best-effort (сбой одного не роняет аудит). READ-ONLY."""
    # Ленивая загрузка: не тянем reports/ads.read на уровне модуля (audit/ должен оставаться лёгким).
    from ads.read import account_currency
    from core.resilience import run_ads_read_call
    from reports.queries import (
        KEYWORD_INVENTORY_LIMIT,
        fetch_account_status,
        fetch_ad_policy_health,
        fetch_adgroup_structure,
        fetch_bid_landscape,
        fetch_bid_simulations,
        fetch_budget_simulations,
        fetch_campaign_assets,
        fetch_campaign_settings,
        fetch_conversion_health,
        fetch_geo_waste,
        fetch_impression_share,
        fetch_keyword_inventory,
        fetch_keyword_quality,
        fetch_negative_lists,
        fetch_optimization_score,
        fetch_pmax_asset_groups,
        fetch_pmax_campaigns,
        fetch_recommendations,
        fetch_rsa_assets,
        fetch_schedule,
        fetch_search_terms,
        read_campaign_bidding,
    )
    from reports.service import build_account_report_async

    cid = str(customer_id)

    async def _safe(fn, *args, label: str):
        try:
            return await run_ads_read_call(fn, *args, label=label)
        except Exception:  # noqa: BLE001 — доп-сигнал не должен ронять аудит
            return None

    currency = (await _safe(account_currency, client, cid, label="audit_currency")) or ""
    # Отчёт (totals + разбивки) — базис; фетчеры аудита идут параллельно с ним.
    (
        report,
        is_rows,
        opt,
        cas,
        bidding,
        recs,
        st,
        ad_policy,
        acct_status,
        adgroup_structure,
        negative_lists,
        keyword_quality,
        geo_waste,
        schedule,
        bid_landscape,
        bid_sims,
        budget_sims,
        rsa_ads,
        kw_inventory,
        campaign_assets,
        campaign_settings,
        pmax_campaigns,
        pmax_asset_groups,
    ) = await asyncio.gather(
        build_account_report_async(client, cid, period, with_comparison=False, currency=currency),
        _safe(fetch_impression_share, client, cid, period, label="audit_is"),
        _safe(fetch_optimization_score, client, cid, label="audit_opt_score"),
        _safe(fetch_conversion_health, client, cid, label="audit_conv"),
        _safe(read_campaign_bidding, client, cid, label="audit_bidding"),
        _safe(fetch_recommendations, client, cid, label="audit_recs"),
        _safe(fetch_search_terms, client, cid, period, label="audit_search_terms"),
        _safe(fetch_ad_policy_health, client, cid, label="audit_ad_policy"),
        _safe(fetch_account_status, client, cid, label="audit_account_status"),
        _safe(fetch_adgroup_structure, client, cid, period, label="audit_adgroup_structure"),
        _safe(fetch_negative_lists, client, cid, label="audit_negative_lists"),
        _safe(fetch_keyword_quality, client, cid, period, label="audit_keyword_quality"),
        _safe(fetch_geo_waste, client, cid, period, label="audit_geo_waste"),
        _safe(fetch_schedule, client, cid, period, label="audit_schedule"),
        _safe(fetch_bid_landscape, client, cid, period, label="audit_bid_landscape"),
        _safe(fetch_bid_simulations, client, cid, label="audit_bid_sims"),
        _safe(fetch_budget_simulations, client, cid, label="audit_budget_sims"),
        _safe(fetch_rsa_assets, client, cid, period, label="audit_rsa_assets"),
        _safe(fetch_keyword_inventory, client, cid, period, label="audit_keyword_inventory"),
        _safe(fetch_campaign_assets, client, cid, label="audit_campaign_assets"),
        _safe(fetch_campaign_settings, client, cid, period, label="audit_campaign_settings"),
        _safe(fetch_pmax_campaigns, client, cid, period, label="audit_pmax_campaigns"),
        _safe(fetch_pmax_asset_groups, client, cid, period, label="audit_pmax_asset_groups"),
    )

    # Ф6 (G05): бренд-токены из профиля клиента (§20) — локальная БД, не Google Ads (в gather выше
    # не идут: там семафор Ads-чтений). Нет профиля/сбой → пустой набор ⇒ чек молчит, а не гадает.
    try:
        from clients.store import ClientProfileStore

        brand_terms = await ClientProfileStore().brand_tokens(cid)
    except Exception:  # noqa: BLE001 — профиль опционален, аудит не роняем
        brand_terms = set()

    # N1.3: какие best-effort сигналы НЕ получены (сбой → None). Пустой список ([]) — НЕ пробел:
    # чтение прошло, данных просто нет. Рендер честно покажет «недостаточно данных» вместо
    # молчаливого «в норме» (GR8: нет данных ≠ здорово). account_status — детектор катастрофы,
    # а не «здоровье семьи»: его отсутствие НЕ пробел (в data_gaps не добавляем).
    data_gaps = [
        name
        for name, val in (
            ("impression_share", is_rows),
            ("optimization_score", opt),
            ("conversion_actions", cas),
            ("bidding", bidding),
            ("recommendations", recs),
            ("search_terms", st),
            ("ad_policy", ad_policy),
            ("adgroup_structure", adgroup_structure),
            ("negative_lists", negative_lists),
            ("keyword_quality", keyword_quality),
            ("geo_waste", geo_waste),
            ("schedule", schedule),
            ("bid_landscape", bid_landscape),
            ("rsa_ads", rsa_ads),
            ("keyword_inventory", kw_inventory),
            ("campaign_assets", campaign_assets),
            ("campaign_settings", campaign_settings),
        )
        if val is None
    ]
    # Ф7: два чтения — один сигнал (семья pmax слепа, если не прочитано ЛЮБОЕ из них). Пустой список
    # («PMax в аккаунте нет») пробелом НЕ считается: это факт, а не отсутствие данных.
    if pmax_campaigns is None or pmax_asset_groups is None:
        data_gaps.append("pmax")
    # Ревизия волны: инвентарь ключей УПЁРСЯ в LIMIT ⇒ он неполон, а в майнинге он — отрицательный
    # фильтр («чего у меня нет»). Неполный список ⇒ совет «собери/заминусуй» может указать на СВОЙ же
    # ключ. Чеки harvest/ngram обязаны замолчать, а карточка — честно сказать «данных не хватает».
    kw_truncated = kw_inventory is not None and len(kw_inventory) >= KEYWORD_INVENTORY_LIMIT
    if kw_truncated and "keyword_inventory" not in data_gaps:
        data_gaps.append("keyword_inventory")
    # RSA прочитаны, но НИ У ОДНОГО нет даты старта (`start_date_time` пуст у большинства объявлений —
    # это ограничение расписания, а не дата создания) ⇒ чек rsa_stale физически слеп. Без этой пометки
    # рендер сказал бы «объявления в норме» там, где мы про их возраст просто ничего не знаем (GR8).
    if rsa_ads and all(int(getattr(a, "age_days", -1) or -1) < 0 for a in rsa_ads):
        data_gaps.append("rsa_age")
    # Симуляторы Google в data_gaps НЕ идут: их отсутствие — норма (Google строит их только при
    # достаточных данных и только на ручных ставках), а не сбой чтения. Пометив пробелом, мы бы
    # написали «данных нет» здоровому аккаунту без симуляторов.

    return build_audit(
        report,
        thresholds,
        target_cpa,
        is_rows=is_rows,
        conversion_actions=cas,
        bidding=bidding,
        optimization_score=opt,
        recommendations=recs,
        search_terms=st,
        ad_policy=ad_policy,
        account_status=acct_status,
        data_gaps=data_gaps,
        adgroup_structure=adgroup_structure,
        negative_lists=negative_lists,
        keyword_quality=keyword_quality,
        geo_waste=geo_waste,
        schedule=schedule,
        bid_landscape=bid_landscape,
        bid_simulations=bid_sims,
        budget_simulations=budget_sims,
        rsa_ads=rsa_ads,
        keyword_inventory=kw_inventory,
        keyword_inventory_truncated=kw_truncated,
        brand_terms=brand_terms,
        campaign_assets=campaign_assets,
        campaign_settings=campaign_settings,
        pmax_campaigns=pmax_campaigns,
        pmax_asset_groups=pmax_asset_groups,
    )


@dataclass
class BidsResult:
    """/bids: доска возможностей по ставкам. has_landscape=False → слой ставок НЕ прочитан (сбой/нет
    доступа) — это «нет данных», а не «поднимать нечего»: рендер обязан сказать разное (GR8/GR10)."""

    items: list
    currency: str
    has_landscape: bool
    has_sims: bool


async def gather_bids(
    client: Any,
    customer_id: str,
    period,
    *,
    thresholds: dict | None = None,
    target_cpa: float | None = None,
    top_n: int = 10,
) -> BidsResult:
    """Ф1: сбор под /bids — ЛЁГКИЙ путь (5 чтений), не весь аудит-контур: слой ставок/позиций +
    симуляторы + totals (средний CPA как потолок окупаемости) + стратегии кампаний (цель tCPA).
    Формулы и пороги — те же, что у чеков аудита (audit.bidscape), иначе /bids и /audit разошлись бы
    в советах. READ-ONLY: доска ничего не создаёт, ставку меняет ОТДЕЛЬНАЯ команда через confirm-гейт."""
    from ads.read import account_currency
    from audit.bidscape import board
    from audit.engine import _Ctx
    from audit.thresholds import DEFAULT_AUDIT_THRESHOLDS
    from core.resilience import run_ads_read_call
    from reports.queries import (
        fetch_bid_landscape,
        fetch_bid_simulations,
        fetch_totals,
        read_campaign_bidding,
    )

    cid = str(customer_id)

    async def _safe(fn, *args, label: str):
        try:
            return await run_ads_read_call(fn, *args, label=label)
        except Exception:  # noqa: BLE001 — доп-сигнал не должен ронять доску
            return None

    currency, totals, rows, sims, bidding = await asyncio.gather(
        _safe(account_currency, client, cid, label="bids_currency"),
        _safe(fetch_totals, client, cid, period, label="bids_totals"),
        _safe(fetch_bid_landscape, client, cid, period, label="bids_landscape"),
        _safe(fetch_bid_simulations, client, cid, label="bids_sims"),
        _safe(read_campaign_bidding, client, cid, label="bids_bidding"),
    )

    thr = {**DEFAULT_AUDIT_THRESHOLDS, **(thresholds or {})}
    # Цель CPA: пер-кампанийная (tCPA) побеждает глобальный /target — ровно как в чеках (_Ctx.target_for).
    ctx = _Ctx(
        target_cpa=target_cpa,
        bidding_by_name={b.name: b for b in (bidding or [])},
    )
    items = board(
        rows,
        sims,
        acct_cpa=float(getattr(totals, "cpa", 0.0) or 0.0),
        target_for=ctx.target_for,
        gap_min=float(thr["bid_gap_min"]),
        min_cost=float(thr["min_spend"]),
        min_conv_gain=float(thr["sim_min_conv_gain"]),
        top_n=top_n,
    )
    return BidsResult(
        items=items,
        currency=currency or "",
        has_landscape=rows is not None,
        has_sims=sims is not None,
    )
