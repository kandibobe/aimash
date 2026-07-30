"""Outcome Logger — запись результата execute_confirmed для self-learning.

Волна 5. После каждой применённой мутации асинхронно записывает строку в outcome_log
с metrics_before. Через 7 дней OutcomeChecker собирает metrics_after

Использование:
    from learning.outcome_log import record_outcome, check_pending_outcomes

    # После execute_confirmed
    record_outcome(
        confirmation_id=...,
        account_id=...,
        platform="google",
        operation="update_budget",
        summary="Бюджет: $100→$115",
        metrics_before=cpa_snapshot,  # опционально — снимок до
        budget_before=100.0,
        budget_after=115.0,
    )
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import OutcomeLog
from db.session import Session

logger = logging.getLogger(__name__)


# ── Record outcome ────────────────────────────────────────────────────────────


async def record_outcome(
    *,
    confirmation_id: str,
    account_id: str,
    account_name: str | None = None,
    platform: str = "google",
    campaign_id: str | None = None,
    operation: str,
    summary: str,
    metrics_before: dict | None = None,
    budget_before: float | None = None,
    budget_after: float | None = None,
    bid_before: float | None = None,
    bid_after: float | None = None,
) -> None:
    """Записать outcome в БД.

    Не роняет основной поток при ошибке — ошибка молча логируется.
    """
    try:
        async with Session() as sess:
            row = OutcomeLog(
                confirmation_id=confirmation_id,
                account_id=account_id,
                account_name=account_name,
                platform=platform,
                campaign_id=campaign_id,
                operation=operation,
                summary=summary,
                metrics_before=metrics_before,
                budget_before=budget_before,
                budget_after=budget_after,
                bid_before=bid_before,
                bid_after=bid_after,
                verdict="pending",
                applied_at=datetime.now(timezone.utc),
            )
            sess.add(row)
            await sess.commit()
            logger.info("Outcome recorded: %s (%s)", confirmation_id, operation)
    except Exception:
        logger.warning("Failed to record outcome for %s", confirmation_id, exc_info=True)


# ── Outcome Checker ───────────────────────────────────────────────────────────


async def check_pending_outcomes(
    min_age_days: int = 7,
    limit: int = 10,
) -> list[dict]:
    """Проверить pending-исходы старше N дней.

    Для каждого исхода:
    1. Снимает metrics_after через get_campaign_stats
    2. Сравнивает с metrics_before
    3. Выставляет verdict: success/neutral/failure
    4. Обновляет строку в БД

    Возвращает список проверенных исходов (для memory-правил).
    """
    try:
        async with Session() as sess:
            cutoff = datetime.now(timezone.utc)
            from datetime import timedelta

            cutoff = cutoff - timedelta(days=min_age_days)

            rows = (
                (
                    await sess.execute(
                        select(OutcomeLog)
                        .where(
                            OutcomeLog.verdict == "pending",
                            OutcomeLog.applied_at <= cutoff,
                        )
                        .order_by(OutcomeLog.applied_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

        results = []
        for row in rows:
            try:
                verdict_data = await _evaluate_outcome(row)
                async with Session() as sess:
                    r = await sess.get(OutcomeLog, row.id)
                    if r:
                        r.metrics_after = verdict_data.get("metrics_after")
                        r.checked_at = datetime.now(timezone.utc)
                        r.verdict = verdict_data["verdict"]
                        r.reason = verdict_data.get("reason")
                        r.delta_percent = verdict_data.get("delta_percent")
                        await sess.commit()
                results.append(verdict_data)
            except Exception:
                logger.warning("Failed to evaluate outcome id=%s", row.id, exc_info=True)
                # Пометить как error чтобы не зацикливаться
                async with Session() as sess:
                    r = await sess.get(OutcomeLog, row.id)
                    if r:
                        r.verdict = "error"
                        r.reason = "Evaluation failed"
                        r.checked_at = datetime.now(timezone.utc)
                        await sess.commit()

        return results
    except Exception:
        logger.warning("check_pending_outcomes failed", exc_info=True)
        return []


async def _evaluate_outcome(
    row: OutcomeLog,
    metrics_after: dict | None = None,
) -> dict:
    """Оценить один исход: сравнить метрики до и после.

    metrics_after передаётся вызывающим (OutcomeChecker cron через MCP get_campaign_stats).
    Если не передан — вердикт neutral.
    """
    if not metrics_after or not row.metrics_before:
        return {
            "verdict": "neutral",
            "reason": "No metrics data available",
            "metrics_after": metrics_after,
            "delta_percent": None,
        }

    before = row.metrics_before
    after = metrics_after

    # CPA delta
    cpa_before = before.get("cpa")
    cpa_after = after.get("cpa")
    cpa_delta = None
    if cpa_before and cpa_after and cpa_before > 0:
        cpa_delta = ((cpa_after - cpa_before) / cpa_before) * 100

    # ROAS delta
    roas_before = before.get("roas")
    roas_after = after.get("roas")
    roas_delta = None
    if roas_before and roas_after and roas_before > 0:
        roas_delta = ((roas_after - roas_before) / roas_before) * 100

    # Вердикт
    if cpa_delta is not None and roas_delta is not None:
        if cpa_delta <= -10 and roas_delta >= 15:
            verdict = "success"
            reason = (
                f"CPA: {cpa_before}→{cpa_after} ({cpa_delta:+.1f}%), "
                f"ROAS: {roas_before}→{roas_after} ({roas_delta:+.1f}%)"
            )
        elif cpa_delta >= 15 and roas_delta <= -10:
            verdict = "failure"
            reason = (
                f"CPA: {cpa_before}→{cpa_after} ({cpa_delta:+.1f}%), "
                f"ROAS: {roas_before}→{roas_after} ({roas_delta:+.1f}%)"
            )
        else:
            verdict = "neutral"
            reason = f"CPA Δ {cpa_delta:+.1f}%, ROAS Δ {roas_delta:+.1f}%"
    elif cpa_delta is not None:
        if cpa_delta <= -10:
            verdict = "success"
        elif cpa_delta >= 15:
            verdict = "failure"
        else:
            verdict = "neutral"
        reason = f"CPA Δ {cpa_delta:+.1f}%"
    elif roas_delta is not None:
        if roas_delta >= 15:
            verdict = "success"
        elif roas_delta <= -10:
            verdict = "failure"
        else:
            verdict = "neutral"
        reason = f"ROAS Δ {roas_delta:+.1f}%"
    else:
        verdict = "neutral"
        reason = "No comparable metrics"

    # Основной delta (CPA если есть, иначе ROAS)
    main_delta = cpa_delta if cpa_delta is not None else roas_delta

    return {
        "verdict": verdict,
        "reason": reason,
        "metrics_after": metrics_after,
        "delta_percent": round(main_delta, 1) if main_delta is not None else None,
        "confirmation_id": row.confirmation_id,
        "operation": row.operation,
        "account_id": row.account_id,
        "account_name": row.account_name,
    }


# ── Pattern Extractor ─────────────────────────────────────────────────────────


async def extract_patterns(min_sample: int = 3) -> list[dict]:
    """Извлечь обобщённые паттерны из outcome_log.

    Группирует по operation + platform, считает success_rate.
    Если success_rate > 70% и выборка ≥ min_sample — возвращает как паттерн.

    Вызывается из недельного PatternExtractor cron.
    """
    try:
        async with Session() as sess:
            rows = (
                (
                    await sess.execute(
                        select(OutcomeLog).where(
                            OutcomeLog.verdict.in_(["success", "failure", "neutral"])
                        )
                    )
                )
                .scalars()
                .all()
            )

        if not rows:
            return []

        # Группировка
        from collections import defaultdict

        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for r in rows:
            key = (r.operation, r.platform)
            groups[key].append(
                {
                    "verdict": r.verdict,
                    "account_id": r.account_id,
                    "account_name": r.account_name,
                    "reason": r.reason,
                    "delta_percent": r.delta_percent,
                }
            )

        patterns = []
        for (operation, platform), entries in groups.items():
            total = len(entries)
            if total < min_sample:
                continue

            successes = sum(1 for e in entries if e["verdict"] == "success")
            failures = sum(1 for e in entries if e["verdict"] == "failure")
            success_rate = (successes / total) * 100

            if success_rate >= 70:
                accounts = list({e["account_name"] for e in entries if e["account_name"]})
                avg_delta = (
                    sum(e["delta_percent"] for e in entries if e["delta_percent"] is not None)
                    / total
                    if entries
                    else 0
                )

                patterns.append(
                    {
                        "operation": operation,
                        "platform": platform,
                        "total": total,
                        "successes": successes,
                        "failures": failures,
                        "success_rate": round(success_rate, 1),
                        "avg_delta": round(avg_delta, 1),
                        "accounts": accounts,
                        "memory_rule": (
                            f"outcome:{operation}:{platform}: "
                            f"Успех в {success_rate:.0f}% ({successes}/{total}). "
                            f"Средний Δ: {avg_delta:+.1f}%. "
                            f"Аккаунты: {', '.join(accounts)}"
                        ),
                    }
                )

        return patterns
    except Exception:
        logger.warning("extract_patterns failed", exc_info=True)
        return []
