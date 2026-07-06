"""Чистое ядро генерации рекомендаций (advisory) — БЕЗ сети/SDK, полностью тестируемо.

Образец scheduler/anomaly.py: детекторы принимают УЖЕ прочитанные данные (reports.service.ReportData,
scheduler.anomaly.Alert, ads.read.AccountMedians) и возвращают list[Recommendation]. Это ТОЛЬКО
подсказки — рекомендация НЕ создаёт proposal и не меняет аккаунт (golden rule #1/#3): исполнение
любого совета идёт ОТДЕЛЬНОЙ командой через confirm-гейт. `suggested_operation` — advisory-МЕТКА
(для связывания исхода в Слое B), НЕ путь исполнения. Приоритет считает КОД (деньги-под-риском ×
вес важности × множитель опыта), метрики/пороги к LLM НЕ уходят — как rank_clusters (§7).

⚠️ Этот модуль (и весь пакет advisor/) НЕ импортирует ads.mutations / ads.service —
инвариант tests/test_advisor.py::test_advisor_never_imports_mutations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Пороги по умолчанию (переопределяемы per-chat, как UserSettings.alert_thresholds["advisor"]).
DEFAULT_ADVISOR_THRESHOLDS: dict[str, float] = {
    "min_spend": 1.0,  # шумовой порог расхода (в валюте аккаунта) — ниже не рекомендуем
    "pause_min_spend": 5.0,  # расход ENABLED-кампании при 0 конверсий, чтобы флагнуть «впустую»
    "high_cpa_factor": 2.0,  # CPA кампании ≥ factor × средний CPA аккаунта → флаг «дорогая конверсия»
    "budget_share_pct": 60.0,  # доля расхода одной кампании, при которой смотрим её эффективность
    # Тема keywords: ключ с расходом ≥ порога при 0 конверсий → кандидат в минус-слова.
    "kw_min_spend": 3.0,
    "kw_top_n": 5,  # не больше N советов по ключам за раз (анти-спам; берём самые дорогие)
    # Тема rsa: объявление с показами ≥ порога и CTR < factor × средний CTR аккаунта → обновить тексты.
    "min_impressions": 200.0,
    "low_ctr_factor": 0.5,
    # Тема structure: единственная ENABLED-кампания с расходом ≥ порога → рассмотреть разделение.
    "single_campaign_min_spend": 10.0,
}

# Вес важности (severity) для приоритета — деньги-под-риском доминируют, severity лишь модулирует.
_SEVERITY_WEIGHT: dict[str, float] = {"warning": 1.0, "info": 0.5}

# Порог «крупных денег»: suppress (оператор замьютил вид многими 👎) НИКОГДА не прячет совет с
# деньгами-под-риском ≥ этого значения — крупный слив бюджета важнее усталости от вида. Ниже порога
# приглушённый вид отсекается (в валюте аккаунта; для одно-аккаунтного /advise валюта однородна).
SUPPRESS_MONEY_FLOOR: float = 50.0


@dataclass
class Recommendation:
    """Одна рекомендация. `facts` — язык-нейтральные значения для рендера; `evidence` — метрики-
    триггер (база для замера delta в Слое B). rec_uid/body заполняет store/service при персисте
    (ядро остаётся чистым — не генерит uuid и не рендерит текст)."""

    kind: str  # spend_no_conv | high_cpa | budget_imbalance | …
    topic: str  # optimize | keywords | rsa | structure
    severity: str  # warning | info
    priority: float = 0.0  # считает rank_recommendations (КОД)
    target_campaign: str | None = None
    suggested_operation: str | None = None  # advisory-метка для outcome, НЕ путь исполнения
    facts: dict = field(default_factory=dict)  # значения для render.render_recommendation
    evidence: dict = field(default_factory=dict)  # метрики-триггер (для outcome/delta)
    rec_uid: str | None = None  # проставит store при персисте
    body: str = ""  # показанный текст (проставит service после render/formulate)


def _thr(thresholds: dict | None) -> dict:
    return {**DEFAULT_ADVISOR_THRESHOLDS, **(thresholds or {})}


def _breakdown_rows(report, key: str) -> list:
    """Строки разбивки report по ключу (campaign/keyword/ad/…). Пусто, если разбивки нет."""
    b = next((b for b in getattr(report, "breakdowns", []) if b.key == key), None)
    return list(b.rows) if b and b.rows else []


def _campaign_rows(report) -> list:
    """Строки разбивки по кампаниям: [((name, status), Metrics), …]. Пусто, если разбивки нет."""
    return _breakdown_rows(report, "campaign")


# ── Детекторы Темы «optimize» (принимают уже прочитанные данные; ничего не читают) ──
def rec_spend_no_conv(report, thr: dict) -> list[Recommendation]:
    """ENABLED-кампания с расходом ≥ порога и кликами, но 0 конверсий — деньги впустую."""
    out: list[Recommendation] = []
    cur = getattr(report, "currency", "")
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED":
            continue
        if m.cost >= thr["pause_min_spend"] and m.clicks > 0 and m.conversions == 0:
            out.append(
                Recommendation(
                    kind="spend_no_conv",
                    topic="optimize",
                    severity="warning",
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


def rec_high_cpa(report, thr: dict) -> list[Recommendation]:
    """ENABLED-кампания с конверсиями, чей CPA ≥ factor × средний CPA аккаунта — дорогая конверсия.
    Опорный CPA — по totals аккаунта (включает саму кампанию; консервативно, без ложных срабатываний
    на пустых аккаунтах: если totals.cpa == 0 — правило молчит)."""
    acct_cpa = float(getattr(report.totals, "cpa", 0.0) or 0.0)
    if acct_cpa <= 0:
        return []
    factor = thr["high_cpa_factor"]
    cur = getattr(report, "currency", "")
    out: list[Recommendation] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED" or m.conversions <= 0 or m.cost < thr["min_spend"]:
            continue
        if m.cpa >= factor * acct_cpa:
            out.append(
                Recommendation(
                    kind="high_cpa",
                    topic="optimize",
                    severity="warning",
                    target_campaign=name,
                    suggested_operation="update_bid",
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
                        "conversions": m.conversions,
                    },
                )
            )
    return out


def rec_budget_imbalance(report, thr: dict) -> list[Recommendation]:
    """Одна ENABLED-кампания съедает ≥ budget_share_pct расхода, а её эффективность НЕ лучше средней
    (CPA хуже аккаунтного либо 0 конверсий) — повод пересмотреть распределение. ⚠️ Совет ТЕКСТОВЫЙ:
    suggested_operation='update_budget' — лишь метка; бюджет меняется только прямой командой (rule #3)."""
    total_cost = float(getattr(report.totals, "cost", 0.0) or 0.0)
    if total_cost < thr["pause_min_spend"]:
        return []
    acct_cpa = float(getattr(report.totals, "cpa", 0.0) or 0.0)
    cur = getattr(report, "currency", "")
    out: list[Recommendation] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED":
            continue
        share = m.cost / total_cost * 100.0 if total_cost else 0.0
        worse = (m.conversions <= 0) or (acct_cpa > 0 and m.cpa > acct_cpa)
        if share >= thr["budget_share_pct"] and worse:
            out.append(
                Recommendation(
                    kind="budget_imbalance",
                    topic="optimize",
                    severity="info",
                    target_campaign=name,
                    suggested_operation="update_budget",
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
                        "acct_cpa": round(acct_cpa, 2),
                    },
                )
            )
    return out


