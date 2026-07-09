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
    # Экспертное расширение (2026-07-09): вклад в score для находок, где деньги — это УПУЩЕННАЯ выгода
    # (не потраченное), которую нельзя класть в at_risk (сломает инвариант at_risk ≤ total_spend и
    # дедуп). Пример: is_lost_revenue ставит score_intensity = clamp(lost_revenue/total_spend), at_risk=0.
    # None → интенсивность как раньше (at_risk/spend для денежных, иначе NONMONEY_INTENSITY).
    score_intensity: float | None = None

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
    # Heartbeat: customer.status ∈ {SUSPENDED, CANCELED, CLOSED} → баннер-катастрофа НАД score
    # («показы остановлены»). None — аккаунт активен ИЛИ статус не прочитан (fail-soft). НЕ подавляет
    # score (карточка кампаний остаётся полезной, как measurement_gap).
    account_status: str | None = None
    # Экспертное расширение (2026-07-09): ОЦЕНКА упущенной выгоды (недополученная выручка/конверсии из-за
    # потери impression share по бюджету/рангу) в валюте аккаунта. Это НЕ at_risk (не потраченные деньги,
    # а не заработанные) — показывается ОТДЕЛЬНОЙ строкой «💡 Упущено ~X», помеченной как оценка. 0.0 →
    # нет оценки. В конце dataclass (позиционные конструкторы/тесты не ломаются).
    lost_opportunity: float = 0.0


@dataclass
class _Ctx:
    """Контекст проверок: цель CPA (глоб./пер-кампания) + опц. живые данные (IS/конверсии). duck-typed."""

    target_cpa: float | None = None
    is_rows: list | None = None
    conversion_actions: list | None = None
    search_terms: list | None = None
    bidding_by_name: dict = field(default_factory=dict)
    ad_policy: list | None = (
        None  # Heartbeat: строки модерации объявлений (None → сигнал не прочитан)
    )
    adgroup_structure: list | None = (
        None  # D: строки структуры групп (ключи/RSA per group), None → нет данных
    )
    negative_lists: object | None = (
        None  # D: NegativeListsInfo (кол-во/привязка минус-списков), None → нет данных
    )
    keyword_quality: list | None = (
        None  # B: строки Quality Score ключей (+3 компонента), None → нет данных
    )
    geo_waste: list | None = (
        None  # A: строки geographic_view (кампания/регион/метрики), None → нет данных
    )
    schedule: list | None = (
        None  # A: ячейки час×день (кампания/день/час/метрики), None → нет данных
    )

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
    """Impression-share: потеря показов по БЮДЖЕТУ (семья budget) vs по РАНГУ (rank_constrained, семья
    rsa). Классифицируем ТОЛЬКО когда данные полны (Σ долей ≈ 1.0) — иначе proto3-zero «нет данных», молчим.

    Экспертное расширение (C, 2026-07-09): если кампания С доказанными конверсиями теряет показы по
    БЮДЖЕТУ — это УПУЩЕННАЯ выручка (не потраченное!). Оцениваем консервативно:
      lost_conv = conversions × budget_lost_is / search_is  (search_is ≥ пол — иначе экстраполяция рвётся)
      lost_revenue = lost_conv × ценность конверсии (conv_value/conv, иначе target_cpa, иначе acct_cpa)
    Эмитим is_lost_revenue (warning, at_risk=0 — упущенное НЕ кладём в headline, чтобы не сломать
    инвариант at_risk ≤ расход и дедуп; вклад в score через score_intensity = clamp(lost_rev/расход)).
    Без конверсий / ненадёжный search_is → is_budget_constrained (info, config-сигнал, как раньше).
    Rank-lost — is_rank_constrained (info, rsa): ранг чинится качеством/ставкой, не бюджетом (порог 20%)."""
    rows = ctx.is_rows or []
    tol = thr.get("is_data_tolerance", 0.02)
    lost_min = thr.get("is_lost_min", 0.10)
    rank_min = thr.get("is_rank_lost_min", 0.20)
    search_floor = thr.get("is_search_floor", 0.05)
    cur = getattr(report, "currency", "")
    total_spend = float(getattr(report.totals, "cost", 0.0) or 0.0)
    acct_cpa = float(getattr(report.totals, "cpa", 0.0) or 0.0)
    m_by_name = {name: m for (name, _st), m in _campaign_rows(report)}
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
            m = m_by_name.get(name)
            conv = float(getattr(m, "conversions", 0.0) or 0.0) if m else 0.0
            if conv > 0 and s >= search_floor:  # доказанная способность + надёжный search_is
                lost_conv = conv * b / s
                conv_value = float(getattr(m, "conv_value", 0.0) or 0.0)
                value_per_conv = (
                    conv_value / conv
                    if conv_value > 0
                    else (ctx.target_for(name) or acct_cpa or 0.0)
                )
                lost_rev = round(lost_conv * value_per_conv, 2)
                si = min(1.0, lost_rev / total_spend) if total_spend > 0 else 0.0
                out.append(
                    Finding(
                        check_id="is_lost_revenue",
                        family="budget",
                        severity="warning",
                        at_risk=0.0,  # упущенное ≠ потраченное → в score через score_intensity
                        spend_segment=name,
                        target_campaign=name,
                        suggested_operation=None,  # бюджет — прозой (rule #3)
                        facts={
                            "campaign": name,
                            "budget_lost": round(b * 100),
                            "lost_conv": round(lost_conv, 1),
                            "lost_revenue": lost_rev,
                            "currency": cur,
                        },
                        evidence={
                            "budget_lost_is": round(b, 4),
                            "search_is": round(s, 4),
                            "conversions": round(conv, 2),
                        },
                        score_intensity=si,
                    )
                )
            else:
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
        elif rk >= rank_min:
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


