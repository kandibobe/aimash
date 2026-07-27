"""Shadow Mode — теневое тестирование алгоритмов на исторических данных.

Проблема: прежде чем доверить агенту реальные деньги, нужно понять, насколько
его решения эффективны. Тестирование в production на живых кампаниях — unacceptable risk.

Решение: раз в день cron-джоба скармливает агенту срез ВЧЕРАШНИХ данных по кампаниям.
Агент генерирует решения (в DRY_RUN=true), НЕ выполняя их. Результат сравнивается
с тем, что случилось на самом деле:
  - «Агент предложил снизить CPA на X% — помогло бы это реальным метрикам?»
  - «Агент предложил поднять бюджет — окупилось бы?»

Результат: ежедневный отчёт в #approvals-and-audits.

Использование (cron):
  Ежедневно в 00:00 MSK: python -m scheduler.shadow_mode
  Или через Hermes cronjob с prompt: "Запусти shadow mode аудит"

Архитектура:
  1. Собрать вчерашние метрики по всем боевым аккаунтам
  2. Сгенерировать рекомендации (DRY_RUN)
  3. Сравнить с реальностью (сегодня минус вчера)
  4. Отчёт: accuracy, false positives, потенциальная экономия
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from core.context import request_scope


@dataclass
class ShadowRecommendation:
    """Одна рекомендация агента в shadow mode."""

    account: str
    campaign_id: str
    campaign_name: str
    operation: str  # "budget_change", "pause", "bid_adjust", etc.
    params: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""  # почему агент это предложил


@dataclass
class ShadowOutcome:
    """Сравнение рекомендации с реальностью."""

    recommendation: ShadowRecommendation
    would_help: bool | None = None  # True/False/None (нельзя определить)
    actual_cpa_change: float = 0.0  # как изменился CPA на самом деле (%)
    actual_cost_change: float = 0.0  # как изменился расход (%)
    verdict: str = ""  # человекочитаемый вердикт


@dataclass
class ShadowReport:
    """Итоговый отчёт теневого тестирования."""

    date: str
    accounts_checked: int
    campaigns_checked: int
    recommendations_total: int
    would_help_count: int
    would_hurt_count: int
    undetermined_count: int
    outcomes: list[ShadowOutcome] = field(default_factory=list)
    summary: str = ""


# ── Основной пайплайн ────────────────────────────────────────────────────


def collect_historical_metrics(
    account: str, target_date: date
) -> dict[str, Any]:
    """Собрать метрики по кампаниям аккаунта за конкретную дату.

    Заглушка фазы 1 — возвращает структуру, которую реальный сборщик
    наполнит через MCP: get_campaign_stats(account, date_from=target_date,
    date_to=target_date).
    """
    date_str = target_date.isoformat()
    return {
        "account": account,
        "date": date_str,
        "currency": "???",
        "campaigns": [],  # список campaign_stats
        "total_cost": 0.0,
        "total_conversions": 0.0,
    }


async def run_shadow_cycle(
    account: str,
    target_date: date | None = None,
    *,
    dry_run: bool = True,
) -> ShadowReport:
    """Выполнить один цикл теневого тестирования для аккаунта.

    Args:
        account: ID аккаунта Google Ads
        target_date: дата для анализа (по умолчанию — вчера)
        dry_run: если True, рекомендации НЕ выполняются

    Returns:
        ShadowReport с результатами сравнения
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    with request_scope("shadow_mode", customer_id=account):
        # 1. Собрать исторические метрики
        metrics = collect_historical_metrics(account, target_date)

        # 2. Сгенерировать рекомендации (через MCP-инструменты)
        recommendations = await _generate_recommendations(metrics)

        # 3. Сравнить с реальными сегодняшними метриками
        outcomes = await _compare_with_reality(recommendations, account, target_date)

        # 4. Построить отчёт
        return _build_report(account, target_date, outcomes)


