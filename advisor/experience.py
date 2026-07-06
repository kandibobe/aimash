"""Слой B: накопленный опыт как КОД-сигнал для ранжирования (не сырьё для LLM).

Агрегирует обратную связь оператора (👍/👎), вердикты замеров (improved/worse) и «показано и
проигнорировано» (status='dismissed', кнопка 🙈) ДВУМЯ уровнями: по kind (account-wide, как раньше)
и по (kind, target_campaign) — пер-кампанийная усталость: совет, который менеджер повторно
отклоняет по КОНКРЕТНОЙ кампании, гаснет быстрее, НЕ глуша тот же вид на других кампаниях.
Отдаёт множитель priority + флаг suppress. Детерминировано (образец keywords.cluster.rank_clusters):
решение принимает КОД, LLM опыт НЕ получает — это отсекает автономность через «обучение» (golden
rule #1). Результат подаётся в advisor.rules.rank_recommendations.
"""

from __future__ import annotations

from sqlalchemy import select

from db.models import Recommendation as RecRow
from db.models import RecommendationFeedback, RecommendationOutcome
from db.session import Session

# Ключ второго уровня — tuple (kind, target_campaign); первый уровень — str kind (обратная
# совместимость: старые вызовы exp.get(r.kind) продолжают работать).
ExpKey = str | tuple[str, str]


def _weight(
    up: int, down: int, improved: int, worse: int, dismissed: int = 0
) -> tuple[float, bool]:
    """priority-множитель ∈ [0.2, 2.0] и флаг suppress. 👎 и ухудшения тянут вниз, 👍 и улучшения —
    вверх; dismissed (🙈) — слабый негатив (в 3 раза слабее 👎: игнор ≠ осознанное «мимо»).
    suppress — оператор устойчиво отвергает вид (много 👎 без 👍) ЛИБО устойчиво игнорирует
    (≥5 🙈 без единого 👍)."""
    score = 1.0 + 0.15 * (up - down) + 0.25 * (improved - worse) - 0.05 * dismissed
    weight = max(0.2, min(2.0, round(score, 2)))
    suppress = (down >= 3 and up == 0) or (down - up >= 4) or (dismissed >= 5 and up == 0)
    return weight, suppress


async def load_experience(chat_id: int, customer_id) -> dict:
    """{kind | (kind, campaign): {"weight","suppress","up","down","improved","worse","dismissed"}}
    по фидбеку чата + вердиктам аккаунта + скрытым карточкам. Пусто (нет истории) → {}
    (rank_recommendations возьмёт веса 1.0)."""
    async with Session() as s:
        recs = (
            await s.execute(
                select(RecRow.rec_uid, RecRow.kind, RecRow.target_campaign, RecRow.status).where(
                    RecRow.chat_id == int(chat_id),
                    RecRow.customer_id == str(customer_id),
                )
            )
        ).all()
        info_by_uid = {u: (k, c or "") for u, k, c, _st in recs}
        if not info_by_uid:
            return {}
        uids = list(info_by_uid)
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

    agg: dict[ExpKey, dict] = {}

    def _slot(key: ExpKey) -> dict:
        return agg.setdefault(key, {"up": 0, "down": 0, "improved": 0, "worse": 0, "dismissed": 0})

    def _bump(uid: str, field: str) -> None:
        info = info_by_uid.get(uid)
        if not info:
            return
        kind, campaign = info
        _slot(kind)[field] += 1  # уровень 1: account-wide по kind (как раньше)
        if campaign:
            _slot((kind, campaign))[field] += 1  # уровень 2: пер-кампанийная усталость

    for uid, rating in fbs:
        if rating == "up":
            _bump(uid, "up")
        elif rating == "down":
            _bump(uid, "down")
    for uid, v in outs:
        if v == "improved":
            _bump(uid, "improved")
        elif v == "worse":
            _bump(uid, "worse")
    for u, _kind, _camp, st in recs:  # dismissed — прямо из строк recs (status уже выбран)
        if st == "dismissed":
            _bump(u, "dismissed")

    exp: dict[ExpKey, dict] = {}
    for key, a in agg.items():
        w, sup = _weight(a["up"], a["down"], a["improved"], a["worse"], a["dismissed"])
        exp[key] = {"weight": w, "suppress": sup, **a}
    return exp
