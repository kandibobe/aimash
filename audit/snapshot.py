"""N1.1: снапшоты health-score → ЛОКАЛЬНАЯ таблица account_health_snapshot (субстрат трендов).

Только агрегаты (score/grade/at_risk/штрафы семей) — без PII/имён кампаний. Идемпотентно per
(customer_id, snapshot_date, period_days), где дата — в ТАЙМЗОНЕ аккаунта (границы дня аккаунта,
не хоста; дату считает bot-слой), а окно аудита в ключе — чтобы /audit 7 не клобберил 30-дневную
базу тренда в тот же день. Запись fail-OPEN: сбой БД НЕ роняет аудит — снапшот побочный продукт.
Дельты трендов сравнимы ТОЛЬКО между снапшотами одной score_model_version И одного period_days
(N1.0a) — иначе честное «н/д», не ложная дельта.

⚠️ Пишем ТОЛЬКО в локальную БД — Google Ads не трогаем (audit/ без ads.mutations/ads.service).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def record_snapshot(result, *, snapshot_date: str, period_days: int) -> bool:
    """Записать/обновить снапшот аккаунта за дату (upsert по (customer_id, snapshot_date,
    period_days)). False → не записан: нет активности/score (снапшотить нечего) ИЛИ сбой БД
    (fail-open)."""
    if not getattr(result, "has_activity", False) or getattr(result, "score", None) is None:
        return False
    try:
        from sqlalchemy import select

        from db.models import AccountHealthSnapshot
        from db.session import Session

        fam_penalty = {
            fam: info.get("penalty", 0.0) for fam, info in (result.families or {}).items()
        }
        async with Session() as s:
            row = (
                await s.execute(
                    select(AccountHealthSnapshot).where(
                        AccountHealthSnapshot.customer_id == str(result.customer_id),
                        AccountHealthSnapshot.snapshot_date == snapshot_date,
                        AccountHealthSnapshot.period_days == int(period_days),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = AccountHealthSnapshot(
                    customer_id=str(result.customer_id),
                    snapshot_date=snapshot_date,
                    period_days=int(period_days),
                )
                s.add(row)
            row.score = int(result.score)
            row.grade = str(result.grade)
            row.total_spend = float(result.total_spend)
            row.at_risk = float(result.at_risk)
            row.currency = str(result.currency or "")
            row.period_days = int(period_days)
            row.family_penalty = fam_penalty
            row.score_model_version = str(getattr(result, "score_model_version", "") or "")
            await s.commit()
        return True
    except Exception as e:  # noqa: BLE001 — fail-open: снапшот не должен ронять аудит
        log.warning("health-снапшот не записан: %s", type(e).__name__)
        return False


async def previous_snapshot(customer_id: str, *, before_date: str, period_days: int):
    """Последний снапшот аккаунта СТРОГО раньше даты и с ТЕМ ЖЕ окном period_days (сравнивать
    7-дневный score с 30-дневным нечестно). None → базы для тренда нет (или сбой БД — fail-open).
    Сверку score_model_version делает вызывающий (bot-слой): разные версии → «н/д»."""
    try:
        from sqlalchemy import select

        from db.models import AccountHealthSnapshot
        from db.session import Session

        async with Session() as s:
            return (
                await s.execute(
                    select(AccountHealthSnapshot)
                    .where(
                        AccountHealthSnapshot.customer_id == str(customer_id),
                        AccountHealthSnapshot.snapshot_date < str(before_date),
                        AccountHealthSnapshot.period_days == int(period_days),
                    )
                    .order_by(AccountHealthSnapshot.snapshot_date.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception as e:  # noqa: BLE001 — тренд не критичен
        log.warning("health-снапшот не прочитан: %s", type(e).__name__)
        return None
