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

from advisor import rules  # noqa: E402
from advisor.from_findings import to_recommendations  # noqa: E402
from audit.engine import build_audit  # noqa: E402
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


# ── Маппер: единственный мост находка → рекомендация ───────────────────────────────
def _candidates(report, topics=None):
    """Кандидаты /advise: находки движка аудита, спроецированные маппером (детекторов у advisor нет)."""
    res = build_audit(report)
    return to_recommendations(res.findings, "ru", res.currency, topics=topics)


def test_kind_is_bare_check_id_and_topic_is_family():
    """КЛЮЧ ОБУЧЕНИЯ. До слияния /audit писал kind='audit_high_cpa', а experience.load_experience
    искала 'high_cpa' — 👍/👎 под карточками аудита копились в бакете, который никто не читал.
    Любой префикс здесь снова расщепит опыт надвое (миграция 0024 чинила ровно это)."""
    from audit.engine import CHECK_REGISTRY

    rows = [(("Camp A", "ENABLED"), _m(100, clicks=50, conv=0))]
    cands = _candidates(_report(rows, _m(100, clicks=50, conv=0)))
    assert cands, "ожидались рекомендации"
    for c in cands:
        assert not c.kind.startswith("audit_"), f"префикс в kind: {c.kind}"
        assert c.kind in CHECK_REGISTRY, f"kind ≠ check_id: {c.kind}"
        assert c.topic == CHECK_REGISTRY[c.kind][0]  # topic == семья чека (одна таксономия)
    waste = next(c for c in cands if c.kind == "spend_no_conv")
    assert waste.target_campaign == "Camp A"
    assert waste.suggested_operation == "pause_campaign"  # advisory-метка, не путь исполнения
    assert waste.evidence["cost"] == 100
    assert waste.body  # текст — детерминированный audit.render.finding_text


def test_high_cpa_carries_money_and_bid_label():
    rows = [(("Pricey", "ENABLED"), _m(90, clicks=30, conv=1))]  # cpa 90
    totals = _m(200, clicks=100, conv=10)  # acct cpa 20
    r = next(c for c in _candidates(_report(rows, totals)) if c.kind == "high_cpa")
    # update_bid — advice_operation: МЕТКА для замера эффекта; кнопки не даёт (rule #3).
    assert r.suggested_operation == "update_bid"
    assert r.at_risk and r.at_risk > 0  # деньги-под-риском → ранжирование по деньгам работает


# ── Ранжирование: детерминизм + опыт (Слой B) ──────────────────────────────────────
def _multi_issue_report():
    rows = [
        (("Waste", "ENABLED"), _m(120, clicks=60, conv=0)),  # spend_no_conv
        (("Pricey", "ENABLED"), _m(80, clicks=20, conv=1)),  # high_cpa
    ]
    totals = _m(200, clicks=80, conv=5)  # acct cpa 40; Pricey cpa 80 ≥ 2×40
    return _report(rows, totals)


def test_rank_is_deterministic():
    rep = _multi_issue_report()
    a = rules.rank_recommendations(_candidates(rep))
    b = rules.rank_recommendations(_candidates(rep))

    def key(recs):
        return [(r.kind, r.target_campaign, r.priority) for r in recs]

    assert key(a) == key(b)
    assert a, "ожидались рекомендации"
    # деньги-под-риском доминируют: приоритет монотонно убывает
    assert [r.priority for r in a] == sorted((r.priority for r in a), reverse=True)


def test_rank_experience_suppress_hides_small_money():
    # мелкие деньги (< SUPPRESS_MONEY_FLOOR=50): замьюченный вид скрывается
    rep = _report([(("Waste", "ENABLED"), _m(10, clicks=6, conv=0))], _m(10, clicks=6, conv=0))
    cands = _candidates(rep)
    assert any(c.kind == "spend_no_conv" for c in cands)
    ranked = rules.rank_recommendations(cands, experience={"spend_no_conv": {"suppress": True}})
    assert all(r.kind != "spend_no_conv" for r in ranked)


def test_rank_suppress_never_hides_big_money():
    # крупные деньги-под-риском (≥ SUPPRESS_MONEY_FLOOR): совет показываем ДАЖЕ при suppress —
    # 3 👎 по одной кампании не должны прятать крупный слив по другой (фикс ревью).
    rep = _report([(("BigWaste", "ENABLED"), _m(500, clicks=200, conv=0))], _m(500, conv=0))
    cands = _candidates(rep)
    ranked = rules.rank_recommendations(cands, experience={"spend_no_conv": {"suppress": True}})
    assert any(r.kind == "spend_no_conv" for r in ranked)


