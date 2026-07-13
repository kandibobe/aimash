"""Слой B: связывание рекомендация → ПРИМЕНЁННАЯ мутация → замер delta метрик.

`link_applied_mutation` — лёгкий хук из bot.main._do_confirm ПОСЛЕ успешного execute_confirmed:
детерминированно матчит применённую мутацию к открытой рекомендации того же чата/аккаунта/операции/
кампании и заводит RecommendationOutcome (ждёт замера). `measure_outcome` — READ-ONLY джоба: читает
метрики кампании ДО/ПОСЛЕ применения, delta+verdict считает КОД. Ни хук, ни джоба НИЧЕГО не мутируют
в Google Ads (golden rule #3) — только локальная БД. Пакет advisor/ не импортирует ads.mutations
(инвариант test_advisor_never_imports_mutations).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from audit.engine import ONE_TAP_OPS
from db.models import Recommendation as RecRow
from db.models import RecommendationOutcome
from db.session import Session

OUTCOME_WINDOW_DAYS = 14  # окно замера ДО/ПОСЛЕ применения (равные окна вокруг apply_date)


def _baseline_from_evidence(ev: dict | None) -> dict:
    """Нормализованный снимок метрик на момент рекомендации {cost, conversions, cpa} из evidence."""
    ev = ev or {}
    cost = float(ev.get("cost", 0) or 0)
    conv = float(ev.get("conversions", 0) or 0)
    cpa = ev.get("cpa")
    if cpa is None:
        cpa = round(cost / conv, 2) if conv else 0.0
    return {"cost": round(cost, 2), "conversions": conv, "cpa": round(float(cpa or 0), 2)}


async def link_applied_mutation(
    chat_id: int,
    operation: str,
    params: dict,
    customer_id: str,
    confirmation_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Найти открытую рекомендацию, к которой относится applied-мутация, и завести outcome.

    Матч ДЕТЕРМИНИРОВАННЫЙ, два уровня:
      1. params['_rec_uid'] — служебный ключ, который bot._advise_apply кладёт в черновик при one-tap
         «применить». ТОЧНАЯ привязка: одну и ту же операцию предлагают РАЗНЫЕ чеки (pause_campaign —
         spend_no_conv и kill_rule; add_negative_keywords — wasteful_keyword и wasteful_search_term),
         поэтому матч только по (operation, campaign) отнёс бы замер в чужой бакет опыта.
      2. Фолбэк (мутация применена вручную командой, а не кнопкой) — ТОЛЬКО для операций из
         ONE_TAP_OPS: chat_id + customer_id + suggested_operation == operation + target_campaign ==
         params['campaign'], самая свежая.
    Почему фолбэк НЕ работает для денежных операций (update_bid/update_budget/create_rsa): у находок
    аудита это advice_operation — МЕТКА, а не совет нажать кнопку (её и нет, rule #3). Пользователь,
    который своей командой поднял бюджет кампании, мог сделать это ВОПРЕКИ совету («перераспредели») —
    матч по (operation, campaign) слеп к направлению и завёл бы замер чужого действия на бакет опыта,
    вплоть до suppress живого совета по шуму. Такие операции связываются ИСКЛЮЧИТЕЛЬНО по rec_uid.
    Найденная строка обязана принадлежать ТОМУ ЖЕ чату и аккаунту (иначе чужой rec_uid из подложенных
    params завёл бы outcome на чужую рекомендацию). Дубль по confirmation_id не создаём. Ничего не
    найдено → False (тихо). Только локальная БД — Google Ads не трогаем."""
    campaign = (params or {}).get("campaign")
    if not campaign or not operation:
        return False
    rec_uid = (params or {}).get("_rec_uid")
    now = now or datetime.now(timezone.utc)
    async with Session() as s:
        dup = (
            await s.execute(
                select(RecommendationOutcome).where(
                    RecommendationOutcome.confirmation_id == confirmation_id
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            return False
        q = select(RecRow).where(
            RecRow.chat_id == int(chat_id),
            RecRow.customer_id == str(customer_id),
        )
        if rec_uid:
            q = q.where(RecRow.rec_uid == str(rec_uid))
        elif operation in ONE_TAP_OPS:
            q = q.where(
                RecRow.suggested_operation == operation,
                RecRow.target_campaign == campaign,
            )
        else:
            return False
        rec = (await s.execute(q.order_by(RecRow.id.desc()).limit(1))).scalar_one_or_none()
        if rec is None:
            return False
        s.add(
            RecommendationOutcome(
                rec_uid=rec.rec_uid,
                confirmation_id=confirmation_id,
                customer_id=str(customer_id),
                target_campaign=campaign,
                apply_date=now,
                baseline=_baseline_from_evidence(rec.evidence),
                measure_after=now + timedelta(days=OUTCOME_WINDOW_DAYS),
            )
        )
        await s.commit()
    return True


async def due_outcomes(*, now: datetime | None = None, limit: int = 50) -> list:
    """Outcome-строки, у которых наступил measure_after и ещё не замерены (measured_at IS NULL)."""
    now = now or datetime.now(timezone.utc)
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(RecommendationOutcome)
                    .where(
                        RecommendationOutcome.measured_at.is_(None),
                        RecommendationOutcome.measure_after <= now,
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


def _campaign_metrics(rows, name: str) -> dict | None:
    """Метрики кампании по имени из разбивки fetch_by_campaign ([((name, status), Metrics), …])."""
    for (n, _status), m in rows or []:
        if n == name:
            return {
                "cost": round(m.cost, 2),
                "conversions": float(m.conversions),
                "cpa": round(m.cpa, 2),
            }
    return None


def verdict(before: dict | None, after: dict | None) -> str:
    """Вердикт замера (КОД, детерминированно): improved | worse | neutral. Нет данных → neutral.

    КОНВЕРСИИ — первичный сигнал цели, оцениваются РАНЬШЕ CPA (иначе падение CPA маскировало бы
    обвал конверсий: напр. conv 10→2 при CPA 100→85 ошибочно читалось бы как 'improved' и усиливало
    вредный совет через experience). Только при РАВНЫХ конверсиях решает CPA (упал ≥10% → improved,
    вырос ≥20% → worse)."""
    if not before or not after:
        return "neutral"
    if after["conversions"] > before["conversions"]:
        return "improved"
    if after["conversions"] < before["conversions"]:
        return "worse"
    # Конверсии равны → судим по стоимости конверсии (оба CPA известны и ненулевые).
    if before["cpa"] and after["cpa"]:
        if after["cpa"] <= before["cpa"] * 0.9:
            return "improved"
        if after["cpa"] >= before["cpa"] * 1.2:
            return "worse"
    return "neutral"


async def measure_outcome(outcome, client, *, now: datetime | None = None) -> str:
    """READ-ONLY: метрики кампании ДО/ПОСЛЕ применения → verdict (КОД). Пишет followup/delta/verdict/
    measured_at. Кампания не найдена/сбой чтения → verdict='neutral', но measured (не зацикливаемся).
    Ничего не мутирует в Google Ads (golden rule #3)."""
    from core.resilience import run_ads_read_call
    from reports.period import custom
    from reports.queries import fetch_by_campaign

    now = now or datetime.now(timezone.utc)
    ad = (
        outcome.apply_date.date()
        if outcome.apply_date
        else (now.date() - timedelta(days=OUTCOME_WINDOW_DAYS))
    )
    before_p = custom(ad - timedelta(days=OUTCOME_WINDOW_DAYS), ad - timedelta(days=1))
    after_p = custom(ad + timedelta(days=1), ad + timedelta(days=OUTCOME_WINDOW_DAYS))
    before = after = None
    try:
        bd_b = await run_ads_read_call(
            fetch_by_campaign, client, outcome.customer_id, before_p, label="advise_before"
        )
        bd_a = await run_ads_read_call(
            fetch_by_campaign, client, outcome.customer_id, after_p, label="advise_after"
        )
        before = _campaign_metrics(bd_b.rows, outcome.target_campaign)
        after = _campaign_metrics(bd_a.rows, outcome.target_campaign)
    except Exception:  # noqa: BLE001 — сбой чтения → neutral+measured (не зацикливаемся на строке)
        before = after = None
    v = verdict(before, after)
    async with Session() as s:
        row = (
            await s.execute(
                select(RecommendationOutcome).where(RecommendationOutcome.id == outcome.id)
            )
        ).scalar_one_or_none()
        if row is not None:
            row.followup = after or {}
            row.delta = {"before": before or {}, "after": after or {}}
            row.verdict = v
            row.measured_at = now
            await s.commit()
    return v
