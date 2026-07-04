"""CRUD таблиц advisor: recommendation / recommendation_feedback / recommendation_outcome.

Только ЛОКАЛЬНАЯ БД — ни одна функция не трогает Google Ads и не создаёт proposal (golden rule
#1/#3). Персист рекомендаций назначает rec_uid (hex uuid); фидбек — один голос-тогл на оператора.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from db.models import Recommendation as RecRow
from db.models import RecommendationFeedback
from db.session import Session


async def record_recommendations(chat_id, customer_id, recs, source: str = "advise") -> list[str]:
    """Персист показанных рекомендаций. Каждой назначается rec_uid (hex uuid) — он же проставляется
    в rec.rec_uid (для рендера кнопок 👍/👎). Возвращает список rec_uid в порядке recs. body берётся
    из rec.body (service рендерит текст до персиста). НЕ создаёт proposal."""
    uids: list[str] = []
    async with Session() as s:
        for r in recs:
            uid = uuid4().hex
            r.rec_uid = uid
            s.add(
                RecRow(
                    rec_uid=uid,
                    chat_id=int(chat_id),
                    customer_id=str(customer_id),
                    topic=r.topic,
                    kind=r.kind,
                    severity=r.severity,
                    target_campaign=r.target_campaign,
                    suggested_operation=r.suggested_operation,
                    priority=float(r.priority),
                    evidence=r.evidence or {},
                    body=r.body or "",
                    source=source,
                    status="shown",
                )
            )
            uids.append(uid)
        await s.commit()
    return uids


async def record_feedback(
    rec_uid: str,
    chat_id: int,
    rating: str,
    *,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
) -> None:
    """Записать 👍/👎 (rating='up'|'down'). Один голос-тогл на (rec_uid, chat_id) — повтор ОБНОВЛЯЕТ.
    Пишет ТОЛЬКО в recommendation_feedback — Google Ads/proposal не трогает (инвариант test_advisor)."""
    async with Session() as s:
        existing = (
            await s.execute(
                select(RecommendationFeedback).where(
                    RecommendationFeedback.rec_uid == rec_uid,
                    RecommendationFeedback.chat_id == int(chat_id),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.rating = rating
            existing.actor_user_id = actor_user_id
            existing.actor_username = actor_username
        else:
            s.add(
                RecommendationFeedback(
                    rec_uid=rec_uid,
                    chat_id=int(chat_id),
                    rating=rating,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
            )
        await s.commit()


async def get_recommendation(rec_uid: str) -> RecRow | None:
    """Строка рекомендации по rec_uid (для outcome-связывания в Слое B). None — нет такой."""
    async with Session() as s:
        return (
            await s.execute(select(RecRow).where(RecRow.rec_uid == rec_uid))
        ).scalar_one_or_none()