# ── Тема «keywords» (Фаза 3): дорогие ключи без конверсий → кандидаты в минус-слова ──
def rec_wasteful_keyword(report, thr: dict) -> list[Recommendation]:
    """Ключевые слова с расходом ≥ kw_min_spend и кликами, но 0 конверсий (из разбивки keyword_view).
    Берём топ kw_top_n по расходу (анти-спам). suggested_operation='add_negative_keywords' — метка."""
    out: list[Recommendation] = []
    cur = getattr(report, "currency", "")
    for dims, m in _breakdown_rows(report, "keyword"):
        # dims = (campaign, ad_group, keyword_text, match_type)
        campaign, ad_group, kw_text, match_type = (list(dims) + ["", "", "", ""])[:4]
        if m.cost >= thr["kw_min_spend"] and m.clicks > 0 and m.conversions == 0:
            out.append(
                Recommendation(
                    kind="wasteful_keyword",
                    topic="keywords",
                    severity="warning",
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
    out.sort(key=lambda r: -float(r.evidence.get("cost", 0) or 0))
    return out[: int(thr.get("kw_top_n", 5))]


# ── Тема «rsa» (Фаза 3): объявления с низким CTR → обновить тексты ───────────────────
def rec_low_ctr_ad(report, thr: dict) -> list[Recommendation]:
    """Объявления с показами ≥ min_impressions и CTR < low_ctr_factor × средний CTR аккаунта — повод
    освежить заголовки/описания (RSA). Опора — totals.ctr аккаунта (0 → правило молчит)."""
    acct_ctr = float(getattr(report.totals, "ctr", 0.0) or 0.0)
    if acct_ctr <= 0:
        return []
    cur = getattr(report, "currency", "")
    out: list[Recommendation] = []
    for dims, m in _breakdown_rows(report, "ad"):
        # dims = (campaign, ad_group, ad_id, ad_type)
        campaign, ad_group = (list(dims) + ["", ""])[:2]
        if m.impressions >= thr["min_impressions"] and m.ctr < acct_ctr * thr["low_ctr_factor"]:
            out.append(
                Recommendation(
                    kind="low_ctr_ad",
                    topic="rsa",
                    severity="info",
                    target_campaign=campaign,
                    suggested_operation="create_rsa",
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
                        "cost": round(m.cost, 2),
                    },
                )
            )
    return out


# ── Тема «structure» (Фаза 3): весь трафик в одной кампании → рассмотреть разделение ──
def rec_single_campaign(report, thr: dict) -> list[Recommendation]:
    """Единственная ENABLED-кампания с расходом ≥ single_campaign_min_spend — повод разделить трафик
    (темы/гео/типы соответствия) для контроля ставок и бюджета. suggested_operation=None: совет ведёт
    к СОЗДАНИЮ новой кампании (create_search_campaign, params['campaign_name'] с ДРУГИМ именем) —
    сопоставить исход со старой кампанией нельзя, замер Слоя B для этого вида честно не заводится."""
    enabled = [((n, s), m) for (n, s), m in _campaign_rows(report) if s == "ENABLED"]
    if len(enabled) != 1:
        return []
    (name, _s), m = enabled[0]
    if m.cost < thr["single_campaign_min_spend"]:
        return []
    return [
        Recommendation(
            kind="single_campaign",
            topic="structure",
            severity="info",
            target_campaign=name,
            suggested_operation=None,  # не измеряемо (новая кампания ≠ старая) — не мёртвый матч
            facts={
                "campaign": name,
                "cost": round(m.cost, 2),
                "currency": getattr(report, "currency", ""),
            },
            evidence={"cost": round(m.cost, 2)},
        )
    ]


# Детекторы по темам (Фаза 1: optimize; Фаза 3: keywords/rsa/structure). alerts/medians — задел.
_DETECTORS: dict[str, tuple] = {
    "optimize": (rec_spend_no_conv, rec_high_cpa, rec_budget_imbalance),
    "keywords": (rec_wasteful_keyword,),
    "rsa": (rec_low_ctr_ad,),
    "structure": (rec_single_campaign,),
}


def build_candidates(
    report,
    alerts=None,
    medians=None,
    experience=None,
    *,
    thresholds: dict | None = None,
    topics=None,
) -> list[Recommendation]:
    """Собрать кандидатов по запрошенным темам (topics=None ⇒ все). alerts/medians/experience —
    задел под будущие правила. Чистая функция: детерминирована при фиксированном report."""
    thr = _thr(thresholds)
    wanted = set(topics) if topics else set(_DETECTORS)
    cands: list[Recommendation] = []
    for topic, detectors in _DETECTORS.items():
        if topic not in wanted:
            continue
        for detect in detectors:
            cands.extend(detect(report, thr))
    return cands


def _magnitude(rec: Recommendation) -> float:
    """Деньги-под-риском рекомендации (руб./usd/…): расход из evidence. Больше денег → выше приоритет."""
    return float(rec.evidence.get("cost", 0.0) or 0.0)


def rank_cross_account(items: list[tuple], top_n: int = 5) -> list[tuple]:
    """Кросс-аккаунтный Top-N «где горит сильнее» БЕЗ FX (golden rule #4): суммы РАЗНЫХ валют
    никогда не сравниваются между собой — ключ ранжирования БЕЗРАЗМЕРНЫЙ: вес severity ×
    (деньги-под-риском / общий расход аккаунта) = доля расхода аккаунта под риском. Совет на 4 000
    UAH при расходе 100 000 (4%) уступит совету на 50 USD при расходе 60 (83%) — честно и без
    выдуманных курсов; суммы показываются в валюте своего аккаунта.

    items: (account, currency, account_total_cost, Recommendation). Советы без денег
    (magnitude=0) уходят в хвост по severity (затем rec.priority — там уже сидит опыт Слоя B).
    Чистая детерминированная функция (стабильная сортировка)."""

    def _rel_score(it: tuple) -> float:
        _acct, _cur, total, rec = it
        rel = (_magnitude(rec) / float(total)) if float(total or 0.0) > 0 else 0.0
        return _SEVERITY_WEIGHT.get(rec.severity, 0.5) * rel

    ranked = sorted(
        items,
        key=lambda it: (
            -_rel_score(it),
            -_SEVERITY_WEIGHT.get(it[3].severity, 0.5),
            -float(it[3].priority or 0.0),
            str(it[0]),
            it[3].kind,
            it[3].target_campaign or "",
        ),
    )
    return ranked[: max(1, int(top_n))]


def rank_recommendations(recs: list[Recommendation], experience=None) -> list[Recommendation]:
    """priority = вес severity × (1 + деньги-под-риском) × множитель опыта; отсев suppress-видов.
    experience (Слой B) — ДВУХУРОВНЕВЫЙ dict: {kind: {...}} (account-wide, как раньше) И
    {(kind, target_campaign): {...}} (пер-кампанийная усталость); None ⇒ веса 1.0.
    Детерминированно (образец rank_clusters): опыт — посчитанный КОДОМ множитель, не сырьё для LLM.

    suppress приглушает НАДОЕВШИЙ оператору вид (много 👎 / устойчивый 🙈-игнор) — на любом из
    двух уровней, но НЕ прячет совет с деньгами-под-риском ≥ SUPPRESS_MONEY_FLOOR: 3 👎 по одной
    кампании не должны молча скрыть крупный слив бюджета. Money-floor guard действует для ОБОИХ
    уровней. Пер-кампанийный ключ гасит совет только по СВОЕЙ кампании."""
    exp = experience or {}
    ranked: list[Recommendation] = []
    for r in recs:
        e_kind = exp.get(r.kind) or {}
        e_camp = exp.get((r.kind, r.target_campaign or "")) or {}
        suppressed = bool(e_kind.get("suppress")) or bool(e_camp.get("suppress"))
        if suppressed and _magnitude(r) < SUPPRESS_MONEY_FLOOR:
            continue
        weight = max(
            0.2,
            min(2.0, float(e_kind.get("weight", 1.0)) * float(e_camp.get("weight", 1.0))),
        )
        r.priority = round(
            _SEVERITY_WEIGHT.get(r.severity, 0.5) * (1.0 + _magnitude(r)) * weight, 2
        )
        ranked.append(r)
    # Стабильный порядок: по приоритету убыв., затем по kind+кампании (детерминизм при равных).
    return sorted(ranked, key=lambda r: (-r.priority, r.kind, r.target_campaign or ""))
