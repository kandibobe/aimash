"""Константы аудита: пороги проверок + модель health-score. Всё считает КОД (golden rule #4/#6).

Health-score — ЛИНЕЙНАЯ модель с потолком штрафа НА СЕМЬЮ проверок: `penalty(family) =
FAMILY_WEIGHT[family] × min(1, Σ_findings SEVERITY_MULT[sev] × intensity)`, `score = 100 − Σ penalty`.
FAMILY_WEIGHT суммируются в 100 (полный провал всех семей → 0). Ранняя экспоненциальная зарисовка
(100·exp(−Σ/K)) заменена этой: она объяснима клиенту и ПИНУЕТСЯ golden-fixture тестом
(tests/test_audit_engine.py) — правка веса сразу ловится тестом, а не молча сдвигает всем grade (крит.C1).

Пороги — В ВАЛЮТЕ АККАУНТА (FX не выдумываем, golden rule #4); переопределяемы per-chat
(как advisor DEFAULT_ADVISOR_THRESHOLDS / UserSettings.alert_thresholds).
"""

from __future__ import annotations

# Макс. штраф score НА СЕМЬЮ проверок (Σ = 100). Семьи без реализованных проверок дают 0 штрафа.
# Heartbeat (2026-07): добавлена семья delivery=8 (дизапрувы + 0-показов — тихая потеря без расхода).
# Экспертное расширение (2026-07-09): гео стало ДЕНЕЖНОЙ семьёй (geo_no_conv/schedule_waste), rsa/
# structure принимают Quality Score / SKAG / RSA-покрытие / brand-isolation. Ре-баланс профинансирован
# из bidding (broad_unmanaged — слабейшая evidence) и пустого assets (задел без проверок → честный 0):
# geo 2→8, rsa 6→7, structure 4→5; доноры delivery 8→6, bidding 8→4, assets 2→0. Σ доноров −8 = Σ
# получателей +8. waste/conversion_tracking/keywords НЕ тронуты → golden-баллы A–F не сдвигаются. Смена
# вектора ротирует SCORE_MODEL_VERSION (веса в хэше) → тренд честно «н/д» через версию (N1.0a).
FAMILY_WEIGHT: dict[str, float] = {
    "waste": 30.0,
    "conversion_tracking": 20.0,
    "budget": 10.0,
    "keywords": 10.0,
    "geo": 8.0,
    "rsa": 7.0,
    "delivery": 6.0,
    "structure": 5.0,
    "bidding": 4.0,
    "assets": 0.0,
}

# Множитель важности находки. warning доминирует, info лишь модулирует.
SEVERITY_MULT: dict[str, float] = {"warning": 1.0, "info": 0.4}

# Интенсивность НЕденежной находки (у денежной intensity = clamp(at_risk / spend)).
# 0.5 → две warning-находки в семье насыщают её потолок (min(1, Σ)); одна = половина штрафа.
NONMONEY_INTENSITY: float = 0.5

# Границы буквенной оценки (score 0..100). Пустой аккаунт → score None, grade "—" (не фейковые 100).
GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (90.0, "A"),
    (80.0, "B"),
    (65.0, "C"),
    (50.0, "D"),
    (0.0, "F"),
)

