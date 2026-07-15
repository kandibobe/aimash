"""Батч 1: обогащённый СИД аудита (_audit_facts) + read-only drill get_competitors/get_bid_landscape.

Лечим «голодание контекста»: нарратив и Q&A делят один сид и один drill, но конкуренция/ставки
(at_risk=0 → хвост сортировки severity+at_risk) выпадали из топ-N, а домены соперников и оценки ставок
были недостижимы вовсе — Q&A врал «данных нет». Доказываем:
- сид добирает ХУДШУЮ находку каждой ещё не представленной семьи сверх топ-N;
- срез конкурентов из ЛОКАЛЬНОЙ БД попадает в сид в ПРОЦЕНТАХ (fact-guard сверяет токен прозы → долю
  0.32 модель процитирует как «32%», а 32 ∉ {0.32} — храним percent);
- сбой БД конкурентов не роняет сид (best-effort);
- новые drill-тулзы отдают данные и has_data:false на пустой БД;
- S4: новые тулзы read-only, без выбора аккаунта, не мутации.

Без сети/SDK: latest_snapshot и run_ads_read_call монкейпатчатся.
"""

from __future__ import annotations

from types import SimpleNamespace

import bot.main as bm


def _f(check_id, family, severity, at_risk, campaign="C", **facts):
    return SimpleNamespace(
        check_id=check_id,
        family=family,
        severity=severity,
        target_campaign=campaign,
        at_risk=at_risk,
        facts=facts,
    )


def _result(findings):
    fams: dict = {}
    for f in findings:
        d = fams.setdefault(f.family, {"count": 0, "at_risk": 0.0})
        d["count"] += 1
        d["at_risk"] += f.at_risk
    return SimpleNamespace(
        customer_id="1234567890",
        currency="USD",
        score=76,
        grade="C",
        total_spend=100000.0,
        at_risk=5000.0,
        optimization_score=0.8,
        findings=findings,
        families=fams,
    )


def _row(domain, is_you, imp, over, above, outrank):
    return SimpleNamespace(
        domain=domain,
        is_you=is_you,
        impression_share=imp,
        overlap_rate=over,
        position_above_rate=above,
        top_of_page_rate=0.0,
        abs_top_of_page_rate=0.0,
        outranking_share=outrank,
    )


def _snap():
    return SimpleNamespace(
        snapshot_date="2026-07-10",
        period_label="1–30 июн",
        you=_row("darial.co.jp", True, 0.32, 0.0, 0.0, 0.0),
        competitors=(
            _row("beforward.jp", False, 0.61, 0.44, 0.55, 0.70),
            _row("autocom.jp", False, 0.28, 0.30, 0.40, 0.50),
        ),
    )


# ── сид: добор худшей находки каждой семьи сверх топ-N ────────────────────────────────────
async def test_seed_adds_worst_finding_per_family_beyond_topn(monkeypatch):
    monkeypatch.setattr(bm, "_AUDIT_MAX_FINDINGS", 2)

    async def _no_snap(cid, **k):
        return None

    monkeypatch.setattr("db.competitors.latest_snapshot", _no_snap)
    findings = [  # worst-first: 2 waste займут топ-2, competition/bidding — хвост (at_risk=0)
        _f("spend_no_conv", "waste", "high", 3000.0, cost=3000.0),
        _f("high_cpa", "waste", "high", 2000.0, cost=2000.0),
        _f("weak_share", "competition", "medium", 0.0, share_pct=32.0),
        _f("underbid_top", "bidding", "medium", 0.0, bid=1.5),
    ]
    facts = await bm._audit_facts(_result(findings), 30)
    fams = [x["family"] for x in facts["findings"]]
    assert fams[:2] == ["waste", "waste"]  # топ-2 не тронуты
    assert "competition" in fams and "bidding" in fams  # добраны, а не выпали в хвост
    # добор — именно ХУДШАЯ находка семьи (первая в worst-first хвосте)
    comp = next(x for x in facts["findings"] if x["family"] == "competition")
    assert comp["issue"] == "weak_share"