# Статусы модерации объявления, при которых оно НЕ показывается / показывается ограниченно
# (Heartbeat). APPROVED и служебные UNKNOWN/UNSPECIFIED не флагуем.
_IMPAIRED_APPROVAL = frozenset({"DISAPPROVED", "APPROVED_LIMITED", "AREA_OF_INTEREST_ONLY"})


def check_ads_disapproved(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Heartbeat: ENABLED-объявления с impaired approval_status (дизапрув/ограничение) — молча не
    показываются. Агрегат ПО КАМПАНИИ (per-ad затопил бы _AUDIT_MAX_FINDINGS). НЕденежная (at_risk=0):
    это тихая потеря БЕЗ расхода; чинится правкой объявления/посадочной, НЕ мутацией бота (не one-tap).
    Нет данных о модерации (ctx.ad_policy is None) → молчим (fail-safe, как check_broad_unmanaged)."""
    if ctx.ad_policy is None:
        return []
    agg: dict[str, dict] = {}
    for r in ctx.ad_policy:
        status = str(getattr(r, "approval_status", "") or "")
        if status not in _IMPAIRED_APPROVAL:
            continue
        campaign = str(getattr(r, "campaign", "") or "")
        a = agg.setdefault(campaign, {"n": 0, "disapproved": 0, "sample": ""})
        a["n"] += 1
        if status == "DISAPPROVED":
            a["disapproved"] += 1
        if not a["sample"]:
            a["sample"] = str(getattr(r, "ad_group", "") or "")
    out: list[Finding] = []
    for campaign, a in sorted(agg.items()):
        out.append(
            Finding(
                check_id="ads_disapproved",
                family="delivery",
                severity="warning",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=campaign,
                suggested_operation=None,  # правка объявления/посадочной — НЕ мутация бота
                facts={
                    "campaign": campaign,
                    "count": a["n"],
                    "disapproved": a["disapproved"],
                    "ad_group": a["sample"],
                },
                evidence={"count": a["n"], "disapproved": a["disapproved"]},
            )
        )
    return out


def check_zero_impressions(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Heartbeat: ENABLED-кампания с 0 показов за окно — включена, но молчит (сломанные объявления/
    узкий таргет/пустые группы). Тихая потеря БЕЗ расхода. НЕденежная (at_risk=0), прозой, не one-tap.
    Гард активностью аккаунта (totals.impressions>0) — не флагать целиком мёртвый/Draft-аккаунт.
    Новый GAQL не нужен — из campaign-разбивки отчёта."""
    acct_impr = float(getattr(report.totals, "impressions", 0) or 0)
    if acct_impr <= 0:
        return []
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status == "ENABLED" and float(getattr(m, "impressions", 0) or 0) == 0:
            out.append(
                Finding(
                    check_id="zero_impressions",
                    family="delivery",
                    severity="warning",
                    at_risk=0.0,
                    spend_segment=None,
                    target_campaign=name,
                    suggested_operation=None,
                    facts={"campaign": name},
                    evidence={"impressions": 0},
                )
            )
    return out


def check_adgroup_bloat(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """D (claude-ads G03): активные группы с >N ключей — «свалка» тем размывает релевантность и Quality
    Score. Один агрегат (сколько групп + худшая), не спам per-группа. Неденежная (структура), прозой,
    не one-tap. Нет данных структуры (ctx.adgroup_structure=None) → молчим (fail-safe)."""
    rows = ctx.adgroup_structure
    if not rows:
        return []
    cap = int(thr.get("kw_per_group_max", 20))
    bloated = [r for r in rows if int(getattr(r, "kw_count", 0) or 0) > cap]
    if not bloated:
        return []
    worst = max(bloated, key=lambda r: int(getattr(r, "kw_count", 0) or 0))
    return [
        Finding(
            check_id="adgroup_bloat",
            family="structure",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(worst, "campaign", None),
            suggested_operation=None,
            facts={
                "count": len(bloated),
                "cap": cap,
                "worst_group": getattr(worst, "ad_group", ""),
                "worst_kw": int(getattr(worst, "kw_count", 0) or 0),
            },
            evidence={"bloated_groups": len(bloated)},
        )
    ]


def check_rsa_thin(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """D (copilot/North Country): активные группы с <N включённых RSA — мало вариантов для показа/
    оптимизации. Один агрегат (сколько групп + худшая). Неденежная (rsa), прозой. Нет данных → молчим."""
    rows = ctx.adgroup_structure
    if not rows:
        return []
    need = int(thr.get("rsa_min_per_group", 2))
    thin = [r for r in rows if int(getattr(r, "rsa_count", 0) or 0) < need]
    if not thin:
        return []
    worst = min(thin, key=lambda r: int(getattr(r, "rsa_count", 0) or 0))
    return [
        Finding(
            check_id="rsa_thin",
            family="rsa",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(worst, "campaign", None),
            suggested_operation=None,
            facts={
                "count": len(thin),
                "need": need,
                "worst_group": getattr(worst, "ad_group", ""),
                "worst_rsa": int(getattr(worst, "rsa_count", 0) or 0),
            },
            evidence={"thin_groups": len(thin)},
        )
    ]


def check_no_negative_list(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """D (claude-ads G14): в аккаунте с реальным трафиком НЕТ ни одного списка минус-слов (shared_set
    NEGATIVE_KEYWORDS) — базовая гигиена не настроена, нецелевые запросы не отсекаются. Неденежная
    (keywords), прозой. Нет данных (ctx.negative_lists=None) или списки есть → молчим."""
    nl = ctx.negative_lists
    if nl is None or int(getattr(nl, "count", 0) or 0) > 0:
        return []
    if float(getattr(report.totals, "clicks", 0) or 0) <= 0:  # нет трафика → списки и не нужны
        return []
    return [
        Finding(
            check_id="no_negative_list",
            family="keywords",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=None,
            suggested_operation=None,
            facts={"lists": 0},
            evidence={"negative_lists": 0},
        )
    ]


def check_quality_score(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """B (claude-ads G20-G24 + Hassanelsisi): Quality Score дорогих ключей + разложение на 3 компонента
    (Expected CTR / Ad Relevance / Landing Page). qs_low — есть ключи с QS ≤ порога; qs_ctr_below /
    qs_relevance_below / qs_landing_below — доля ключей с компонентом «ниже среднего» ≥ порога (диагноз,
    ЧТО чинить). Гейт proto3-zero: quality_score==0 → «нет данных», ключ не учитываем. Только ключи с
    расходом ≥ kw_min_spend (шум мелких отсекаем). Всё info/неденежное (rsa), прозой. Нет данных → молчим."""
    rows = ctx.keyword_quality
    if not rows:
        return []
    floor = thr.get("kw_min_spend", 3.0)
    qs_fail = int(thr.get("qs_fail", 4))
    below_pct = thr.get("qs_component_below_pct", 0.35)
    considered = [
        r
        for r in rows
        if int(getattr(r, "quality_score", 0) or 0) > 0
        and float(getattr(getattr(r, "metrics", None), "cost", 0.0) or 0.0) >= floor
    ]
    if not considered:
        return []
    n = len(considered)
    out: list[Finding] = []
    low = [r for r in considered if int(r.quality_score) <= qs_fail]
    if low:
        worst = min(low, key=lambda r: (int(r.quality_score), -float(r.metrics.cost)))
        out.append(
            Finding(
                check_id="qs_low",
                family="rsa",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=getattr(worst, "campaign", None),
                suggested_operation=None,
                facts={
                    "count": len(low),
                    "qs_fail": qs_fail,
                    "worst_kw": getattr(worst, "keyword", ""),
                    "worst_qs": int(worst.quality_score),
                },
                evidence={"low_qs_keywords": len(low)},
            )
        )
    ctr_below = [r for r in considered if getattr(r, "expected_ctr", "") == "BELOW_AVERAGE"]
    if ctr_below and len(ctr_below) / n >= below_pct:
        out.append(
            Finding(
                check_id="qs_ctr_below",
                family="rsa",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=None,
                suggested_operation=None,
                facts={"share": round(len(ctr_below) / n * 100), "count": len(ctr_below)},
                evidence={"ctr_below": len(ctr_below)},
            )
        )
    rel_below = [r for r in considered if getattr(r, "ad_relevance", "") == "BELOW_AVERAGE"]
    if rel_below and len(rel_below) / n >= below_pct:
        out.append(
            Finding(
                check_id="qs_relevance_below",
                family="rsa",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=None,
                suggested_operation=None,
                facts={"share": round(len(rel_below) / n * 100), "count": len(rel_below)},
                evidence={"relevance_below": len(rel_below)},
            )
        )
    lp_below = [r for r in considered if getattr(r, "landing_page", "") == "BELOW_AVERAGE"]
    if lp_below and len(lp_below) / n >= below_pct:
        out.append(
            Finding(
                check_id="qs_landing_below",
                family="rsa",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=None,
                suggested_operation=None,
                facts={"share": round(len(lp_below) / n * 100), "count": len(lp_below)},
                evidence={"landing_below": len(lp_below)},
            )
        )
    return out


def check_geo_no_conv(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """A (North Country): регион (LOCATION_OF_PRESENCE) с расходом ≥ порога и 0 конверсий при кликах —
    реальный слив гео. ДЕНЕЖНАЯ (at_risk=cost, семья geo). spend_segment=`geo::{кампания}::{регион}`,
    target_campaign=кампания → дедуп кэпит cost региона расходом кампании (гео ⊂ кампания, не задваивает
    spend_no_conv/wasteful_keyword). Топ-N по расходу (анти-спам). Не one-tap (гео-исключение курируется).
    Нет данных гео (ctx.geo_waste=None) → молчим (fail-safe)."""
    rows = ctx.geo_waste
    if not rows:
        return []
    floor = thr.get("geo_min_spend", 20.0)
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for r in rows:
        m = getattr(r, "metrics", None)
        cost = float(getattr(m, "cost", 0.0) or 0.0)
        if (
            cost >= floor
            and float(getattr(m, "clicks", 0) or 0) > 0
            and float(getattr(m, "conversions", 0) or 0) == 0
        ):
            camp = getattr(r, "campaign", "")
            region = getattr(r, "region", "")
            out.append(
                Finding(
                    check_id="geo_no_conv",
                    family="geo",
                    severity="warning",
                    at_risk=round(cost, 2),
                    spend_segment=f"geo::{camp}::{region}",
                    target_campaign=camp,
                    suggested_operation=None,  # гео-исключение — не one-tap (курация)
                    facts={
                        "campaign": camp,
                        "region": region,
                        "cost": round(cost, 2),
                        "clicks": int(getattr(m, "clicks", 0) or 0),
                        "currency": cur,
                    },
                    evidence={"cost": round(cost, 2), "clicks": int(getattr(m, "clicks", 0) or 0)},
                )
            )
    out.sort(key=lambda f: -f.at_risk)
    return out[: int(thr.get("kw_top_n", 5))]


def check_schedule_waste(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """A (Hassanelsisi heatmap): ячейки час×день с расходом ≥ порога и 0 конверсий при кликах — слив по
    расписанию (dayparting). НЕденежная (at_risk=0, семья geo): расход ячейки ⊂ расход кампании, деньги
    уже меряют слив-проверки; здесь — сигнал «сдвинь ставки по времени». Один агрегат (сколько ячеек +
    худшая). Не one-tap. Нет данных (ctx.schedule=None) → молчим."""
    cells = ctx.schedule
    if not cells:
        return []
    floor = thr.get("schedule_min_spend", 20.0)
    cur = getattr(report, "currency", "")
    waste = [
        c
        for c in cells
        if float(getattr(getattr(c, "metrics", None), "cost", 0.0) or 0.0) >= floor
        and float(getattr(getattr(c, "metrics", None), "clicks", 0) or 0) > 0
        and float(getattr(getattr(c, "metrics", None), "conversions", 0) or 0) == 0
    ]
    if not waste:
        return []
    worst = max(waste, key=lambda c: float(getattr(c.metrics, "cost", 0.0) or 0.0))
    total = round(sum(float(getattr(c.metrics, "cost", 0.0) or 0.0) for c in waste), 2)
    return [
        Finding(
            check_id="schedule_waste",
            family="geo",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(worst, "campaign", None),
            suggested_operation=None,
            facts={
                "count": len(waste),
                "cost": total,
                "worst_day": getattr(worst, "day_of_week", ""),
                "worst_hour": int(getattr(worst, "hour", 0) or 0),
                "currency": cur,
            },
            evidence={"waste_cells": len(waste)},
        )
    ]


def check_manual_bid_high_vol(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Bidding (claude-ads G36/G40): ENABLED-кампания на РУЧНОЙ стратегии (_NON_SMART_BIDDING) с высоким
    объёмом конверсий (> порога) — ручные ставки оставляют деньги на столе, Smart Bidding оптимизировал
    бы лучше. Неденежная (bidding), прозой — смена стратегии требует решения владельца, не one-tap.
    Нет данных о стратегии (ctx.bidding_by_name пуст) → молчим (fail-safe)."""
    if not ctx.bidding_by_name:
        return []
    need = float(thr.get("smart_bid_min_conv", 30))
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED":
            continue
        conv = float(getattr(m, "conversions", 0.0) or 0.0)
        if conv <= need:
            continue
        b = ctx.bidding_by_name.get(name)
        strat = getattr(b, "strategy_type", "") if b is not None else ""
        if strat not in _NON_SMART_BIDDING:
            continue
        out.append(
            Finding(
                check_id="manual_bid_high_vol",
                family="bidding",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=name,
                suggested_operation=None,
                facts={"campaign": name, "conversions": round(conv, 1), "strategy_type": strat},
                evidence={"conversions": round(conv, 2), "strategy_type": strat},
            )
        )
    return out


# Порядок проверок = порядок запуска (на рендер не влияет — там сортировка по важности).
_CHECKS = (
    check_no_conversion_tracking,
    check_duplicate_conversions,
    check_ads_disapproved,
    check_zero_impressions,
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
    check_adgroup_bloat,
    check_rsa_thin,
    check_no_negative_list,
    check_quality_score,
    check_manual_bid_high_vol,
    check_geo_no_conv,
    check_schedule_waste,
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
    "is_lost_revenue": ("budget", "warning"),
    "is_rank_constrained": ("rsa", "info"),
    "broad_unmanaged": ("bidding", "warning"),
    "duplicate_conversions": ("conversion_tracking", "info"),
    "ads_disapproved": ("delivery", "warning"),
    "zero_impressions": ("delivery", "warning"),
    "adgroup_bloat": ("structure", "info"),
    "rsa_thin": ("rsa", "info"),
    "no_negative_list": ("keywords", "info"),
    "qs_low": ("rsa", "info"),
    "qs_ctr_below": ("rsa", "info"),
    "qs_relevance_below": ("rsa", "info"),
    "qs_landing_below": ("rsa", "info"),
    "manual_bid_high_vol": ("bidding", "info"),
    "geo_no_conv": ("geo", "warning"),
    "schedule_waste": ("geo", "info"),
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
    """Интенсивность находки для штрафа: денежная → доля at_risk/расход (0..1); явный score_intensity
    (упущенная выгода, at_risk=0) → он; иначе — фикс NONMONEY_INTENSITY."""
    if f.at_risk > 0 and total_spend > 0:
        return max(0.0, min(1.0, f.at_risk / total_spend))
    if f.score_intensity is not None:
        return max(0.0, min(1.0, f.score_intensity))
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
    ad_policy: list | None = None,
    account_status: str | None = None,
    data_gaps: list | None = None,
    adgroup_structure: list | None = None,
    negative_lists: object | None = None,
    keyword_quality: list | None = None,
    geo_waste: list | None = None,
    schedule: list | None = None,
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

    # Heartbeat: аккаунт приостановлен/отменён/закрыт → баннер-катастрофа (нужен и на мёртвом
    # аккаунте: suspension объясняет отсутствие активности). Иначе None (активен / статус не прочитан).
    acct_status = account_status if account_status in ("SUSPENDED", "CANCELED", "CLOSED") else None

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
            account_status=acct_status,
        )

    campaign_cost = {name: m.cost for (name, _status), m in _campaign_rows(report)}
    bidding_by_name = {getattr(b, "name", ""): b for b in (bidding or [])}
    ctx = _Ctx(
        target_cpa=target_cpa,
        is_rows=is_rows,
        conversion_actions=conversion_actions,
        search_terms=search_terms,
        bidding_by_name=bidding_by_name,
        ad_policy=ad_policy,
        adgroup_structure=adgroup_structure,
        negative_lists=negative_lists,
        keyword_quality=keyword_quality,
        geo_waste=geo_waste,
        schedule=schedule,
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
    # C: суммарная ОЦЕНКА упущенной выгоды (не at_risk!) — из facts находок is_lost_revenue. Отдельная
    # строка карточки «💡 Упущено ~X»; в headline «под риском» НЕ входит (потраченное ≠ недополученное).
    lost_opportunity = round(
        sum(float((f.facts or {}).get("lost_revenue", 0.0) or 0.0) for f in findings), 2
    )
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
        account_status=acct_status,
        lost_opportunity=lost_opportunity,
    )
