"""advisor 1.4 (аудит 2026-07-06): «🙈 Скрыть» (dismissed) + пер-кампанийная усталость.

Инварианты: dismiss пишет ТОЛЬКО status='dismissed' своей строки (чужой chat_id — отказ; Proposal/
AuditLog не растут); dismissed — слабый негатив (слабее 👎) и суппрессит лишь при устойчивом
игноре; suppress по (kind, campaign) гасит совет ТОЛЬКО по своей кампании; money-floor guard
(SUPPRESS_MONEY_FLOOR) держится на ОБОИХ уровнях опыта.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from advisor import experience, store  # noqa: E402
from advisor.rules import SUPPRESS_MONEY_FLOOR, Recommendation, rank_recommendations  # noqa: E402


def _rec(kind="spend_no_conv", camp="Camp A", cost=10.0) -> Recommendation:
    return Recommendation(
        kind=kind,
        topic="optimize",
        severity="warning",
        target_campaign=camp,
        facts={},
        evidence={"cost": cost},
        body="x",
    )


# ── чистая логика _weight ─────────────────────────────────────────────────────────────
def test_weight_dismissed_weaker_than_down():
    w_down, _ = experience._weight(0, 1, 0, 0)
    w_dis, _ = experience._weight(0, 0, 0, 0, dismissed=1)
    assert w_dis > w_down  # 🙈 тянет вниз слабее, чем осознанное 👎


def test_weight_dismiss_suppresses_only_on_sustained_ignore():
    _, sup4 = experience._weight(0, 0, 0, 0, dismissed=4)
    _, sup5 = experience._weight(0, 0, 0, 0, dismissed=5)
    _, sup5_up = experience._weight(1, 0, 0, 0, dismissed=5)
    assert not sup4 and sup5 and not sup5_up  # ≥5 🙈 без единого 👍


# ── rank: пер-кампанийный suppress и money-floor ──────────────────────────────────────
def test_per_campaign_suppress_hides_only_own_campaign():
    exp = {("spend_no_conv", "Camp A"): {"weight": 0.2, "suppress": True}}
    ranked = rank_recommendations([_rec(camp="Camp A"), _rec(camp="Camp B")], exp)
    camps = [r.target_campaign for r in ranked]
    assert camps == ["Camp B"]  # Camp A погашена, Camp B того же вида — жива


def test_money_floor_guard_holds_for_campaign_level():
    exp = {("spend_no_conv", "Camp A"): {"weight": 0.2, "suppress": True}}
    big = _rec(camp="Camp A", cost=SUPPRESS_MONEY_FLOOR + 1)
    ranked = rank_recommendations([big], exp)
    assert ranked  # крупный слив не прячется даже при пер-кампанийной усталости


def test_kind_and_campaign_weights_multiply_clamped():
    exp = {
        "spend_no_conv": {"weight": 0.5, "suppress": False},
        ("spend_no_conv", "Camp A"): {"weight": 0.5, "suppress": False},
    }
    (only,) = rank_recommendations([_rec(camp="Camp A", cost=0.0)], exp)
    # base = severity(warning) * (1+0) = вес severity; множитель 0.5*0.5=0.25 (в клампе [0.2, 2.0])
    (base,) = rank_recommendations([_rec(camp="Camp A", cost=0.0)], None)
    assert abs(only.priority - round(base.priority * 0.25, 2)) < 0.02


# ── dismiss: только своя строка, только status, без proposal ──────────────────────────
async def test_dismiss_sets_status_and_feeds_experience():
    from sqlalchemy import func, select

    from db.models import AuditLog, Proposal
    from db.models import Recommendation as RecRow
    from db.session import Session, init_db

    await init_db()
    chat, customer = 6001, "7753643025"
    r = _rec(camp="Camp D")
    (uid,) = await store.record_recommendations(chat, customer, [r], "advise")

    async with Session() as s:
        p0 = (await s.execute(select(func.count()).select_from(Proposal))).scalar()
        a0 = (await s.execute(select(func.count()).select_from(AuditLog))).scalar()

    assert await store.dismiss_recommendation(uid, chat) is True
    assert await store.dismiss_recommendation(uid, chat_id=999999) is False  # чужой чат — отказ

    async with Session() as s:
        row = (await s.execute(select(RecRow).where(RecRow.rec_uid == uid))).scalar_one()
        assert row.status == "dismissed"
        p1 = (await s.execute(select(func.count()).select_from(Proposal))).scalar()
        a1 = (await s.execute(select(func.count()).select_from(AuditLog))).scalar()
    assert (p0, a0) == (p1, a1)  # ни proposal, ни audit-строк — только локальный status

    exp = await experience.load_experience(chat, customer)
    assert exp["spend_no_conv"]["dismissed"] == 1  # уровень kind
    assert exp[("spend_no_conv", "Camp D")]["dismissed"] == 1  # уровень кампании


async def test_dismiss_unknown_uid_returns_false():
    from db.session import init_db

    await init_db()
    assert await store.dismiss_recommendation("нет такого uid", 6002) is False
