"""1.5 (аудит 2026-07-06): «утренний экран действий» — проактивный дайджест advisor с кнопками.

Инварианты: кросс-аккаунтный Top-N БЕЗ FX (безразмерная доля расхода под риском); каждая карточка —
отдельным сообщением с reply_markup (👍/👎/🙈/apply — тот же advise_feedback_kb, что /advise);
proposal НЕ создаётся (кнопка apply лишь стартует confirm-гейт по тапу); персистятся ТОЛЬКО
показанные Top-N (source='scheduler'); отчёт per-account собирается один раз на прогон; между
сообщениями — пауза (flood-limits).
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from advisor.rules import Recommendation, rank_cross_account  # noqa: E402


def _rec(kind="spend_no_conv", camp="Camp", cost=10.0, severity="warning") -> Recommendation:
    return Recommendation(
        kind=kind,
        topic="optimize",
        severity=severity,
        target_campaign=camp,
        suggested_operation="pause_campaign",
        facts={},
        evidence={"cost": cost},
        body=f"совет по {camp}",
    )


# ── rank_cross_account: честное кросс-валютное ранжирование БЕЗ FX ────────────────────
def test_rank_cross_account_relative_share_no_fx():
    """UAH-совет на 4 000 при расходе 100 000 (4%) НИЖЕ USD-совета на 50 при расходе 60 (83%) —
    сравниваются ДОЛИ, а не суммы (курсы не выдумываем, golden rule #4)."""
    uah = ("111", "UAH", 100_000.0, _rec(camp="UAH-camp", cost=4_000.0))
    usd = ("222", "USD", 60.0, _rec(camp="USD-camp", cost=50.0))
    top = rank_cross_account([uah, usd], top_n=2)
    assert [it[3].target_campaign for it in top] == ["USD-camp", "UAH-camp"]


def test_rank_cross_account_zero_magnitude_goes_to_tail():
    money = ("111", "USD", 100.0, _rec(camp="Money", cost=50.0))
    nomoney = ("111", "USD", 100.0, _rec(camp="NoMoney", cost=0.0, severity="critical"))
    top = rank_cross_account([nomoney, money], top_n=2)
    assert top[0][3].target_campaign == "Money"  # деньги-под-риском важнее severity без денег


def test_rank_cross_account_deterministic_and_caps():
    items = [("1", "USD", 100.0, _rec(camp=f"C{i}", cost=10.0 + i)) for i in range(7)]
    a = rank_cross_account(items, top_n=3)
    b = rank_cross_account(list(reversed(items)), top_n=3)
    assert [x[3].target_campaign for x in a] == [x[3].target_campaign for x in b]
    assert len(a) == 3


# ── джоба: карточки с кнопками, без proposal, персист только Top-N ────────────────────
async def test_digest_sends_cards_with_buttons_and_no_proposal(monkeypatch):
    from sqlalchemy import delete, func, select

    import advisor.service as advisor_service
    from db.models import Proposal
    from db.models import Recommendation as RecRow
    from db.session import Session, init_db
    from scheduler import jobs

    await init_db()
    chat = 6101
    A, B = "1112223334", "2223334445"
    async with Session() as s:  # идемпотентность на общем temp-SQLite
        await s.execute(delete(RecRow).where(RecRow.chat_id == chat))
        await s.commit()

    async def _arecipients():
        return {chat}

    async def _achats(_recipients):
        return {chat}

    monkeypatch.setattr(jobs, "_recipients", _arecipients)
    monkeypatch.setattr(jobs, "_advise_proactive_chats", _achats)
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [A, B])

    gathered: list[str] = []

    async def _fake_gather(acct, _days):
        gathered.append(str(acct))
        total = 100.0 if acct == A else 1000.0
        return SimpleNamespace(
            totals=SimpleNamespace(cost_micros=int(total * 1_000_000)),
            currency="USD",
        )

    async def _fake_build(chat_id, acct, **kw):
        assert kw.get("persist") is False  # персист — только для показанных Top-N
        assert kw.get("use_llm") is False
        assert kw.get("report") is not None  # отчёт из кэша прогона, не пересобирается
        cost = 80.0 if str(acct) == A else 10.0  # A: 80% под риском, B: 1%
        return SimpleNamespace(recs=[_rec(camp=f"Camp-{acct}", cost=cost)], account=str(acct))

    monkeypatch.setattr(advisor_service, "_gather_report", _fake_gather)
    monkeypatch.setattr(advisor_service, "build_recommendations", _fake_build)

    pauses: list[float] = []
    real_sleep = jobs.asyncio.sleep

    async def _fake_sleep(sec):
        pauses.append(sec)
        await real_sleep(0)

    monkeypatch.setattr(jobs.asyncio, "sleep", _fake_sleep)

    sent: list[tuple[int, str, object]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            sent.append((chat_id, text, reply_markup))

    async with Session() as s:
        p0 = (await s.execute(select(func.count()).select_from(Proposal))).scalar()

    await jobs.run_recommendations_digest(FakeBot())

    # отчёты собраны по одному разу на аккаунт
    assert sorted(gathered) == sorted([A, B])
    # header + 2 карточки + дисклеймер; карточки с кнопками; первая — A (80% > 1%)
    texts = [t for _c, t, _m in sent]
    assert any("топ-" in t or "top" in t.lower() for t in texts[:1])
    cards = [(t, m) for _c, t, m in sent if m is not None]
    assert len(cards) == 2 and all(m is not None for _t, m in cards)
    assert f"Camp-{A}" in cards[0][0]  # кросс-аккаунтный порядок: доля 80% первой
    assert "%" in cards[0][0]  # доля расхода показана
    assert pauses  # flood-паузы были

    async with Session() as s:
        p1 = (await s.execute(select(func.count()).select_from(Proposal))).scalar()
        rows = (await s.execute(select(RecRow).where(RecRow.chat_id == chat))).scalars().all()
    assert p0 == p1  # дайджест НЕ создаёт proposal (golden rule #1/#3)
    assert len(rows) == 2 and all(r.source == "scheduler" for r in rows)  # только Top-N
    assert all(r.rec_uid for r in rows)  # rec_uid проставлен — кнопки живые


async def test_digest_silent_without_optin(monkeypatch):
    from scheduler import jobs

    async def _arecipients():
        return {6102}

    async def _achats(_recipients):
        return set()  # никто не включил advise_proactive → тишина (анти-спам, fail-closed)

    monkeypatch.setattr(jobs, "_recipients", _arecipients)
    monkeypatch.setattr(jobs, "_advise_proactive_chats", _achats)

    sent: list = []

    class FakeBot:
        async def send_message(self, *a, **kw):
            sent.append(a)

    await jobs.run_recommendations_digest(FakeBot())
    assert sent == []
