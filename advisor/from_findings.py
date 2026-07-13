"""Единственный мост audit.engine.Finding → advisor.rules.Recommendation.

До слияния (2026-07-13) рекомендации рождались в ДВУХ местах: 27 чеков `audit/engine.py` и 6
детекторов `advisor/rules.py`, из которых 5 были копией чеков с чуть другими порогами. Клиент мог
получить два разных ответа на один вопрос, а обучение на 👍/👎 копилось в двух РАЗНЫХ бакетах
(`/advise` писал kind='high_cpa', `/audit` — 'audit_high_cpa', и второй никогда не читался). Теперь
источник один — движок аудита; advisor остаётся тем, что умеет: ранжировать по деньгам (rules),
копить опыт (experience) и мерить эффект (outcome).

Три вещи, которые здесь НЕЛЬЗЯ ломать:
  • kind == check_id (без префиксов) — это КЛЮЧ ОБУЧЕНИЯ. Любой префикс снова расщепит бакет опыта.
  • suggested_operation = f.suggested_operation OR f.advice_operation — метка для outcome-линковки.
    Кнопку «применить» рисует НЕ она, а bot-слой по своему allow-list (_ADVISE_APPLY_OPS), поэтому
    update_bid/update_budget/create_rsa сюда попадают безопасно (golden rule #3: деньги — только по
    прямой команде). Гард: tests/test_audit_engine.test_advice_operation_never_one_tap.
  • topic == family — ОДНА таксономия на аудит и советы. Меню /advise (optimize|keywords|rsa|
    structure) отображается на семьи через ADVISE_TOPIC_FAMILIES, а не наоборот.
"""

from __future__ import annotations

from advisor.rules import Recommendation

# Тема меню /advise → семьи чеков аудита. Семьи вне карты (conversion_tracking, delivery, geo,
# bidding, assets) темой не выбираются, но попадают в /advise «по всем темам» (topics=None) и в
# /audit — прятать поломку измерения только потому, что для неё нет кнопки в меню, нельзя.
ADVISE_TOPIC_FAMILIES: dict[str, frozenset[str]] = {
    "optimize": frozenset({"waste", "budget", "bidding"}),
    "keywords": frozenset({"keywords"}),
    "rsa": frozenset({"rsa"}),
    "structure": frozenset({"structure"}),
}

# Семьи, чей ВЕРДИКТ зависит от ctx-сигналов (живые conversion_action и т.п.). /advise и дайджест
# строят аудит ТОЛЬКО по отчёту (report-only, без доп. запросов к API), а там
# check_no_conversion_tracking уходит в метрическую эвристику: аккаунту с настроенным отслеживанием и
# 0 конверсий она скажет «отслеживания нет» (с ctx тот же вход даёт ДРУГОЙ чек — zero_conversions).
# Плюс at_risk такой находки = ВЕСЬ расход аккаунта ⇒ ложь заняла бы №1 в ранге и в дайджесте, а 👍/👎
# по ней снова расщепили бы бакет опыта надвое. Молчать честнее: точный вердикт даёт /audit (он ctx
# читает). Гард класса — tests/test_advisor.py::test_advise_findings_are_subset_of_audit.
CTX_ONLY_FAMILIES: frozenset[str] = frozenset({"conversion_tracking"})


def families_for_topics(topics) -> set[str] | None:
    """Семьи, попадающие в запрошенные темы. topics пусты/None → None (значит «все семьи»)."""
    if not topics:
        return None
    out: set[str] = set()
    for t in topics:
        out |= ADVISE_TOPIC_FAMILIES.get(str(t), frozenset())
    return out


def to_recommendation(f, lang: str, currency: str) -> Recommendation:
    """Находка аудита → рекомендация advisor (текст — детерминированный audit.render.finding_text;
    LLM его лишь переписывает в advisor.formulate, факты сверяет КОД)."""
    from audit.render import finding_text

    return Recommendation(
        kind=f.check_id,
        topic=f.family,
        severity=f.severity,
        target_campaign=f.target_campaign,
        # ⚠️ Метка для замера эффекта, НЕ путь исполнения (см. докстринг модуля).
        suggested_operation=f.suggested_operation or f.advice_operation,
        facts=f.facts,
        evidence=f.evidence,
        at_risk=f.at_risk,
        body=finding_text(f, lang, currency),
    )


def to_recommendations(
    findings, lang: str, currency: str, *, topics=None, report_only: bool = False
) -> list[Recommendation]:
    """Находки → рекомендации, отфильтрованные по темам меню (topics=None ⇒ все).

    report_only=True — аудит собран БЕЗ ctx-сигналов (путь /advise и дайджеста): выкидываем семьи,
    чей вердикт без ctx недостоверен (CTX_ONLY_FAMILIES)."""
    fams = families_for_topics(topics)
    return [
        to_recommendation(f, lang, currency)
        for f in findings
        if (fams is None or f.family in fams)
        and not (report_only and f.family in CTX_ONLY_FAMILIES)
    ]
