"""Детерминированный движок аудита аккаунта — БЕЗ сети/SDK, полностью тестируемо.

Принимает УЖЕ прочитанные данные (reports.service.ReportData + опц. impression-share / действия-
конверсии / стратегии ставок / нативный optimization_score — всё duck-typed, БЕЗ импорта reports/ads)
и возвращает AuditResult: health-score (КОД), буква, деньги-под-риском (дедуп ≤ расхода), находки по
семьям. Это ТОЛЬКО диагностика — движок НЕ создаёт proposal и не меняет аккаунт (golden rule #1/#3);
исполнение любого совета идёт отдельной командой через confirm-гейт.

Google-optimization_score показываем как «второе мнение» РЯДОМ (не в нашем score) — числа Google
считает Google. Наш score — только КОД.

⚠️ Пакет audit/ НЕ импортирует ads.mutations / ads.service (инвариант test_audit_engine).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from audit.thresholds import (
    DEFAULT_AUDIT_THRESHOLDS,
    FAMILY_WEIGHT,
    GRADE_BANDS,
    NONMONEY_INTENSITY,
    SCORE_MODEL_EPOCH,
    SEVERITY_MULT,
    grade_for,
)

# Операции, применимые в ОДИН тап (совпадает с bot ADVISE_APPLY_OPS — не денежные). Всё остальное
# (бюджет/ставка) — только прозаический совет без кнопки: бюджет меняется лишь прямой командой (rule #3).
ONE_TAP_OPS = frozenset({"pause_campaign", "add_negative_keywords"})


@dataclass
class Finding:
    """Одна находка аудита. at_risk — деньги-под-риском в валюте аккаунта (0 → неденежная).
    spend_segment — ключ дедупа денег (кампания/ключ/«__account__»), чтобы находки на одном сегменте
    не суммировались (крит.C2). suggested_operation ∈ ONE_TAP_OPS → кнопка «применить»; иначе — прозой."""

    check_id: str
    family: str
    severity: str  # warning | info
    at_risk: float = 0.0
    spend_segment: str | None = None
    target_campaign: str | None = None
    suggested_operation: str | None = None
    facts: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)

    @property
    def one_tap(self) -> bool:
        return self.suggested_operation in ONE_TAP_OPS


@dataclass
class AuditResult:
    customer_id: str
    currency: str
    score: int | None  # None → нет активности (grade «—»)
    grade: str
    total_spend: float
    at_risk: float  # дедуплицированные деньги-под-риском, ≤ total_spend
    findings: list[Finding]  # ранжированы worst-first
    families: dict  # family → {"count", "at_risk", "penalty"}
    has_activity: bool
    # Нативный Google optimization_score (0..100) + uplift — «второе мнение», НЕ наш score. В конце
    # dataclass (позиционные конструкторы/тесты не ломаются). None → недоступно.
    optimization_score: int | None = None
    optimization_uplift: int | None = None
    # Нативные Google-рекомендации (тип → число активных) — показ, НЕ применяем (см. render).
    google_recommendations: dict = field(default_factory=dict)
    # N1.0a: версия score-модели (хэш весов/порогов/набора проверок) — тренд-дельты сравнимы ТОЛЬКО
    # в пределах одной версии, иначе деплой нового чека читается клиентом как ложный «−10 за неделю».
    score_model_version: str = ""
    # N1.3: best-effort сигналы, которые collect НЕ получил (сбой фетчера → None). None → collect
    # не запускался вовсе (engine-only вызов, напр. /report health) — рендер тогда молчит про семьи.
    data_gaps: list | None = None
    # N1.5: расход есть, а активной PRIMARY-конверсии нет → баннер «сначала почини измерение»
    # НАД score (сам score не подавляем — честность). False и при отсутствии живых conversion_actions.
    measurement_gap: bool = False


@dataclass
class _Ctx:
    """Контекст проверок: цель CPA (глоб./пер-кампания) + опц. живые данные (IS/конверсии). duck-typed."""

    target_cpa: float | None = None
    is_rows: list | None = None
    conversion_actions: list | None = None
    search_terms: list | None = None
    bidding_by_name: dict = field(default_factory=dict)

    def target_for(self, campaign_name: str) -> float | None:
        """Цель CPA для кампании: пер-кампанийная стратегия (tCPA) побеждает глобальный /target."""
        b = self.bidding_by_name.get(campaign_name)
        if b is not None and getattr(b, "target_cpa", None):
            return float(b.target_cpa)
        return self.target_cpa


# ── Хелперы чтения разбивок (как advisor/rules.py) ───────────────────────────────────
def _breakdown_rows(report, key: str) -> list:
    b = next((b for b in getattr(report, "breakdowns", []) if b.key == key), None)
    return list(b.rows) if b and b.rows else []


def _campaign_rows(report) -> list:
    return _breakdown_rows(report, "campaign")


# ── Проверки (принимают уже прочитанные данные + ctx; ничего не читают) ───────────────
def check_spend_no_conv(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """ENABLED-кампания: расход ≥ порога, клики есть, 0 конверсий — деньги впустую (семья waste)."""
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED":
            continue
        if m.cost >= thr["pause_min_spend"] and m.clicks > 0 and m.conversions == 0:
            out.append(
                Finding(
                    check_id="spend_no_conv",
                    family="waste",
                    severity="warning",
                    at_risk=round(m.cost, 2),
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation="pause_campaign",
                    facts={
                        "campaign": name,
                        "cost": round(m.cost, 2),
                        "clicks": m.clicks,
                        "currency": cur,
                    },
                    evidence={
                        "cost": round(m.cost, 2),
                        "clicks": m.clicks,
                        "conversions": m.conversions,
                    },
                )
            )
    return out


def check_high_cpa(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """ENABLED-кампания с конверсиями, чей CPA ≥ factor × средний по аккаунту (семья waste, прозой).
    at_risk = перерасход над средней эффективностью аккаунта. totals.cpa == 0 → правило молчит."""
    acct_cpa = float(getattr(report.totals, "cpa", 0.0) or 0.0)
    if acct_cpa <= 0:
        return []
    factor = thr["high_cpa_factor"]
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED" or m.conversions <= 0 or m.cost < thr["min_spend"]:
            continue
        if m.cpa >= factor * acct_cpa:
            excess = max(0.0, m.cost - m.conversions * acct_cpa)
            out.append(
                Finding(
                    check_id="high_cpa",
                    family="waste",
                    severity="warning",
                    at_risk=round(excess, 2),
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation=None,  # ставка/бюджет — прозой (rule #3), без кнопки
                    facts={
                        "campaign": name,
                        "cpa": round(m.cpa, 2),
                        "acct_cpa": round(acct_cpa, 2),
                        "factor": round(m.cpa / acct_cpa, 1),
                        "currency": cur,
                    },
                    evidence={
                        "cpa": round(m.cpa, 2),
                        "acct_cpa": round(acct_cpa, 2),
                        "cost": round(m.cost, 2),
                    },
                )
            )
    return out


def check_kill_rule(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """«3× Kill Rule»: CPA кампании ≥ factor × ЦЕЛЬ. Цель — пер-кампанийная tCPA (ctx.bidding) ИЛИ
    глобальный /target. Нет цели → no-op (не фабрикуем «×цель» на goalless/manual-CPC). pause — one-tap."""
    factor = thr["kill_cpa_factor"]
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED" or m.conversions <= 0 or m.cost < thr["min_spend"]:
            continue
        target = ctx.target_for(name)
        if not target or target <= 0:
            continue
        if m.cpa >= factor * target:
            excess = max(0.0, m.cost - m.conversions * target)
            out.append(
                Finding(
                    check_id="kill_rule",
                    family="waste",
                    severity="warning",
                    at_risk=round(excess, 2),
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation="pause_campaign",
                    facts={
                        "campaign": name,
                        "cpa": round(m.cpa, 2),
                        "target_cpa": round(float(target), 2),
                        "factor": round(m.cpa / target, 1),
                        "currency": cur,
                    },
                    evidence={
                        "cpa": round(m.cpa, 2),
                        "target_cpa": round(float(target), 2),
                        "cost": round(m.cost, 2),
                    },
                )
            )
    return out


def check_wasteful_keyword(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ключ с расходом ≥ порога и кликами, но 0 конверсий → кандидат в минус-слова (семья keywords).
    Топ-N по расходу (анти-спам). one-tap add_negative_keywords (по умолчанию exact — решает bot-слой)."""
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for dims, m in _breakdown_rows(report, "keyword"):
        campaign, ad_group, kw_text, match_type = (list(dims) + ["", "", "", ""])[:4]
        if m.cost >= thr["kw_min_spend"] and m.clicks > 0 and m.conversions == 0:
            out.append(
                Finding(
                    check_id="wasteful_keyword",
                    family="keywords",
                    severity="warning",
                    at_risk=round(m.cost, 2),
                    spend_segment=f"kw::{campaign}::{kw_text}",
                    target_campaign=campaign,
                    suggested_operation="add_negative_keywords",
                    facts={
                        "campaign": campaign,
                        "ad_group": ad_group,
                        "keyword": kw_text,
                        "match_type": match_type,
                        "cost": round(m.cost, 2),
                        "clicks": m.clicks,
                        "currency": cur,
                    },
                    evidence={"cost": round(m.cost, 2), "clicks": m.clicks, "keyword": kw_text},
                )
            )
    out.sort(key=lambda f: -float(f.evidence.get("cost", 0) or 0))
    return out[: int(thr.get("kw_top_n", 5))]


