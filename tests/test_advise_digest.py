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
