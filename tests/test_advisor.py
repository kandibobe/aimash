"""advisor (Слой A/B): рекомендации advisory + инварианты безопасности.

Ключевые инварианты (golden rule #1/#3):
- пакет advisor/ НЕ импортирует ads.mutations/ads.service и не зовёт execute_confirmed/apply_/mutate_
  (AST-гард — копия tests/test_scheduler.py::test_scheduler_never_imports_mutations);
- build_recommendations НЕ создаёт proposal (рекомендация ничего не исполняет);
- фидбек 👍/👎 пишет ТОЛЬКО в recommendation_feedback (ни proposal, ни audit);
- чистое ядро (rules) детерминировано; experience-suppress воспроизводим;
- analyze_account — read-only tool (в READ_TOOLS, не в MUTATION_TOOLS); loop → advise_intent.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
from contextlib import contextmanager
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from advisor import render, rules  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────────
def _m(cost, *, clicks=10, conv=0.0, conv_value=0.0, impressions=100) -> Metrics:
    return Metrics(
        impressions=impressions,
        clicks=clicks,
        cost_micros=int(cost * 1_000_000),
        conversions=conv,
        conv_value=conv_value,
    )


def _report(rows, totals, currency="USD"):
    """Минимальный duck-typed ReportData (rules читают только breakdowns/totals/currency)."""
    camp = Breakdown("campaign", "Кампании", ["Кампания", "Статус"], rows)
    period = SimpleNamespace(label="last 30 days", date_from="2026-06-04", date_to="2026-07-03")
    return SimpleNamespace(
        customer_id="7753643025",
        period=period,
        totals=totals,
        prev_totals=None,
        breakdowns=[camp],
        currency=currency,
    )


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ── AST-гард: advisor никогда не импортирует слой мутаций/исполнения ────────────────
def test_advisor_never_imports_mutations():
    """Структурный инвариант (по AST, не по тексту): пакет advisor/ не импортирует ads.mutations/
    ads.service и не вызывает execute_confirmed/apply_*/mutate_*. Нельзя импортировать — нельзя
    вызвать → рекомендации не изменят аккаунт (golden rule #1/#3)."""
    pkg = pathlib.Path(__file__).resolve().parents[1] / "advisor"
    files = list(pkg.glob("*.py"))
    assert files, "не найдены файлы advisor/*.py"
    forbidden = {"ads.mutations", "ads.service"}
    for py in files:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=py.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod not in forbidden, f"{py.name}: import из {mod}"
                if mod == "ads":
                    for a in node.names:
                        assert a.name not in {"mutations", "service"}, (
                            f"{py.name}: from ads import {a.name}"
                        )
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name not in forbidden, f"{py.name}: import {a.name}"
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                assert name != "execute_confirmed", f"{py.name}: вызов execute_confirmed"
                assert not name.startswith(("apply_", "mutate_")), f"{py.name}: вызов {name}"


# ── Чистое ядро: детекторы Темы optimize ───────────────────────────────────────────
def test_rule_spend_no_conv():
    rows = [(("Camp A", "ENABLED"), _m(100, clicks=50, conv=0))]
    rep = _report(rows, _m(100, clicks=50, conv=0))
    recs = rules.rec_spend_no_conv(rep, rules.DEFAULT_ADVISOR_THRESHOLDS)
    assert len(recs) == 1
    r = recs[0]
    assert r.kind == "spend_no_conv" and r.topic == "optimize"
    assert r.target_campaign == "Camp A"
    assert r.suggested_operation == "pause_campaign"  # advisory-метка, не путь исполнения
    assert r.evidence["cost"] == 100


def test_rule_spend_no_conv_ignores_paused_and_low_spend():
    rows = [
        (("Paused", "PAUSED"), _m(100, clicks=50, conv=0)),  # не ENABLED
        (("Tiny", "ENABLED"), _m(1, clicks=2, conv=0)),  # ниже pause_min_spend
        (("NoClicks", "ENABLED"), _m(50, clicks=0, conv=0)),  # нет кликов
    ]
    rep = _report(rows, _m(151, clicks=52, conv=0))
    assert rules.rec_spend_no_conv(rep, rules.DEFAULT_ADVISOR_THRESHOLDS) == []


def test_rule_high_cpa():
    rows = [(("Pricey", "ENABLED"), _m(90, clicks=30, conv=1))]  # cpa 90
    totals = _m(200, clicks=100, conv=10)  # acct cpa 20; порог 2× = 40
    recs = rules.rec_high_cpa(_report(rows, totals), rules.DEFAULT_ADVISOR_THRESHOLDS)
    assert len(recs) == 1 and recs[0].kind == "high_cpa"
    assert recs[0].suggested_operation == "update_bid"


def test_rule_high_cpa_silent_on_empty_account():
    rows = [(("X", "ENABLED"), _m(90, clicks=30, conv=1))]
    totals = _m(0, clicks=0, conv=0)  # acct cpa 0 → правило молчит (нет опоры)
    assert rules.rec_high_cpa(_report(rows, totals), rules.DEFAULT_ADVISOR_THRESHOLDS) == []


def test_rule_budget_imbalance():
    rows = [
        (("Whale", "ENABLED"), _m(150, clicks=50, conv=0)),  # 75% расхода, 0 конв.
        (("Small", "ENABLED"), _m(50, clicks=20, conv=2)),
    ]
    totals = _m(200, clicks=70, conv=2)
    recs = rules.rec_budget_imbalance(_report(rows, totals), rules.DEFAULT_ADVISOR_THRESHOLDS)
    kinds = {(r.kind, r.target_campaign) for r in recs}
    assert ("budget_imbalance", "Whale") in kinds
    assert ("budget_imbalance", "Small") not in kinds  # 25% < порога
    whale = next(r for r in recs if r.target_campaign == "Whale")
    assert whale.suggested_operation == "update_budget"  # МЕТКА — бюджет меняется только командой


# ── Ранжирование: детерминизм + опыт (Слой B) ──────────────────────────────────────
def _multi_issue_report():
    rows = [
        (("Waste", "ENABLED"), _m(120, clicks=60, conv=0)),  # spend_no_conv
        (("Pricey", "ENABLED"), _m(80, clicks=20, conv=1)),  # high_cpa
    ]
    totals = _m(200, clicks=80, conv=5)  # acct cpa 40; Pricey cpa 80 ≥ 80
    return _report(rows, totals)


def test_rank_is_deterministic():
    rep = _multi_issue_report()
    a = rules.rank_recommendations(rules.build_candidates(rep))
    b = rules.rank_recommendations(rules.build_candidates(rep))

    def key(recs):
        return [(r.kind, r.target_campaign, r.priority) for r in recs]

    assert key(a) == key(b)
    assert a, "ожидались рекомендации"
    # деньги-под-риском доминируют: приоритет монотонно убывает
    assert [r.priority for r in a] == sorted((r.priority for r in a), reverse=True)


def test_rank_experience_suppress_hides_small_money():
    # мелкие деньги (< SUPPRESS_MONEY_FLOOR=50): замьюченный вид скрывается
    rep = _report([(("Waste", "ENABLED"), _m(10, clicks=6, conv=0))], _m(10, clicks=6, conv=0))
    cands = rules.build_candidates(rep)
    assert any(c.kind == "spend_no_conv" for c in cands)
    ranked = rules.rank_recommendations(cands, experience={"spend_no_conv": {"suppress": True}})
    assert all(r.kind != "spend_no_conv" for r in ranked)


def test_rank_suppress_never_hides_big_money():
    # крупные деньги-под-риском (≥ SUPPRESS_MONEY_FLOOR): совет показываем ДАЖЕ при suppress —
    # 3 👎 по одной кампании не должны прятать крупный слив по другой (фикс ревью).
    rep = _report([(("BigWaste", "ENABLED"), _m(500, clicks=200, conv=0))], _m(500, conv=0))
    cands = rules.build_candidates(rep)
    ranked = rules.rank_recommendations(cands, experience={"spend_no_conv": {"suppress": True}})
    assert any(r.kind == "spend_no_conv" for r in ranked)


def test_rank_experience_weight_scales_priority():
    rep = _report([(("Waste", "ENABLED"), _m(120, clicks=60, conv=0))], _m(120, clicks=60, conv=0))
    base = rules.rank_recommendations(rules.build_candidates(rep))[0].priority
    up = rules.rank_recommendations(
        rules.build_candidates(rep), experience={"spend_no_conv": {"weight": 2.0}}
    )[0].priority
    assert up == round(base * 2, 2)


# ── Рендер: детерминированный, с анти-мутационной формулировкой у бюджета ───────────
def test_render_budget_flags_user_command_only():
    rec = rules.Recommendation(
        kind="budget_imbalance",
        topic="optimize",
        severity="info",
        target_campaign="Whale",
        suggested_operation="update_budget",
        facts={"campaign": "Whale", "share": 75, "cpa": 100, "currency": "USD"},
        evidence={"cost": 150},
    )
    text = render.render_recommendation(rec, "ru")
    assert "Whale" in text and "команд" in text.lower()  # «только по твоей прямой команде»
    assert rec.suggested_operation == "update_budget"  # метка, не путь исполнения


# ── analyze_account — read-only tool + loop → advise_intent ─────────────────────────
def test_analyze_account_is_read_only_tool():
    from agent.tools.schemas import MUTATION_TOOLS, READ_TOOLS, SCHEMAS, TOOLS

    assert "analyze_account" in READ_TOOLS
    assert "analyze_account" not in MUTATION_TOOLS
    assert "analyze_account" in SCHEMAS
    names = {t["function"]["name"] for t in TOOLS}
    assert "analyze_account" in names


async def test_loop_analyze_account_returns_advise_intent():
    import agent.loop as L

    tc = SimpleNamespace(
        function=SimpleNamespace(
            name="analyze_account", arguments=json.dumps({"topic": "optimize"})
        )
    )

    async def _fake_chat(messages, **kwargs):
        return SimpleNamespace(tool_calls=[tc], content="")

    with patched(L, "chat", _fake_chat):
        out = await L.handle_command("что улучшить?", chat_id=7)
    assert out["type"] == "advise_intent"
    assert out["brief"]["topic"] == "optimize"


# ── Слой A/B: build_recommendations НЕ создаёт proposal; фидбек — только своя таблица ──
async def test_build_recommendations_creates_no_proposal():
    from sqlalchemy import func, select

    from advisor import service
    from db.models import Proposal, Recommendation
    from db.session import Session, init_db

    await init_db()
    rows = [(("Camp A", "ENABLED"), _m(100, clicks=50, conv=0))]
    rep = _report(rows, _m(100, clicks=50, conv=0))
    async with Session() as s:
        prop_before = (await s.execute(select(func.count()).select_from(Proposal))).scalar()

    rec_set = await service.build_recommendations(
        777, "7753643025", report=rep, use_llm=False, lang="ru"
    )
    assert rec_set.recs and rec_set.recs[0].rec_uid  # персистнуты, есть uid
    assert rec_set.recs[0].body  # текст отрендерен (fallback без LLM)

    async with Session() as s:
        prop_after = (await s.execute(select(func.count()).select_from(Proposal))).scalar()
        n_rec = (
            await s.execute(
                select(func.count())
                .select_from(Recommendation)
                .where(Recommendation.chat_id == 777)
            )
        ).scalar()
    assert prop_after == prop_before  # рекомендация НЕ создаёт proposal (golden rule #1)
    assert n_rec >= 1


async def test_feedback_writes_only_feedback_table():
    from sqlalchemy import func, select

    from advisor import store
    from advisor.rules import Recommendation
    from db.models import AuditLog, Proposal, RecommendationFeedback
    from db.session import Session, init_db

    await init_db()
    rec = Recommendation(
        kind="spend_no_conv",
        topic="optimize",
        severity="warning",
        facts={},
        evidence={"cost": 10},
        body="x",
    )
    uid = (await store.record_recommendations(888, "7753643025", [rec], "advise"))[0]

    async with Session() as s:
        prop_before = (await s.execute(select(func.count()).select_from(Proposal))).scalar()
        audit_before = (await s.execute(select(func.count()).select_from(AuditLog))).scalar()

    await store.record_feedback(uid, 888, "up", actor_user_id=1, actor_username="u")
    await store.record_feedback(uid, 888, "down", actor_user_id=1, actor_username="u")  # тогл

    async with Session() as s:
        fb = (
            (
                await s.execute(
                    select(RecommendationFeedback).where(RecommendationFeedback.rec_uid == uid)
                )
            )
            .scalars()
            .all()
        )
        prop_after = (await s.execute(select(func.count()).select_from(Proposal))).scalar()
        audit_after = (await s.execute(select(func.count()).select_from(AuditLog))).scalar()

    assert len(fb) == 1 and fb[0].rating == "down"  # один голос-тогл на оператора
    assert prop_after == prop_before  # фидбек не создаёт proposal
    assert audit_after == audit_before  # и не пишет в audit_log