async def _generate_recommendations(
    metrics: dict[str, Any],
) -> list[ShadowRecommendation]:
    """Сгенерировать рекомендации на основе метрик.

    Фаза 1: вызывает get_account_audit через MCP (инструмент уже существует).
    Агент (Hermes/Claude) анализирует находки аудита и формирует рекомендации.

    Заглушка: возвращает пустой список — реальная логика живёт
    в промпте агента при вызове через cron.
    """
    # Реальная имплементация:
    #   from mcp_server.tools_read import get_account_audit
    #   audit = await get_account_audit(account=metrics["account"], ...)
    #   findings = audit["rows"]
    #   recommendations = [_finding_to_recommendation(f) for f in findings]
    return []


async def _compare_with_reality(
    recommendations: list[ShadowRecommendation],
    account: str,
    target_date: date,
) -> list[ShadowOutcome]:
    """Сравнить рекомендации с реальными метриками за сегодня.

    Для каждой рекомендации:
      - Смотрим, изменились ли метрики в предсказанном направлении
      - Выставляем would_help/would_hurt/undetermined
    """
    outcomes: list[ShadowOutcome] = []

    today = date.today()
    # today_metrics = await get_campaign_stats(account, date_from=today, ...)

    for rec in recommendations:
        # Заглушка: сравниваем по эвристике
        outcome = ShadowOutcome(
            recommendation=rec,
            would_help=None,
            actual_cpa_change=0.0,
            actual_cost_change=0.0,
            verdict="Недостаточно данных для сравнения",
        )
        outcomes.append(outcome)

    return outcomes


def _build_report(
    account: str, target_date: date, outcomes: list[ShadowOutcome]
) -> ShadowReport:
    """Построить итоговый отчёт."""
    would_help = sum(1 for o in outcomes if o.would_help is True)
    would_hurt = sum(1 for o in outcomes if o.would_help is False)
    undetermined = sum(1 for o in outcomes if o.would_help is None)

    # Человекочитаемое саммари
    lines = [
        f"📊 Shadow Mode: {account}",
        f"⏱️ Анализ за {target_date.isoformat()}",
        f"Всего рекомендаций: {len(outcomes)}",
        f"✅ Помогло бы: {would_help}",
        f"❌ Ухудшило бы: {would_hurt}",
        f"❓ Не определено: {undetermined}",
        "",
    ]

    for o in outcomes:
        icon = "✅" if o.would_help else ("❌" if o.would_help is False else "❓")
        lines.append(
            f"{icon} {o.recommendation.operation} "
            f"({o.recommendation.campaign_name}): {o.verdict}"
        )

    report = ShadowReport(
        date=target_date.isoformat(),
        accounts_checked=1,
        campaigns_checked=len(outcomes),
        recommendations_total=len(outcomes),
        would_help_count=would_help,
        would_hurt_count=would_hurt,
        undetermined_count=undetermined,
        outcomes=outcomes,
        summary="\n".join(lines),
    )

    return report


def format_for_telegram(report: ShadowReport) -> str:
    """Форматировать отчёт для отправки в Telegram."""
    return report.summary


# ── CLI entry point ───────────────────────────────────────────────────────


async def main() -> None:
    """Точка входа для cron-джобы: python -m scheduler.shadow_mode."""
    from core.config import settings

    active_accounts = settings.active_customer_ids or [
        "6764040266",  # Aimash
        "7990205915",  # Irisboutique
        "8325477566",  # Rozowy Słoń
        "9889330611",  # Art Or
    ]

    all_outcomes: list[ShadowOutcome] = []
    yesterday = date.today() - timedelta(days=1)

    for account in active_accounts:
        try:
            report = await run_shadow_cycle(account, target_date=yesterday)
            all_outcomes.extend(report.outcomes)
            print(format_for_telegram(report))
            print("---")
        except Exception as e:
            print(f"❌ Shadow mode failed for {account}: {type(e).__name__}")

    # Итоговое саммари
    would_help = sum(1 for o in all_outcomes if o.would_help is True)
    would_hurt = sum(1 for o in all_outcomes if o.would_help is False)
    print(f"\n📊 Итого: {len(all_outcomes)} рекомендаций | ✅{would_help} ❌{would_hurt}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())