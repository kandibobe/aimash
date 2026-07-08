"""Сбор данных для аудита (READ-ONLY) → build_audit. Живые чтения под таймаут/ретрай семафора
Google Ads (core.resilience.run_ads_read_call); каждое доп-чтение best-effort (сбой → None →
проверка деградирует мягко, аудит не падает). Резолв аккаунта/клиента делает bot-слой (как _do_read)
и передаёт сюда готовый client+cid.

⚠️ audit/ не импортирует ads.mutations / ads.service (инвариант test_audit_engine) — здесь только
read-слой (reports/ads.read) + core.resilience.
"""

from __future__ import annotations

import asyncio
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
    """Собрать отчёт + аудит-фетчеры (IS / optimization_score / конверсии / стратегии ставок) и
    построить AuditResult. Доп-чтения best-effort (сбой одного не роняет аудит). READ-ONLY."""
    # Ленивая загрузка: не тянем reports/ads.read на уровне модуля (audit/ должен оставаться лёгким).
    from ads.read import account_currency
    from core.resilience import run_ads_read_call
    from reports.queries import (
        fetch_conversion_health,
        fetch_impression_share,
        fetch_optimization_score,
        fetch_recommendations,
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
    report, is_rows, opt, cas, bidding, recs, st = await asyncio.gather(
        build_account_report_async(client, cid, period, with_comparison=False, currency=currency),
        _safe(fetch_impression_share, client, cid, period, label="audit_is"),
        _safe(fetch_optimization_score, client, cid, label="audit_opt_score"),
        _safe(fetch_conversion_health, client, cid, label="audit_conv"),
        _safe(read_campaign_bidding, client, cid, label="audit_bidding"),
        _safe(fetch_recommendations, client, cid, label="audit_recs"),
        _safe(fetch_search_terms, client, cid, period, label="audit_search_terms"),
    )

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
    )
