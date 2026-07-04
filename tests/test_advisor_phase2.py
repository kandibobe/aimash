"""advisor Слой B (Фаза 2): outcome-связывание + замер результата + опыт как код-сигнал.

Инварианты: link_applied_mutation матчит применённую мутацию к рекомендации ДЕТЕРМИНИРОВАННО и
только в локальную БД (ни мутаций Ads, ни proposal); verdict/experience считает КОД; measure_outcome
read-only. AST-гард «advisor не импортирует mutations» и «scheduler не импортирует mutations» —
в tests/test_advisor.py и tests/test_scheduler.py (новые джобы попадают под последний).
"""

from __future__ import annotations

import pathlib
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from advisor import experience, outcome  # noqa: E402
from advisor.rules import Recommendation  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _m(cost, *, clicks=10, conv=0.0):
    return Metrics(
        impressions=100,
        clicks=clicks,
        cost_micros=int(cost * 1_000_000),
        conversions=conv,
        conv_value=0.0,
    )


# ── verdict / baseline — чистые функции ─────────────────────────────────────────────
def test_verdict_improved_on_new_conversions():
    before = {"cost": 100, "conversions": 0, "cpa": 0}
    after = {"cost": 100, "conversions": 3, "cpa": 33.3}
    assert outcome.verdict(before, after) == "improved"


def test_verdict_improved_on_cpa_drop():
    assert (
        outcome.verdict(
            {"cost": 100, "conversions": 5, "cpa": 20}, {"cost": 100, "conversions": 5, "cpa": 15}
        )
        == "improved"
    )


def test_verdict_worse_and_neutral():
    assert (
        outcome.verdict(
            {"cost": 100, "conversions": 5, "cpa": 20}, {"cost": 100, "conversions": 2, "cpa": 50}
        )
        == "worse"
    )
    assert (
        outcome.verdict(
            {"cost": 100, "conversions": 5, "cpa": 20}, {"cost": 100, "conversions": 5, "cpa": 20}
        )
        == "neutral"
    )
    assert outcome.verdict(None, {"cost": 1, "conversions": 1, "cpa": 1}) == "neutral"


def test_verdict_conversion_drop_beats_cpa_drop():
    # фикс ревью: обвал конверсий НЕ должен маскироваться падением CPA → 'worse', не 'improved'
    before = {"cost": 1000, "conversions": 10, "cpa": 100}
    after = {"cost": 170, "conversions": 2, "cpa": 85}  # CPA упал, но конверсии рухнули -80%
    assert outcome.verdict(before, after) == "worse"


def test_baseline_from_evidence_derives_cpa():
    b = outcome._baseline_from_evidence({"cost": 100, "conversions": 4})
    assert b["cpa"] == 25.0 and b["conversions"] == 4


# ── formulate: код-гард сохранности фактов (искажение LLM → откат на render) ─────────
def test_formulate_facts_preserved_guard():
    from advisor import formulate
    from advisor.rules import Recommendation

    rec = Recommendation(
        kind="spend_no_conv",
        topic="optimize",
        severity="warning",
        target_campaign="Бренд",
        facts={"campaign": "Бренд"},
    )
    base = "• «Бренд»: расход 1 234.00 UAH при 56 кликах"
    assert formulate._facts_preserved(base, "Кампания «Бренд» слила 1 234.00 UAH за 56 кликов", rec)
    # искажена сумма → не сохранено
    assert not formulate._facts_preserved(base, "«Бренд» слила ≈1200 UAH за 56 кликов", rec)
    # пропало имя кампании → не сохранено
    assert not formulate._facts_preserved(base, "Кампания слила 1 234.00 UAH за 56 кликов", rec)


async def test_formulate_reverts_distorted_line():
    from advisor import formulate
    from advisor.rules import Recommendation

    rec = Recommendation(
        kind="single_campaign",
        topic="structure",
        severity="info",
        target_campaign="Solo",
        facts={"campaign": "Solo", "cost": 120, "currency": "USD"},
    )
    base_line = formulate.render.render_recommendation(rec, "ru")

    async def _distort(messages, **kwargs):
        return SimpleNamespace(content='["Solo — перепиши без чисел вообще"]')

    with patched(formulate, "chat", _distort):
        out = await formulate.phrase([rec], "ru")
    assert out == [base_line]  # искажённая (без суммы) строка откачена на детерминированный render


# ── link_applied_mutation — детерминированный матч, только локальная БД ──────────────
async def _persist_rec(
    chat, customer, *, kind="spend_no_conv", op="pause_campaign", camp="Camp X", evidence=None
):
    from advisor import store

    rec = Recommendation(
        kind=kind,
        topic="optimize",
        severity="warning",
        target_campaign=camp,
        suggested_operation=op,
        facts={},
        evidence=evidence or {"cost": 100, "conversions": 0},
        body="x",
    )
    (uid,) = await store.record_recommendations(chat, customer, [rec], "advise")
    return uid


