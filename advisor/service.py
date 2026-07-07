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
    extras: list[str] = field(default_factory=list)  # advisory-довески (напр. LLM-минус-слова, #3)
    has_activity: bool = (
        False  # была ли активность в периоде (расход/показы) — для внятного empty-state
    )


def _keyword_texts(report, limit: int = 40) -> list[str]:
    """Тексты ключей из разбивки keyword_view (топ по расходу) — для advisory LLM-минус-слов (#3)."""
    kb = next((b for b in getattr(report, "breakdowns", []) if b.key == "keyword"), None)
    if not kb or not kb.rows:
        return []
    rows = sorted(kb.rows, key=lambda r: -getattr(r[1], "cost_micros", 0))
    out: list[str] = []
    for dims, _m in rows[:limit]:
        if len(dims) >= 3 and dims[2]:
            out.append(str(dims[2]))
    return out


async def _client_profile_context(customer_id) -> str:
    """2.11 (§20.6): компактный профиль клиента как КОНТЕКСТ advisor-LLM (тон/терминология ниши,
    «не минусуй бренд»). Best-effort: нет профиля/сбой БД → '' (advisor работает как раньше).
    Решения по-прежнему принимает КОД (rules) — профиль в детекторы НЕ подаётся."""
    try:
        from clients.store import ClientStore

        return await ClientStore().profile_context_text(str(customer_id), max_chars=600)
    except Exception:  # noqa: BLE001 — контекст-косметика
        return ""


async def _negative_keywords_extra(
    report, topics, lang: str, client_context: str = ""
) -> list[str]:
    """#3: LLM-предложение минус-слов по ключам аккаунта (advisory, ничего не добавляет — как §7).
    Только для темы keywords и при наличии ключей. Сбой/пусто → [] (не роняем /advise).
    client_context (2.11) — бренд/услуги клиента: тематика точнее, брендовые слова не минусуются."""
    if topics and "keywords" not in topics:
        return []
    kws = _keyword_texts(report)
    if not kws:
        return []
    try:
        from bot import i18n
        from keywords.cluster import suggest_negative_keywords

        negs = await suggest_negative_keywords(
            client_context[:400],
            kws,
            language=(lang if lang in ("ru", "uk", "en") else "ru"),
            limit=12,
        )
        if not negs:
            return []
        return [i18n.t("advise_negatives_hint", lang, words=", ".join(negs))]
    except Exception:  # noqa: BLE001 — advisory-довесок, не критичен
        return []


def _has_activity(report) -> bool:
    """Была ли реальная активность в периоде (расход/показы/клики). Пустой Draft/тест-аккаунт →
    False → внятный empty-state («нет данных, выбери живой аккаунт»), а не «советов нет»."""
    t = getattr(report, "totals", None)
    if t is None:
        return False
    return bool(
        getattr(t, "cost_micros", 0) or getattr(t, "impressions", 0) or getattr(t, "clicks", 0)
    )


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
        thresholds=thresholds,  # 2.6: per-chat suppress_money_floor (валюта аккаунта, без FX)
    )[:MAX_RECS]

    if use_llm:
        # 2.11 (§20.6): профиль клиента — контекст формулировок (тон/термины ниши); решения — в КОДЕ.
        client_ctx = await _client_profile_context(customer_id)
        bodies = await formulate.phrase(cands, lang, client_context=client_ctx)
    else:
        client_ctx = ""
        bodies = [render.render_recommendation(r, lang) for r in cands]
    for r, b in zip(cands, bodies):
        r.body = b

    if persist and cands:
        await store.record_recommendations(chat_id, customer_id, cands, source)

    # #3: advisory LLM-минус-слова (тема keywords) — только на интерактивном пути (use_llm).
    extras = (
        await _negative_keywords_extra(report, topics, lang, client_context=client_ctx)
        if use_llm
        else []
    )

    return RecommendationSet(
        account=str(customer_id),
        period_label=_period_label(report),
        currency=getattr(report, "currency", "") or "",
        recs=cands,
        extras=extras,
        has_activity=_has_activity(report),
    )
