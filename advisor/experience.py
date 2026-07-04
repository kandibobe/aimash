"""Слой B: накопленный опыт как КОД-сигнал для ранжирования (не сырьё для LLM).

Агрегирует обратную связь оператора (👍/👎) и вердикты замеров (improved/worse) ПО kind рекомендации
и отдаёт множитель priority + флаг suppress. Детерминировано (образец keywords.cluster.rank_clusters):
решение принимает КОД, LLM опыт НЕ получает — это отсекает автономность через «обучение» (golden
rule #1). Результат подаётся в advisor.rules.rank_recommendations.
"""

from __future__ import annotations

from sqlalchemy import select

from db.models import Recommendation as RecRow
from db.models import RecommendationFeedback, RecommendationOutcome
from db.session import Session


def _weight(up: int, down: int, improved: int, worse: int) -> tuple[float, bool]:
    """priority-множитель ∈ [0.2, 2.0] и флаг suppress. 👎 и ухудшения тянут вниз, 👍 и улучшения —
    вверх. suppress — когда оператор устойчиво отвергает вид (много 👎 без 👍)."""
    score = 1.0 + 0.15 * (up - down) + 0.25 * (improved - worse)
    weight = max(0.2, min(2.0, round(score, 2)))
    suppress = (down >= 3 and up == 0) or (down - up >= 4)
    return weight, suppress


async def load_experience(chat_id: int, customer_id) -> dict:
    """{kind: {"weight": float, "suppress": bool, "up","down","improved","worse"}} по фидбеку чата +
    вердиктам аккаунта. Пусто (нет истории) → {} (rank_recommendations возьмёт веса 1.0)."""
    async with Session() as s:
        recs = (
            await s.execute(
                select(RecRow.rec_uid, RecRow.kind).where(
                    RecRow.chat_id == int(chat_id),
                    RecRow.customer_id == str(customer_id),
                )
            )
        ).all()
        kind_by_uid = {u: k for u, k in recs}
        if not kind_by_uid:
            return {}
        uids = list(kind_by_uid)
        fbs = (
            await s.execute(
                select(RecommendationFeedback.rec_uid, RecommendationFeedback.rating).where(
                    RecommendationFeedback.rec_uid.in_(uids)
                )
            )
        ).all()
        outs = (
            await s.execute(
                select(RecommendationOutcome.rec_uid, RecommendationOutcome.verdict).where(
                    RecommendationOutcome.rec_uid.in_(uids),
                    RecommendationOutcome.verdict.isnot(None),
                )
            )
        ).all()

    agg: dict[str, dict] = {}

    def _slot(kind: str) -> dict:
        return agg.setdefault(kind, {"up": 0, "down": 0, "improved": 0, "worse": 0})

    for uid, rating in fbs:
        k = kind_by_uid.get(uid)
        if not k:
            continue
        if rating == "up":
            _slot(k)["up"] += 1
        elif rating == "down":
            _slot(k)["down"] += 1
    for uid, v in outs:
        k = kind_by_uid.get(uid)
        if not k:
            continue
        if v == "improved":
            _slot(k)["improved"] += 1
        elif v == "worse":
            _slot(k)["worse"] += 1

    exp: dict[str, dict] = {}
    for kind, a in agg.items():
        w, sup = _weight(a["up"], a["down"], a["improved"], a["worse"])
        exp[kind] = {"weight": w, "suppress": sup, **a}
    return exp