async def test_link_matches_and_no_proposal():
    from sqlalchemy import func, select

    from db.models import Proposal, RecommendationOutcome
    from db.session import Session, init_db

    await init_db()
    chat, customer = 5001, "7753643025"
    await _persist_rec(chat, customer, op="pause_campaign", camp="Camp X")

    async with Session() as s:
        prop_before = (await s.execute(select(func.count()).select_from(Proposal))).scalar()

    ok = await outcome.link_applied_mutation(
        chat, "pause_campaign", {"campaign": "Camp X"}, customer, "cid-abc"
    )
    assert ok is True

    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(RecommendationOutcome).where(
                        RecommendationOutcome.confirmation_id == "cid-abc"
                    )
                )
            )
            .scalars()
            .all()
        )
        prop_after = (await s.execute(select(func.count()).select_from(Proposal))).scalar()
    assert len(rows) == 1
    assert rows[0].target_campaign == "Camp X" and rows[0].baseline["cpa"] == 0.0
    assert prop_after == prop_before  # связывание НЕ создаёт proposal (golden rule #1)


async def test_link_no_match_and_dedup():
    from sqlalchemy import func, select

    from db.models import RecommendationOutcome
    from db.session import Session, init_db

    await init_db()
    chat, customer = 5002, "7753643025"
    await _persist_rec(chat, customer, op="pause_campaign", camp="Camp Y")

    # операция не совпала → нет outcome
    assert (
        await outcome.link_applied_mutation(
            chat, "update_bid", {"campaign": "Camp Y"}, customer, "cid-nomatch"
        )
        is False
    )
    # кампания не совпала → нет outcome
    assert (
        await outcome.link_applied_mutation(
            chat, "pause_campaign", {"campaign": "Other"}, customer, "cid-nomatch2"
        )
        is False
    )
    # совпало → outcome; повтор по тому же confirmation_id → дубля нет
    assert (
        await outcome.link_applied_mutation(
            chat, "pause_campaign", {"campaign": "Camp Y"}, customer, "cid-dedup"
        )
        is True
    )
    assert (
        await outcome.link_applied_mutation(
            chat, "pause_campaign", {"campaign": "Camp Y"}, customer, "cid-dedup"
        )
        is False
    )

    async with Session() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(RecommendationOutcome)
                .where(RecommendationOutcome.confirmation_id == "cid-dedup")
            )
        ).scalar()
    assert n == 1


# ── measure_outcome — read-only чтение ДО/ПОСЛЕ → verdict в БД ───────────────────────
async def test_measure_outcome_sets_verdict(monkeypatch):
    from db.session import init_db

    import reports.queries as rq

    await init_db()
    chat, customer = 5003, "7753643025"
    await _persist_rec(chat, customer, op="pause_campaign", camp="Camp Z")
    apply_dt = datetime.now(timezone.utc) - timedelta(days=20)
    await outcome.link_applied_mutation(
        chat,
        "pause_campaign",
        {"campaign": "Camp Z"},
        customer,
        "cid-measure",
        now=apply_dt,
    )
    due = await outcome.due_outcomes()  # measure_after=apply+14д уже прошёл
    assert any(o.confirmation_id == "cid-measure" for o in due)
    target = next(o for o in due if o.confirmation_id == "cid-measure")

    calls = {"n": 0}

    def _fake_by_campaign(client, customer_id, period, campaign_id=None):
        calls["n"] += 1
        conv = 0.0 if calls["n"] == 1 else 3.0  # before: 0 конв., after: 3 → improved
        return Breakdown(
            "campaign",
            "Кампании",
            ["Кампания", "Статус"],
            [(("Camp Z", "ENABLED"), _m(100, clicks=40, conv=conv))],
        )

    monkeypatch.setattr(rq, "fetch_by_campaign", _fake_by_campaign)
    v = await outcome.measure_outcome(target, client=object())
    assert v == "improved"

    remaining = await outcome.due_outcomes()  # замерена → больше не due
    assert all(o.confirmation_id != "cid-measure" for o in remaining)


# ── experience — опыт как код-множитель (фидбек + вердикты) ──────────────────────────
async def test_experience_suppress_on_downvotes():
    from advisor import store
    from db.session import init_db

    await init_db()
    chat, customer = 5004, "7753643025"
    uids = []
    for _ in range(3):  # три рекомендации одного вида, все с 👎
        uids.append(await _persist_rec(chat, customer, kind="high_cpa", op="update_bid", camp="C"))
    for u in uids:
        await store.record_feedback(u, chat, "down", actor_user_id=1)

    exp = await experience.load_experience(chat, customer)
    assert exp["high_cpa"]["suppress"] is True
    assert exp["high_cpa"]["weight"] < 1.0


async def test_experience_weight_up_on_improved_outcomes():
    from advisor import store
    from db.models import RecommendationOutcome
    from db.session import Session, init_db

    await init_db()
    chat, customer = 5005, "7753643025"
    u1 = await _persist_rec(chat, customer, kind="spend_no_conv", op="pause_campaign", camp="A")
    await store.record_feedback(u1, chat, "up", actor_user_id=1)
    # добавим положительный вердикт замера напрямую
    async with Session() as s:
        s.add(
            RecommendationOutcome(
                rec_uid=u1, customer_id=customer, target_campaign="A", verdict="improved"
            )
        )
        await s.commit()

    exp = await experience.load_experience(chat, customer)
    assert exp["spend_no_conv"]["weight"] > 1.0
    assert exp["spend_no_conv"]["suppress"] is False


def test_experience_empty_when_no_history():
    # синхронная обёртка не нужна: чистая проверка _weight (детерминизм формулы)
    w0, s0 = experience._weight(0, 0, 0, 0)
    assert w0 == 1.0 and s0 is False
    w1, s1 = experience._weight(0, 5, 0, 0)
    assert w1 < 1.0 and s1 is True
