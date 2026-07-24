"""Ранжирование рекомендаций — чистое ядро (БЕЗ сети/SDK/LLM), полностью тестируемо.

Что рекомендовать, решает ОДИН источник — движок аудита (`audit/engine.py`, 27 чеков); мост —
`advisor/from_findings.py`. Здесь остаётся то, чего у аудита нет: приоритет по деньгам-под-риском,
множитель накопленного опыта (`advisor/experience.py`, 👍/👎/🙈 + вердикты замеров) и кросс-
аккаунтный Top-N для дайджеста. Своих детекторов у advisor больше НЕТ — до 2026-07-13 их было 6, из
них 5 дублировали чеки аудита с другими порогами (клиент получал два ответа на один вопрос, а опыт
копился в двух разных бакетах). История и инварианты слияния — `advisor/from_findings.py`.

Это ТОЛЬКО подсказки: рекомендация НЕ создаёт proposal и не меняет аккаунт (golden rule #1/#3) —
исполнение любого совета идёт ОТДЕЛЬНОЙ командой через confirm-гейт. `suggested_operation` —
advisory-МЕТКА (для связывания исхода в Слое B), НЕ путь исполнения. Приоритет считает КОД
(деньги-под-риском × вес важности × множитель опыта), метрики/пороги к LLM НЕ уходят — как
rank_clusters (§7).

⚠️ Этот модуль (и весь пакет advisor/) НЕ импортирует ads.mutations / ads.service —
инвариант tests/test_advisor.py::test_advisor_never_imports_mutations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Пороги РАНЖИРОВАНИЯ (не детекции — та вся в audit.thresholds). Отдельный словарь намеренно:
# audit.DEFAULT_AUDIT_THRESHOLDS хэшируется в SCORE_MODEL_VERSION, и подмешивание в него advisor-
# порога рвало бы тренды score при правке, которая на score вообще не влияет.
ADVISOR_RANK_THRESHOLDS: dict[str, float] = {
    # 2.6: порог «крупных денег» для suppress (см. SUPPRESS_MONEY_FLOOR) — переопределяем per-chat,
    # как остальные пороги: у клиента дочерние в USD/UAH/KES/PLN, единый «50» означает ~13× разброс
    # значимости. Значение — В ВАЛЮТЕ АККАУНТА (FX не выдумываем, golden rule #4).
    "suppress_money_floor": 50.0,
}

# Вес важности (severity) для приоритета — деньги-под-риском доминируют, severity лишь модулирует.
# Ф8: critical (уровень аудита — нет трекинга / 3×-Kill) зеркалит audit.thresholds.SEVERITY_MULT.
# Без этой строки .get(sev, 0.5) уронил бы critical-находку НИЖЕ warning: ранжирование советов
# разошлось бы с сортировкой карточки на самой важной находке.
_SEVERITY_WEIGHT: dict[str, float] = {"critical": 1.5, "warning": 1.0, "info": 0.5}

# Порог «крупных денег»: suppress (оператор замьютил вид многими 👎/🙈) НИКОГДА не прячет совет с
# деньгами-под-риском ≥ этого значения — крупный слив бюджета важнее усталости от вида. Ниже порога
# приглушённый вид отсекается (в валюте аккаунта). Константа — ДЕФОЛТ и якорь для тестов/импортов;
# живое значение берётся из ADVISOR_RANK_THRESHOLDS['suppress_money_floor'] (переопределяемо per-chat).
SUPPRESS_MONEY_FLOOR: float = 50.0


@dataclass
class Recommendation:
    """Одна рекомендация (проекция audit.engine.Finding — см. advisor.from_findings). `facts` —
    язык-нейтральные значения для рендера; `evidence` — метрики-триггер (база для замера delta в
    Слое B). rec_uid проставляет store при персисте, body — маппер (audit.render.finding_text)."""

    kind: str  # == audit.engine.Finding.check_id (spend_no_conv | high_cpa | …) — КЛЮЧ ОБУЧЕНИЯ
    topic: str  # == audit.engine.Finding.family (waste | keywords | rsa | conversion_tracking | …)
    severity: str  # warning | info
    priority: float = 0.0  # считает rank_recommendations (КОД)
    target_campaign: str | None = None
    suggested_operation: str | None = None  # advisory-метка для outcome, НЕ путь исполнения
    facts: dict = field(default_factory=dict)  # значения для audit.render.finding_text
    evidence: dict = field(default_factory=dict)  # метрики-триггер (для outcome/delta)
    rec_uid: str | None = None  # проставит store при персисте
    body: str = ""  # показанный текст (проставит маппер, LLM в formulate может переписать)
    # Деньги-под-риском по расчёту аудита (Finding.at_risk): перерасход над средним CPA у high_cpa,
    # весь расход у «слива». None → находка неденежная ИЛИ пришла не из аудита.
    at_risk: float | None = None


def _thr(thresholds: dict | None) -> dict:
    return {**ADVISOR_RANK_THRESHOLDS, **(thresholds or {})}


def _magnitude(rec: Recommendation) -> float:
    """Сила денежного сигнала рекомендации (в валюте аккаунта). Больше денег → выше приоритет.

    Это НЕ at_risk аудита: у трёх находок (budget_imbalance, low_ctr_ad, single_campaign) at_risk
    намеренно 0 — их деньги уже посчитаны в другом сегменте, класть их в headline «Под риском»
    значило бы задвоить. Но для РАНЖИРОВАНИЯ ноль недопустим: с _magnitude=0 эти три получают
    priority=0.5 и с жёсткими срезами (/advise MAX_RECS=5, дайджест top_n=5) не показываются вообще
    — перекос бюджета, сегодня обычно первый в дайджесте, просто исчез бы. Поэтому фолбэк на расход
    самой сущности (evidence['cost']) — сколько денег ПРОХОДИТ через кампанию/группу/ключ.
    """
    if rec.at_risk:
        return float(rec.at_risk)
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


def rank_recommendations(
    recs: list[Recommendation], experience=None, *, thresholds: dict | None = None
) -> list[Recommendation]:
    """priority = вес severity × (1 + деньги-под-риском) × множитель опыта; отсев suppress-видов.
    experience (Слой B) — ДВУХУРОВНЕВЫЙ dict: {kind: {...}} (account-wide, как раньше) И
    {(kind, target_campaign): {...}} (пер-кампанийная усталость); None ⇒ веса 1.0.
    Детерминированно (образец rank_clusters): опыт — посчитанный КОДОМ множитель, не сырьё для LLM.

    suppress приглушает НАДОЕВШИЙ оператору вид (много 👎 / устойчивый 🙈-игнор) — на любом из
    двух уровней, но НЕ прячет совет с деньгами-под-риском ≥ money-floor: 3 👎 по одной кампании
    не должны молча скрыть крупный слив бюджета. Money-floor guard действует для ОБОИХ уровней;
    2.6: порог берётся из thresholds['suppress_money_floor'] (per-chat, в валюте аккаунта),
    дефолт — SUPPRESS_MONEY_FLOOR. Пер-кампанийный ключ гасит совет только по СВОЕЙ кампании.

    ⚠️ Отсев suppress — про СОВЕТЫ (/advise, дайджест), не про ДИАГНОСТИКУ. /audit сознательно НЕ
    зовёт эту функцию: там порядок находок задаёт движок (деньги-под-риском), и приглушённый вид
    обязан остаться на экране — иначе аудит показал бы «score 62, waste −18» без единой строки,
    объясняющей откуда −18 (штраф по семье считается по ПОЛНОМУ набору находок, а money-floor у
    ~18 из 27 чеков не защищает: at_risk = 0 по построению).
    Инвариант: tests/test_advisor.py::test_audit_does_not_rank_or_suppress."""
    exp = experience or {}
    floor = float(_thr(thresholds).get("suppress_money_floor", SUPPRESS_MONEY_FLOOR))
    ranked: list[Recommendation] = []
    for r in recs:
        e_kind = exp.get(r.kind) or {}
        e_camp = exp.get((r.kind, r.target_campaign or "")) or {}
        suppressed = bool(e_kind.get("suppress")) or bool(e_camp.get("suppress"))
        if suppressed and _magnitude(r) < floor:
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