# Пороги проверок (валюта аккаунта; переопределяемы per-chat). Зеркалят advisor, где пересекается.
DEFAULT_AUDIT_THRESHOLDS: dict[str, float] = {
    "min_spend": 1.0,  # шумовой порог расхода — ниже не флагуем
    "pause_min_spend": 5.0,  # ENABLED-кампания: расход при 0 конверсий → «впустую»
    "high_cpa_factor": 2.0,  # CPA кампании ≥ factor × средний по аккаунту → «дорогая конверсия»
    "kill_cpa_factor": 3.0,  # «3× Kill Rule»: CPA ≥ factor × ЦЕЛЬ (только при заданном target_cpa)
    "budget_share_pct": 60.0,  # доля расхода одной кампании, при которой смотрим эффективность
    "kw_min_spend": 3.0,  # ключ с расходом ≥ порога при 0 конверсий → кандидат в минус-слова
    "kw_top_n": 5,  # не больше N ключей за раз (анти-спам; самые дорогие)
    "min_impressions": 200.0,  # объявление: показов ≥ порога для оценки CTR
    "low_ctr_factor": 0.5,  # CTR < factor × средний по аккаунту → освежить тексты
    "single_campaign_min_spend": 10.0,  # единственная ENABLED-кампания с расходом ≥ порога
    "no_conv_min_spend": 10.0,  # аккаунт: расход есть, конверсий 0 → подозрение на трекинг
    "is_lost_min": 0.10,  # потеря impression-share ПО БЮДЖЕТУ ≥ 10% → флаг (North Country/Hassanelsisi)
    "is_rank_lost_min": 0.20,  # C: потеря IS ПО РАНГУ ≥ 20% → флаг (ранг чинится качеством, не бюджетом)
    "is_search_floor": 0.05,  # C: пол search_is для оценки упущенного (иначе impr/search_is → взрыв)
    "is_data_tolerance": 0.02,  # |Σ долей − 1.0| ≤ tol → данные полны (иначе proto3-zero «нет данных»)
    "broad_min_spend": 5.0,  # N1.2: BROAD-ключи без Smart Bidding — флаг от этого расхода на кампанию
    "kw_per_group_max": 20,  # D: >N активных ключей в группе → «свалка» (G03: ≤10 норма, >20 fail)
    "rsa_min_per_group": 2,  # D: <N активных RSA в группе → тонкое покрытие (North Country ≥2-3)
    "qs_fail": 4,  # B: Quality Score ≤ N (при cost ≥ kw_min_spend) → низкий (claude-ads G20: ≤4 fail)
    "qs_component_below_pct": 0.35,  # B: доля ключей с компонентом QS «ниже среднего» ≥ N → флаг (G22-24)
    "smart_bid_min_conv": 30,  # bidding: ручная стратегия при > N конв/период → Smart Bidding лучше (G40)
    "geo_min_spend": 20.0,  # A: регион с расходом ≥ N и 0 конверсий → слив гео (North Country)
    "schedule_min_spend": 20.0,  # A: час×день с расходом ≥ N и 0 конверсий → слив по расписанию
}


# N1.0a (ревью 2026-07-08): ручная эпоха score-модели. Веса/пороги/реестр проверок хэшируются
# автоматически (audit.engine.compute_score_model_version), но СЕМАНТИКУ тела проверки (формулу
# at_risk, условия срабатывания) хэш не видит. Правишь смысл существующего чека БЕЗ смены его
# check_id/family/severity/порогов → БАМПНИ эпоху, иначе тренд покажет ложную дельту клиенту.
# Эпоха 2 (2026-07-09): экспертное расширение — новый механизм score_intensity (упущенная выгода IS
# влияет на балл при at_risk=0) + семантика усиленных чеков (high_cpa 2.0→1.5× и т.п.) хэшу не видна.
# Эпоха 3 (2026-07-13, Ф0): анти-ложноположительные правила claude-ads. Тела трёх чеков сменили
# смысл БЕЗ правки check_id/family/severity/порогов ⇒ хэш их НЕ видит (проверено: версия осталась
# e92402b21117), а находок стало объективно меньше — без бампа тренд сравнил бы несравнимое:
#   • broad_unmanaged  — legacy BMM («+ключ») больше не флажится; добавлена ветка «BROAD под Smart
#     Bidding без единого минус-слова» (G17, вторая половина — раньше не ловилась вовсе);
#   • no_negative_list — минусы ПРЯМО на кампании считаются гигиеной наравне со shared-списком (G15);
#   • adgroup_bloat    — ключи считаются по показам за период и дедупятся по тексту (G03).
SCORE_MODEL_EPOCH: int = 3


def grade_for(score: float | None) -> str:
    """Буква по score (0..100). None → «—» (нет активности)."""
    if score is None:
        return "—"
    for floor, letter in GRADE_BANDS:
        if score >= floor:
            return letter
    return "F"