# ── сид: срез конкурентов из локальной БД → проценты (не доли) ────────────────────────────
async def test_seed_loads_competitors_as_percent(monkeypatch):
    async def _snap_fn(cid, **k):
        return _snap()

    monkeypatch.setattr("db.competitors.latest_snapshot", _snap_fn)
    facts = await bm._audit_facts(_result([_f("x", "waste", "high", 1.0)]), 30)
    comp = facts["competitors"]
    assert comp["snapshot_date"] == "2026-07-10"
    assert comp["you_impression_share_pct"] == 32.0  # 0.32 → 32.0 (percent, НЕ доля 0.32)
    assert comp["rivals"][0]["domain"] == "beforward.jp"  # по убыванию доли
    assert comp["rivals"][0]["impression_share_pct"] == 61.0
    assert comp["rivals"][0]["outranking_share_pct"] == 70.0


async def test_seed_survives_competitor_db_failure(monkeypatch):
    async def _boom(cid, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("db.competitors.latest_snapshot", _boom)
    facts = await bm._audit_facts(_result([_f("x", "waste", "high", 1.0)]), 30)
    assert "competitors" not in facts  # сбой → просто нет блока, сид цел
    assert facts["findings"]  # находки на месте


# ── drill: get_competitors ───────────────────────────────────────────────────────────────
async def test_drill_get_competitors(monkeypatch):
    async def _snap_fn(cid, **k):
        assert cid == "7753643025"  # cid залочен на проаудированный аккаунт
        return _snap()

    monkeypatch.setattr("db.competitors.latest_snapshot", _snap_fn)
    drill = bm._make_audit_drill(object(), "7753643025", object())
    out = await drill("get_competitors", {})
    assert out["has_data"] is True
    assert out["you_impression_share_pct"] == 32.0
    assert out["rivals"][0]["domain"] == "beforward.jp"
    assert out["rivals"][0]["impression_share_pct"] == 61.0
    assert out["rivals"][0]["overlap_rate_pct"] == 44.0


async def test_drill_get_competitors_empty(monkeypatch):
    async def _none(cid, **k):
        return None

    monkeypatch.setattr("db.competitors.latest_snapshot", _none)
    drill = bm._make_audit_drill(object(), "7753643025", object())
    assert await drill("get_competitors", {}) == {"has_data": False}


# ── drill: get_bid_landscape ─────────────────────────────────────────────────────────────
async def test_drill_get_bid_landscape(monkeypatch):
    bidrow = SimpleNamespace(
        keyword="buy car japan",
        campaign="Search-JP",
        match_type="BROAD",
        strategy_type="MANUAL_CPC",
        bid=1.5,
        top_of_page_cpc=2.4,
        first_position_cpc=3.0,
        top_is=0.40,
        rank_lost_top_is=0.35,
    )

    async def _fake_read(fn, *a, **k):
        return [bidrow]

    monkeypatch.setattr("core.resilience.run_ads_read_call", _fake_read)
    drill = bm._make_audit_drill(object(), "7753643025", object())
    out = await drill("get_bid_landscape", {})
    row = out["bid_landscape"][0]
    assert row["keyword"] == "buy car japan"
    assert row["strategy_type"] == "MANUAL_CPC"  # ручная стратегия — «поднять ставку» осмысленно
    assert row["bid"] == 1.5 and row["top_of_page_cpc"] == 2.4
    assert row["top_impression_share_pct"] == 40.0  # 0.40 → 40.0
    assert row["rank_lost_top_share_pct"] == 35.0


async def test_drill_unknown_tool_still_errors():
    drill = bm._make_audit_drill(object(), "7753643025", object())
    assert await drill("apply_pause_campaign", {}) == {"error": "unknown tool"}


# ── S4/GR9: новые тулзы зарегистрированы, read-only, не мутации ───────────────────────────
def test_new_analysis_tools_registered_and_readonly():
    from agent.tools.schemas import ANALYSIS_TOOL_NAMES, ANALYSIS_TOOLS, MUTATION_TOOLS

    assert {"get_competitors", "get_bid_landscape"} <= ANALYSIS_TOOL_NAMES
    assert ANALYSIS_TOOL_NAMES.isdisjoint(MUTATION_TOOLS)  # S4 держится с новыми тулзами
    for t in ANALYSIS_TOOLS:  # GR9: модель не выбирает аккаунт
        if t["function"]["name"] in {"get_competitors", "get_bid_landscape"}:
            assert "account" not in t["function"]["parameters"].get("properties", {})
