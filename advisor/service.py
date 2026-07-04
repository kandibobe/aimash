"""Orchestration рекомендаций (образец scheduler/jobs.py) — READ-ONLY.

Собирает отчёт (reports.service) + аномалии (scheduler.anomaly), прогоняет чистые правила
(advisor.rules), формулирует (advisor.formulate → fallback render) и персистит показанные
рекомендации (advisor.store). НЕ импортирует ads.mutations/ads.service и НЕ создаёт proposal —
исполнение любого совета идёт ОТДЕЛЬНОЙ командой через confirm-гейт (golden rule #1/#3).

Проактивный путь (scheduler) передаёт уже собранный `report` → лишних чтений нет.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from advisor import formulate, render, store
from advisor.rules import Recommendation, build_candidates, rank_recommendations

MAX_RECS = 5  # сколько рекомендаций показываем (топ по приоритету), анти-спам


@dataclass
class RecommendationSet:
    account: str
    period_label: str
    currency: str
    recs: list[Recommendation] = field(default_factory=list)


def _period_label(report) -> str:
    p = getattr(report, "period", None)
    if p is None:
        return ""
    df, dt = getattr(p, "date_from", ""), getattr(p, "date_to", "")
    return f"{df} — {dt}" if df and dt else (getattr(p, "label", "") or "")


def _alerts_from_report(report) -> list:
    """Аномалии из уже прочитанного отчёта (без доп. чтений). Нет предыдущего периода → []."""
    if getattr(report, "prev_totals", None) is None:
        return []
    from scheduler.anomaly import detect_anomalies

    return detect_anomalies(
        report.totals, report.prev_totals, currency=getattr(report, "currency", "")
    )


async def _gather_report(customer_id, period_days: int):
    """Собрать отчёт с предыдущим периодом + валюта (READ-ONLY, замок чтения внутри). Образец
    scheduler.jobs.run_scheduled_report: клиент per-account, currency best-effort, run_ads_read_call."""
    from ads.client import build_client_async
    from ads.read import account_currency
    from core.resilience import run_ads_read_call
    from reports.period import last_n_days
    from reports.service import build_account_report_async

    period = last_n_days(int(period_days))
    client = await build_client_async(str(customer_id))
    currency = ""
    try:
        currency = await run_ads_read_call(
            account_currency, client, str(customer_id), label="advise_cur"
        )
    except Exception:  # noqa: BLE001 — валюта необязательна (показываем метрики без кода валюты)
        currency = ""
    return await build_account_report_async(client, str(customer_id), period, currency=currency)


async def build_recommendations(
    chat_id: int,
    customer_id,
    *,
    period_days: int = 30,
    source: str = "advise",
    report=None,
    alerts=None,
    topics=None,
    lang: str | None = None,
    thresholds: dict | None = None,
    persist: bool = True,
    use_llm: bool = True,
    use_experience: bool = True,
) -> RecommendationSet:
    """Построить рекомендации по аккаунту. `report` не задан → собрать live (READ-ONLY). Кандидаты
    выбирает и ранжирует КОД (advisor.rules); опыт (Слой B) — КОД-множитель priority/suppress,
    подаётся в rank (не в LLM). Текст — advisory-LLM (use_llm) или детерминированный render.
    Персистит показанные (store) — но НИКОГДА не создаёт proposal. Возвращает RecommendationSet
    с recs, у которых проставлены rec_uid + body (для кнопок 👍/👎)."""
    from bot import i18n

    lang = lang or i18n.get_lang(chat_id)
    if report is None:
        report = await _gather_report(customer_id, period_days)
    if alerts is None:
        alerts = _alerts_from_report(report)

    experience: dict = {}
    if use_experience:
        try:  # Слой B: накопленный опыт как КОД-сигнал (образец rank_clusters); сбой → веса 1.0
            from advisor.experience import load_experience

            experience = await load_experience(chat_id, customer_id)
        except Exception:  # noqa: BLE001 — опыт не критичен, ранжируем без него
            experience = {}

    cands = rank_recommendations(
        build_candidates(report, alerts, thresholds=thresholds, topics=topics),
        experience=experience,
    )[:MAX_RECS]

    if use_llm:
        bodies = await formulate.phrase(cands, lang)
    else:
        bodies = [render.render_recommendation(r, lang) for r in cands]
    for r, b in zip(cands, bodies):
        r.body = b

    if persist and cands:
        await store.record_recommendations(chat_id, customer_id, cands, source)

    return RecommendationSet(
        account=str(customer_id),
        period_label=_period_label(report),
        currency=getattr(report, "currency", "") or "",
        recs=cands,
    )
