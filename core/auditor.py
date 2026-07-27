"""Второй LLM-промпт-аудитор для контроля рисковых мутаций.

Проблема: когда GoogleAds Worker решает изменить бюджет/ставку, он не всегда видит
риски, которые очевидны аудитору с другой «температурой» и жёстким промптом на экономию.

Решение (лёгкий вариант — без второго агента):
  Перед execute_approved_action вызывается `audit_proposal()` — второй LLM-промпт,
  который анализирует план и возвращает risk + обоснование:
    - risk=low    → авто-исполнение (если DRY_RUN=false)
    - risk=medium → исполнение с пометкой в лог
    - risk=high   → перенаправление в #approvals-and-audits к человеку

Использование:
  from core.auditor import audit_proposal, AuditResult

  result = audit_proposal(proposal_json, account_context)
  if result.risk == "high":
      redirect_to_approvals(proposal_json, result.reason)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuditResult:
    """Результат проверки финансовым аудитором."""

    risk: str  # "low" | "medium" | "high"
    reason: str  # человекочитаемое обоснование (1-2 предложения)
    warnings: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)


# ── Эвристический аудитор (фаза 1, без LLM) ─────────────────────────


def heuristic_audit(
    proposal: dict[str, Any], account_context: dict[str, Any]
) -> AuditResult:
    """Эвристическая проверка плана по правилам (без LLM-вызова).

    Проверяет:
      1. Бюджет не превышает дневной лимит аккаунта
      2. Δ не превышает 20% за шаг (PolicyEngine)
      3. Не паузим кампанию с активными конверсиями
      4. Нет дублирующихся операций (anti-double-spend)

    Это легковесная проверка (микросекунды), работает всегда.
    Основной аудит через LLM — см. audit_via_llm().
    """
    warnings: list[str] = []
    risk_score = 0  # 0-10

    operation = proposal.get("operation", "")
    params = proposal.get("params", {})
    current_metrics = account_context.get("current_metrics", {})

    # 1. Проверка budget change
    if operation in ("budget_change", "propose_budget_change"):
        new_budget = float(params.get("new_budget_daily", 0))
        old_budget = float(params.get("old_budget_daily", 1))
        delta_pct = abs(new_budget - old_budget) / max(old_budget, 0.01) * 100

        if delta_pct > 50:
            risk_score += 5
            warnings.append(f"Δ бюджета {delta_pct:.0f}% > 50% (подозрительно большой скачок)")

        daily_limit = account_context.get("daily_budget_limit")
        if daily_limit and new_budget > daily_limit:
            risk_score += 3
            warnings.append(f"Бюджет {new_budget:.2f} превышает дневной лимит аккаунта {daily_limit:.2f}")

        # Проверка CPA vs target
        target_cpa = float(account_context.get("target_cpa", 0))
        current_cpa = float(current_metrics.get("cpa", 0))
        if target_cpa > 0 and current_cpa > target_cpa * 2.0:
            risk_score += 5
            warnings.append(
                f"CPA {current_cpa:.2f} в 2× выше target {target_cpa:.2f} — "
                "повышение бюджета при таком CPA категорически не рекомендуется"
            )
        elif target_cpa > 0 and current_cpa > target_cpa * 1.3:
            risk_score += 3
            warnings.append(
                f"CPA {current_cpa:.2f} на 30% выше target {target_cpa:.2f} — "
                "повышение бюджета при плохом CPA рискованно"
            )

    # 2. Проверка паузы кампании
    if operation in ("pause_campaign", "propose_campaign_pause"):
        conversions = float(current_metrics.get("conversions", 0))
        cost = float(current_metrics.get("cost", 0))
        if conversions > 0 and cost > 100:
            risk_score += 4
            warnings.append(
                f"Кампания с {conversions:.0f} конверсиями (расход ${cost:.2f}) — "
                "пауза отключит работающий источник конверсий"
            )

    # 3. Проверка duplicate proposal (anti-double-spend)
    recent_ops = account_context.get("recent_operations", [])
    for recent in recent_ops:
        if recent.get("operation") == operation and recent.get("params") == params:
            risk_score += 8
            warnings.append("ДУБЛИКАТ: такая же операция уже выполнялась недавно")

    # 4. Расчёт финального risk
    if risk_score >= 7:
        risk = "high"
    elif risk_score >= 3:
        risk = "medium"
    else:
        risk = "low"

    reason = "; ".join(warnings) if warnings else "Нарушений не обнаружено"

    return AuditResult(
        risk=risk,
        reason=reason,
        warnings=warnings,
        suggested_actions=_suggest_actions(risk, warnings),
    )


def _suggest_actions(risk: str, warnings: list[str]) -> list[str]:
    """Предложить действия в зависимости от уровня риска."""
    if risk == "low":
        return ["Можно исполнять автоматически"]
    if risk == "medium":
        return ["Добавить в лог мониторинга", "Проверить через 24 часа"]
    return [
        "Отправить в #approvals-and-audits",
        "Дождаться 'да' от человека",
        "Не исполнять автоматически",
    ]


def audit_proposal(
    proposal: dict[str, Any],
    account_context: dict[str, Any],
    *,
    use_llm: bool = False,
) -> AuditResult:
    """Проверить proposal перед исполнением.

    Args:
        proposal: JSON с operation, params, campaign_id
        account_context: контекст аккаунта (лимиты, метрики, история)
        use_llm: включить LLM-аудит (фаза 2, требует API-доступа)

    Returns:
        AuditResult с risk и обоснованием
    """
    result = heuristic_audit(proposal, account_context)

    if use_llm:
        # Фаза 2: дополнительный LLM-промпт
        # llm_result = _audit_via_llm(proposal, account_context)
        # result = _merge_results(result, llm_result)
        pass

    return result


# ── LLM-аудит (фаза 2, заглушка) ─────────────────────────────────────


AUDITOR_SYSTEM_PROMPT = """Ты — финансовый аудитор рекламных кампаний.
Твоя задача: проверить предложенное изменение на риски.
Отвечай ТОЛЬКО JSON: {"risk": "low|medium|high", "reason": "...", "suggestions": [...]}

Критерии оценки:
- Бюджет: Δ > 20% → medium, Δ > 50% → high
- Пауза кампании с конверсиями → medium
- CPA > target × 1.3 при повышении бюджета → medium
- Дубликат недавней операции → high
- Всё остальное → low"""


# Заглушка для будущей интеграции с LLM
async def _audit_via_llm(
    proposal: dict[str, Any], account_context: dict[str, Any]
) -> AuditResult:
    """LLM-аудит (фаза 2). Требует доступа к OpenRouter API."""
    raise NotImplementedError("LLM auditor not yet implemented — use heuristic_audit()")