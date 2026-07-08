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

# Макс. штраф score НА СЕМЬЮ проверок (Σ = 100). Семьи без реализованных в P1 проверок
# (bidding/geo/assets) присутствуют как задел будущих фаз — без находок дают 0 штрафа.
FAMILY_WEIGHT: dict[str, float] = {
    "waste": 30.0,
    "conversion_tracking": 20.0,
    "budget": 12.0,
    "bidding": 10.0,
    "keywords": 10.0,
    "rsa": 8.0,
    "structure": 6.0,
    "geo": 2.0,
    "assets": 2.0,
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
    "is_lost_min": 0.10,  # потеря impression-share (по бюджету/рангу) ≥ 10% → флаг
    "is_data_tolerance": 0.02,  # |Σ долей − 1.0| ≤ tol → данные полны (иначе proto3-zero «нет данных»)
    "broad_min_spend": 5.0,  # N1.2: BROAD-ключи без Smart Bidding — флаг от этого расхода на кампанию
}


# N1.0a (ревью 2026-07-08): ручная эпоха score-модели. Веса/пороги/реестр проверок хэшируются
# автоматически (audit.engine.compute_score_model_version), но СЕМАНТИКУ тела проверки (формулу
# at_risk, условия срабатывания) хэш не видит. Правишь смысл существующего чека БЕЗ смены его
# check_id/family/severity/порогов → БАМПНИ эпоху, иначе тренд покажет ложную дельту клиенту.
SCORE_MODEL_EPOCH: int = 1


def grade_for(score: float | None) -> str:
    """Буква по score (0..100). None → «—» (нет активности)."""
    if score is None:
        return "—"
    for floor, letter in GRADE_BANDS:
        if score >= floor:
            return letter
    return "F"