def check_wasteful_search_term(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Поисковый запрос (search_term_view) с расходом ≥ порога и кликами, но 0 конверсий → кандидат
    в МИНУС-СЛОВА (семья keywords). ОТДЕЛЬНЫЙ kind от wasteful_keyword (запрос vs существующий ключ) —
    дедуп по сегменту «st::». Минус по умолчанию EXACT (режет только этот запрос, не шире). Топ-N."""
    rows = ctx.search_terms or []
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for st in rows:
        m = getattr(st, "metrics", None)
        if m is None:
            continue
        if m.cost >= thr["kw_min_spend"] and m.clicks > 0 and m.conversions == 0:
            term = getattr(st, "search_term", "")
            campaign = getattr(st, "campaign", "")
            out.append(
                Finding(
                    check_id="wasteful_search_term",
                    family="keywords",
                    severity="warning",
                    at_risk=round(m.cost, 2),
                    spend_segment=f"st::{campaign}::{term}",
                    target_campaign=campaign,
                    suggested_operation="add_negative_keywords",
                    facts={
                        "campaign": campaign,
                        "search_term": term,
                        "cost": round(m.cost, 2),
                        "clicks": m.clicks,
                        "currency": cur,
                    },
                    # match_type=exact → _advise_apply_params добавит ТОЧНЫЙ минус (не broad).
                    evidence={
                        "keyword": term,
                        "match_type": "exact",
                        "cost": round(m.cost, 2),
                        "clicks": m.clicks,
                    },
                )
            )
    out.sort(key=lambda f: -float(f.evidence.get("cost", 0) or 0))
    return out[: int(thr.get("kw_top_n", 5))]


def check_low_ctr_ad(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Объявление с показами ≥ порога и CTR < factor × средний по аккаунту → освежить RSA (семья rsa,
    неденежная info). Опора — totals.ctr (0 → правило молчит)."""
    acct_ctr = float(getattr(report.totals, "ctr", 0.0) or 0.0)
    if acct_ctr <= 0:
        return []
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for dims, m in _breakdown_rows(report, "ad"):
        campaign, ad_group = (list(dims) + ["", ""])[:2]
        if m.impressions >= thr["min_impressions"] and m.ctr < acct_ctr * thr["low_ctr_factor"]:
            out.append(
                Finding(
                    check_id="low_ctr_ad",
                    family="rsa",
                    severity="info",
                    at_risk=0.0,
                    spend_segment=None,
                    target_campaign=campaign,
                    suggested_operation=None,  # create_rsa — не one-tap (нужна курация)
                    facts={
                        "campaign": campaign,
                        "ad_group": ad_group,
                        "ctr": round(m.ctr * 100, 2),
                        "acct_ctr": round(acct_ctr * 100, 2),
                        "currency": cur,
                    },
                    evidence={
                        "ctr": round(m.ctr, 4),
                        "acct_ctr": round(acct_ctr, 4),
                        "impressions": m.impressions,
                    },
                )
            )
    return out


def check_budget_imbalance(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Одна ENABLED-кампания съедает ≥ budget_share_pct расхода, а её эффективность НЕ лучше средней
    (семья budget, неденежная info, прозой — бюджет только прямой командой, rule #3)."""
    total_cost = float(getattr(report.totals, "cost", 0.0) or 0.0)
    if total_cost < thr["pause_min_spend"]:
        return []
    acct_cpa = float(getattr(report.totals, "cpa", 0.0) or 0.0)
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED":
            continue
        share = m.cost / total_cost * 100.0 if total_cost else 0.0
        worse = (m.conversions <= 0) or (acct_cpa > 0 and m.cpa > acct_cpa)
        if share >= thr["budget_share_pct"] and worse:
            out.append(
                Finding(
                    check_id="budget_imbalance",
                    family="budget",
                    severity="info",
                    at_risk=0.0,
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation=None,
                    facts={
                        "campaign": name,
                        "share": round(share),
                        "cpa": round(m.cpa, 2),
                        "currency": cur,
                    },
                    evidence={
                        "share": round(share, 1),
                        "cpa": round(m.cpa, 2),
                        "cost": round(m.cost, 2),
                    },
                )
            )
    return out


def check_single_campaign(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Единственная ENABLED-кампания с расходом ≥ порога → рассмотреть разделение (семья structure,
    неденежная info, без кнопки — ведёт к созданию новой кампании через отдельный флоу)."""
    enabled = [((n, s), m) for (n, s), m in _campaign_rows(report) if s == "ENABLED"]
    if len(enabled) != 1:
        return []
    (name, _s), m = enabled[0]
    if m.cost < thr["single_campaign_min_spend"]:
        return []
    return [
        Finding(
            check_id="single_campaign",
            family="structure",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=name,
            suggested_operation=None,
            facts={
                "campaign": name,
                "cost": round(m.cost, 2),
                "currency": getattr(report, "currency", ""),
            },
            evidence={"cost": round(m.cost, 2)},
        )
    ]


def check_no_conversion_tracking(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Отслеживание конверсий. С живыми conversion_action (ctx): НЕТ активных действий при расходе →
    красный «no_conversion_tracking» (весь расход под риском); есть действия, но 0 конверсий при
    кликах → «zero_conversions». Без данных о действиях — метрическая эвристика (расход & 0 конв)."""
    t = report.totals
    total_cost = float(getattr(t, "cost", 0.0) or 0.0)
    clicks = float(getattr(t, "clicks", 0) or 0)
    conv = float(getattr(t, "conversions", 0.0) or 0.0)
    cur = getattr(report, "currency", "")
    spends = total_cost >= thr["no_conv_min_spend"]

    cas = ctx.conversion_actions
    if cas is not None:
        enabled = [c for c in cas if getattr(c, "status", "") == "ENABLED"]
        if spends and not enabled:
            return [
                Finding(
                    check_id="no_conversion_tracking",
                    family="conversion_tracking",
                    severity="warning",
                    at_risk=round(total_cost, 2),
                    spend_segment="__account__",
                    facts={
                        "cost": round(total_cost, 2),
                        "clicks": int(clicks),
                        "currency": cur,
                        "reason": "no_action",
                    },
                    evidence={"cost": round(total_cost, 2), "enabled_actions": 0},
                )
            ]
        if enabled and spends and clicks > 0 and conv == 0:
            return [
                Finding(
                    check_id="zero_conversions",
                    family="conversion_tracking",
                    severity="warning",
                    at_risk=round(total_cost, 2),
                    spend_segment="__account__",
                    facts={
                        "cost": round(total_cost, 2),
                        "clicks": int(clicks),
                        "currency": cur,
                        "reason": "zero_conv",
                    },
                    evidence={"cost": round(total_cost, 2), "conversions": conv},
                )
            ]
        return []

    # Fallback без данных о действиях: метрическая эвристика.
    if spends and clicks > 0 and conv == 0:
        return [
            Finding(
                check_id="no_conversion_tracking",
                family="conversion_tracking",
                severity="warning",
                at_risk=round(total_cost, 2),
                spend_segment="__account__",
                facts={"cost": round(total_cost, 2), "clicks": int(clicks), "currency": cur},
                evidence={"cost": round(total_cost, 2), "clicks": int(clicks), "conversions": conv},
            )
        ]
    return []


def check_impression_share(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Impression-share: потеря показов по БЮДЖЕТУ (budget_constrained, семья budget) vs по РАНГУ
    (rank_constrained, семья rsa). Классифицируем ТОЛЬКО когда данные полны (Σ долей ≈ 1.0) — иначе
    proto3-zero «нет данных», молчим. Неденежные (долю в деньги без допущений не переводим)."""
    rows = ctx.is_rows or []
    tol = thr.get("is_data_tolerance", 0.02)
    lost_min = thr.get("is_lost_min", 0.10)
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for r in rows:
        if getattr(r, "channel_type", "SEARCH") not in ("SEARCH", "SHOPPING"):
            continue
        b = float(getattr(r, "budget_lost_is", 0.0) or 0.0)
        rk = float(getattr(r, "rank_lost_is", 0.0) or 0.0)
        s = float(getattr(r, "search_is", 0.0) or 0.0)
        if not (1.0 - tol <= s + b + rk <= 1.0 + tol):
            continue  # нет данных — не выдумываем
        name = getattr(r, "campaign_name", "")
        if b >= lost_min and b >= rk:
            out.append(
                Finding(
                    check_id="is_budget_constrained",
                    family="budget",
                    severity="info",
                    at_risk=0.0,
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation=None,  # бюджет — прозой (rule #3)
                    facts={"campaign": name, "budget_lost": round(b * 100), "currency": cur},
                    evidence={"budget_lost_is": round(b, 4), "rank_lost_is": round(rk, 4)},
                )
            )
        elif rk >= lost_min:
            out.append(
                Finding(
                    check_id="is_rank_constrained",
                    family="rsa",
                    severity="info",
                    at_risk=0.0,
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation=None,
                    facts={"campaign": name, "rank_lost": round(rk * 100), "currency": cur},
                    evidence={"rank_lost_is": round(rk, 4)},
                )
            )
    return out


# Стратегии БЕЗ конверсионного автобиддинга (N1.2). Whitelist-детект: неизвестная/новая стратегия →
# НЕ флагуем (fail-safe, не гадаем). Знание-детектор из README FGRibreau/mcp-google-ads (MIT);
# сам сервер (31 write-тул) отвергнут — берём только правило «BROAD без Smart Bidding = риск».
_NON_SMART_BIDDING = frozenset(
    {"MANUAL_CPC", "MANUAL_CPM", "MANUAL_CPV", "TARGET_SPEND", "PERCENT_CPC"}
)


def check_broad_unmanaged(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """BROAD-ключи в ENABLED-кампании со стратегией БЕЗ Smart Bidding (семья bidding, N1.2):
    широкое соответствие без конверсионного сигнала льёт нецелевой трафик. ТОЛЬКО прозой —
    сужение соответствия/минус-слова требуют курации, не one-tap (rule #3/#6). Нет данных о
    стратегии (ctx.bidding_by_name пуст / кампания не найдена) → молчим (fail-safe).
    НЕДЕНЕЖНАЯ (ревью 2026-07-08): это риск КОНФИГУРАЦИИ, а не измеренный слив — BROAD-расход
    может конвертить; деньги-под-риском считают проверки слива (wasteful_keyword/spend_no_conv),
    иначе headline задваивал бы тот же ключ и инфлировался конвертящим расходом. Полный
    BROAD-расход остаётся в facts/evidence для прозы."""
    if not ctx.bidding_by_name:
        return []
    status_by_name = {name: status for (name, status), _m in _campaign_rows(report)}
    cur = getattr(report, "currency", "")
    agg: dict[str, dict] = {}
    for dims, m in _breakdown_rows(report, "keyword"):
        campaign, _ad_group, _kw, match_type = (list(dims) + ["", "", "", ""])[:4]
        if match_type != "BROAD" or status_by_name.get(campaign) != "ENABLED":
            continue
        b = ctx.bidding_by_name.get(campaign)
        if b is None or getattr(b, "strategy_type", "") not in _NON_SMART_BIDDING:
            continue
        a = agg.setdefault(
            campaign, {"cost": 0.0, "n": 0, "strategy": getattr(b, "strategy_type", "")}
        )
        a["cost"] += float(m.cost)
        a["n"] += 1
    out: list[Finding] = []
    for campaign, a in agg.items():
        if a["cost"] < thr["broad_min_spend"]:
            continue
        out.append(
            Finding(
                check_id="broad_unmanaged",
                family="bidding",
                severity="warning",
                at_risk=0.0,  # риск конфигурации, не измеренный слив (см. docstring)
                spend_segment=None,
                target_campaign=campaign,
                suggested_operation=None,  # курация соответствий/минусов — НЕ one-tap
                facts={
                    "campaign": campaign,
                    "strategy_type": a["strategy"],
                    "cost": round(a["cost"], 2),
                    "kw_count": a["n"],
                    "currency": cur,
                },
                evidence={
                    "cost": round(a["cost"], 2),
                    "kw_count": a["n"],
                    "strategy_type": a["strategy"],
                },
            )
        )
    return out


def check_duplicate_conversions(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Консервативный детект двойного счёта конверсий (N1.5): ≥2 ENABLED PRIMARY-действий ОДНОЙ
    содержательной категории пишут в столбец «Конверсии» → Smart Bidding учится на задвоенных
    данных. Только прозой, без кнопки. Без живых conversion_actions/категорий → молчим."""
    cas = ctx.conversion_actions
    if not cas:
        return []
    by_cat: dict[str, int] = {}
    for c in cas:
        if getattr(c, "status", "") != "ENABLED" or not getattr(c, "primary_for_goal", False):
            continue
        cat = str(getattr(c, "category", "") or "")
        if cat in ("", "DEFAULT", "UNSPECIFIED", "UNKNOWN"):
            continue  # категория-заглушка — не повод для флага (консервативно)
        by_cat[cat] = by_cat.get(cat, 0) + 1
    out: list[Finding] = []
    for cat, n in sorted(by_cat.items()):
        if n >= 2:
            out.append(
                Finding(
                    check_id="duplicate_conversions",
                    family="conversion_tracking",
                    severity="info",
                    at_risk=0.0,
                    spend_segment=None,
                    suggested_operation=None,
                    facts={"category": cat, "count": n},
                    evidence={"category": cat, "count": n},
                )
            )
    return out


# Порядок проверок = порядок запуска (на рендер не влияет — там сортировка по важности).
_CHECKS = (
    check_no_conversion_tracking,
    check_duplicate_conversions,
    check_spend_no_conv,
    check_kill_rule,
    check_high_cpa,
    check_wasteful_keyword,
    check_wasteful_search_term,
    check_broad_unmanaged,
    check_impression_share,
    check_budget_imbalance,
    check_low_ctr_ad,
    check_single_campaign,
)

# N1.0a: полный реестр эмитируемых проверок check_id → (family, severity) — входит в версию
# score-модели (правка семьи/severity чека тоже сдвигает измерение!). Дрейф с телами проверок
# ловит tests/test_audit_engine.py (regex по исходнику): новый чек ОБЯЗАН попасть и сюда.
CHECK_REGISTRY: dict[str, tuple[str, str]] = {
    "spend_no_conv": ("waste", "warning"),
    "high_cpa": ("waste", "warning"),
    "kill_rule": ("waste", "warning"),
    "wasteful_keyword": ("keywords", "warning"),
    "wasteful_search_term": ("keywords", "warning"),
    "low_ctr_ad": ("rsa", "info"),
    "budget_imbalance": ("budget", "info"),
    "single_campaign": ("structure", "info"),
    "no_conversion_tracking": ("conversion_tracking", "warning"),
    "zero_conversions": ("conversion_tracking", "warning"),
    "is_budget_constrained": ("budget", "info"),
    "is_rank_constrained": ("rsa", "info"),
    "broad_unmanaged": ("bidding", "warning"),
    "duplicate_conversions": ("conversion_tracking", "info"),
}
CHECK_IDS = frozenset(CHECK_REGISTRY)


def compute_score_model_version(
    *,
    family_weight: dict | None = None,
    severity_mult: dict | None = None,
    nonmoney_intensity: float | None = None,
    grade_bands=None,
    thresholds: dict | None = None,
    check_registry: dict | None = None,
    epoch: int | None = None,
) -> str:
    """N1.0a: стабильная версия score-модели — sha256 (12 hex) от ВСЕХ констант измерения: весов
    семей, множителей severity, порогов, бэндов, реестра проверок (check_id → family/severity) и
    ручной эпохи (SCORE_MODEL_EPOCH — бампается при правке СЕМАНТИКИ тел проверок, которую хэш не
    видит). Любая правка модели → новая версия → тренды между версиями честно «н/д», а не ложная
    дельта (outward-facing деньги). Kwargs — только для тестов чувствительности."""
    reg = check_registry if check_registry is not None else CHECK_REGISTRY
    payload = {
        "family_weight": family_weight if family_weight is not None else FAMILY_WEIGHT,
        "severity_mult": severity_mult if severity_mult is not None else SEVERITY_MULT,
        "nonmoney_intensity": (
            nonmoney_intensity if nonmoney_intensity is not None else NONMONEY_INTENSITY
        ),
        "grade_bands": [list(b) for b in (grade_bands if grade_bands is not None else GRADE_BANDS)],
        "thresholds": thresholds if thresholds is not None else DEFAULT_AUDIT_THRESHOLDS,
        "check_registry": {k: list(v) for k, v in sorted(reg.items())},
        "epoch": epoch if epoch is not None else SCORE_MODEL_EPOCH,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# Константы модели не меняются в рантайме → версия вычисляется один раз при импорте.
SCORE_MODEL_VERSION = compute_score_model_version()


def _intensity(f: Finding, total_spend: float) -> float:
    """Интенсивность находки для штрафа: денежная → доля at_risk/расход (0..1); иначе — фикс."""
    if f.at_risk > 0 and total_spend > 0:
        return max(0.0, min(1.0, f.at_risk / total_spend))
    return NONMONEY_INTENSITY


def _dedup_at_risk(
    findings: list[Finding], total_spend: float, campaign_cost: dict | None = None
) -> float:
    """Деньги-под-риском без двойного счёта (крит.C2), в три шага:
    1) по (кампания, сегмент) → MAX (одинаковые сегменты не суммируются);
    2) по КАМПАНИИ → сумма её сегментов, но CAP по СОБСТВЕННОМУ расходу кампании — расход ключа ⊂
       расход кампании, поэтому «слив кампании» + «дорогой ключ той же кампании» не плюсуются поверх
       (иначе headline завышался, ревью 2026-07-08); независимые ключи внутри капа по-прежнему суммируются;
    3) сумма кампаний → CAP по общему расходу. campaign_cost={имя: расход}; None → шаг-2 CAP пропускаем."""
    seg: dict[tuple, float] = {}
    for f in findings:
        if f.at_risk <= 0:
            continue
        key = (f.target_campaign or "", f.spend_segment or f.check_id)
        seg[key] = max(seg.get(key, 0.0), f.at_risk)
    per_camp: dict[str, float] = {}
    for (camp, _sg), v in seg.items():
        per_camp[camp] = per_camp.get(camp, 0.0) + v
    if campaign_cost:
        for camp, v in list(per_camp.items()):
            cc = campaign_cost.get(camp)
            if cc is not None:
                per_camp[camp] = min(v, round(float(cc), 2))
    total = sum(per_camp.values())
    if total_spend > 0:
        total = min(total, total_spend)
    return round(total, 2)


def build_audit(
    report,
    thresholds: dict | None = None,
    target_cpa: float | None = None,
    *,
    is_rows: list | None = None,
    conversion_actions: list | None = None,
    bidding: list | None = None,
    optimization_score=None,
    recommendations: list | None = None,
    search_terms: list | None = None,
    data_gaps: list | None = None,
) -> AuditResult:
    """Собрать аудит по УЖЕ прочитанным данным. Чистая функция (без сети/SDK). Опц. живые данные
    (is_rows / conversion_actions / bidding / optimization_score / recommendations) — duck-typed,
    None → проверка деградирует мягко. Пустой аккаунт → score None / grade «—» (не фейковые 100).
    score — только КОД. Google-рекомендации/optimization_score — «второе мнение», НЕ в нашем score."""
    thr = {**DEFAULT_AUDIT_THRESHOLDS, **(thresholds or {})}
    totals = report.totals
    total_spend = float(getattr(totals, "cost", 0.0) or 0.0)
    currency = getattr(report, "currency", "") or ""
    cid = str(getattr(report, "customer_id", "") or "")

    # Нативный Google-балл (0..1 → 0..100) — «второе мнение», НЕ в нашем score.
    opt_score = opt_uplift = None
    if optimization_score is not None:
        opt_score = round(float(getattr(optimization_score, "score", 0.0)) * 100)
        opt_uplift = round(float(getattr(optimization_score, "uplift", 0.0)) * 100)
    # Активные Google-рекомендации (тип → число) — показ, не применяем.
    grec: dict[str, int] = {}
    for r in recommendations or []:
        if getattr(r, "dismissed", False):
            continue
        t = str(getattr(r, "type", "") or "")
        if t and t not in ("UNSPECIFIED", "UNKNOWN"):
            grec[t] = grec.get(t, 0) + 1

    impressions = float(getattr(totals, "impressions", 0) or 0)
    has_activity = impressions > 0 or total_spend > 0
    if not has_activity:
        return AuditResult(
            cid,
            currency,
            None,
            "—",
            0.0,
            0.0,
            [],
            {},
            False,
            opt_score,
            opt_uplift,
            grec,
            score_model_version=SCORE_MODEL_VERSION,
            data_gaps=data_gaps,
        )

    campaign_cost = {name: m.cost for (name, _status), m in _campaign_rows(report)}
    bidding_by_name = {getattr(b, "name", ""): b for b in (bidding or [])}
    ctx = _Ctx(
        target_cpa=target_cpa,
        is_rows=is_rows,
        conversion_actions=conversion_actions,
        search_terms=search_terms,
        bidding_by_name=bidding_by_name,
    )

    # N1.3 (ревью): IS-строки прочитаны, но НИ ОДНА не прошла tolerance-гейт (proto3-нули) —
    # check_impression_share промолчит «нет данных», и без этой пометки рендер показал бы
    # budget/rsa «в норме». Пробел честнее: сигнал есть формально, данных в нём нет.
    if data_gaps is not None and is_rows and "impression_share" not in data_gaps:
        tol = thr.get("is_data_tolerance", 0.02)
        usable = any(
            getattr(r, "channel_type", "SEARCH") in ("SEARCH", "SHOPPING")
            and 1.0 - tol
            <= (
                float(getattr(r, "search_is", 0.0) or 0.0)
                + float(getattr(r, "budget_lost_is", 0.0) or 0.0)
                + float(getattr(r, "rank_lost_is", 0.0) or 0.0)
            )
            <= 1.0 + tol
            for r in is_rows
        )
        if not usable:
            data_gaps = [*data_gaps, "impression_share"]

    # N1.5: разрыв измерения — расход есть, а активной PRIMARY-конверсии нет: Smart Bidding и все
    # числа ниже опираются на неполные данные. Только при ЖИВЫХ conversion_actions (None → data gap).
    measurement_gap = False
    if conversion_actions is not None and total_spend >= thr["no_conv_min_spend"]:
        measurement_gap = not any(
            getattr(c, "status", "") == "ENABLED" and getattr(c, "primary_for_goal", False)
            for c in conversion_actions
        )

    findings: list[Finding] = []
    for check in _CHECKS:
        findings.extend(check(report, thr, ctx))

    by_family: dict[str, list[Finding]] = {}
    for f in findings:
        by_family.setdefault(f.family, []).append(f)

    total_penalty = 0.0
    families: dict[str, dict] = {}
    for fam, fs in by_family.items():
        raw_sum = sum(SEVERITY_MULT.get(f.severity, 0.5) * _intensity(f, total_spend) for f in fs)
        penalty = FAMILY_WEIGHT.get(fam, 0.0) * min(1.0, raw_sum)
        total_penalty += penalty
        families[fam] = {
            "count": len(fs),
            "at_risk": _dedup_at_risk(fs, total_spend, campaign_cost),
            "penalty": round(penalty, 2),
        }

    score = max(0, min(100, round(100.0 - total_penalty)))
    at_risk = _dedup_at_risk(findings, total_spend, campaign_cost)
    findings.sort(key=lambda f: (-SEVERITY_MULT.get(f.severity, 0.0), -f.at_risk, not f.one_tap))

    return AuditResult(
        customer_id=cid,
        currency=currency,
        score=score,
        grade=grade_for(score),
        total_spend=round(total_spend, 2),
        at_risk=at_risk,
        findings=findings,
        families=families,
        has_activity=True,
        optimization_score=opt_score,
        optimization_uplift=opt_uplift,
        google_recommendations=grec,
        score_model_version=SCORE_MODEL_VERSION,
        data_gaps=data_gaps,
        measurement_gap=measurement_gap,
    )