def test_rank_experience_weight_scales_priority():
    rep = _report([(("Waste", "ENABLED"), _m(120, clicks=60, conv=0))], _m(120, clicks=60, conv=0))
    spend = [c for c in _candidates(rep) if c.kind == "spend_no_conv"]
    base = rules.rank_recommendations(list(spend))[0].priority
    up = rules.rank_recommendations(list(spend), experience={"spend_no_conv": {"weight": 2.0}})[
        0
    ].priority
    assert up == round(base * 2, 2)


def test_magnitude_falls_back_to_entity_cost_when_at_risk_is_zero():
    """budget_imbalance / low_ctr_ad / single_campaign имеют at_risk = 0 ПО ПОСТРОЕНИЮ (их деньги
    уже посчитаны в другом сегменте — иначе «Под риском» задвоилось бы). Если бы _magnitude был
    равен at_risk, эти три получали бы priority = 0.5 и никогда не проходили срез (MAX_RECS=5,
    дайджест top_n=5) — перекос бюджета, сегодня обычно первый в дайджесте, просто исчез бы."""
    rec = rules.Recommendation(
        kind="budget_imbalance",
        topic="budget",
        severity="info",
        target_campaign="Whale",
        suggested_operation="update_budget",
        facts={},
        evidence={"cost": 150.0},
        at_risk=0.0,
    )
    assert rules._magnitude(rec) == 150.0
    assert rules.rank_recommendations([rec])[0].priority > 0.5


def test_audit_does_not_rank_or_suppress():
    """/audit — ДИАГНОСТИКА, а не советы: он показывает находки в порядке движка (деньги-под-риском)
    и НЕ зовёт rank_recommendations. Иначе suppress (много 👎/🙈) спрятал бы строку, которая всё
    равно штрафует score — клиент увидел бы «score 62, waste −18» без единого объяснения откуда −18
    (money-floor не спасает: у ~18 из 27 чеков at_risk = 0 по построению).

    AST-гард по bot/main.py: ни импорта, ни вызова rank_recommendations в файле нет."""
    src = pathlib.Path(__file__).resolve().parents[1] / "bot" / "main.py"
    tree = ast.parse(src.read_text(encoding="utf-8"), filename="main.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("advisor"):
            for a in node.names:
                assert a.name != "rank_recommendations", (
                    "bot/main.py импортирует rank_recommendations"
                )
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name != "rank_recommendations", "bot/main.py вызывает rank_recommendations"


def test_advise_findings_are_subset_of_audit():
    """ГАРД КЛАССА. /advise строит аудит БЕЗ ctx-сигналов (только отчёт — лишних чтений API не
    делаем), /audit — с ними. Значит совет может лишь МОЛЧАТЬ там, где аудит говорит; сказать ДРУГОЕ
    он не вправе — иначе две команды бота дают клиенту два ответа про один аккаунт (ровно то, что
    слияние и убирало). Ловит любой будущий чек с эвристическим фолбэком «нет ctx → всё равно выдам
    находку»: сегодня такой один — check_no_conversion_tracking (без ctx кричит «отслеживания нет»
    аккаунту, где оно есть, а конверсий 0; at_risk = весь расход ⇒ №1 в ранге и в дайджесте)."""
    rows = [(("Camp A", "ENABLED"), _m(100, clicks=50, conv=0))]
    rep = _report(rows, _m(100, clicks=50, conv=0))
    live = build_audit(rep, conversion_actions=[SimpleNamespace(status="ENABLED")])
    audit_ids = {f.check_id for f in live.findings}
    assert "zero_conversions" in audit_ids  # с ctx: отслеживание есть, конверсий нет

    advise = to_recommendations(build_audit(rep).findings, "ru", rep.currency, report_only=True)
    kinds = {c.kind for c in advise}
    assert kinds, "ожидались рекомендации"
    assert "no_conversion_tracking" not in kinds  # без ctx — молчим, а не врём
    assert kinds <= audit_ids, f"/advise сказал то, чего /audit не говорит: {kinds - audit_ids}"


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
