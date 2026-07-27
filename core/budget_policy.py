"""PolicyEngine — единый источник политик бюджета/ставок (P0).

Автоматические проверки ДО создания черновика мутации (policy-гейт в
mcp_server.propose.build_proposal):

  • Δ бюджета >20% (mode=percent/amount) → BLOCKED — PolicyExceeded
  • Δ бюджета >15% (mode=percent/amount) → WARNING — близко к лимиту
  • Δ ставки >30% → WARNING (ставки менее строгие, чем бюджеты)
  • CPA vs target CPA >2× → HIGH
  • Cooldown (кампанию уже меняли <24h назад) → WARNING
  • Дубликат операции (совпадает имя кампании + mode + ±5%) → HIGH

Все проверки — fail-closed: сбой чтения (нет кэша, нет данных) НЕ блокирует мутацию
(избыточная блокировка хуже, чем risk=unknown при наличии confirm-гейта). Но лог пишется.

Интеграция: `build_proposal` в `mcp_server/propose.py` — вызов `check_proposal_policy`
после `compute_new_micros` и до `save_proposal`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from core.logging import log

# ── Пороги ────────────────────────────────────────────────────────────────────────
_BUDGET_DELTA_BLOCK_PCT: float = 20.0  # Δ≥20% → BLOCKED
_BUDGET_DELTA_WARN_PCT: float = 15.0  # Δ≥15% → WARNING (близко к лимиту)
_BID_DELTA_WARN_PCT: float = 30.0  # Δ≥30% → WARNING (ставки мягче)

_CPA_TARGET_HIGH: float = 2.0  # CPA ≥ target × 2 → HIGH
_CPA_TARGET_MEDIUM: float = 1.3  # CPA ≥ target × 1.3 → MEDIUM

_COOLDOWN_HOURS: float = 24.0

# Повторяющиеся режимы, где ±5% считается дубликатом
_DUPE_DELTA_PCT: float = 5.0

# Weekly rolling budget cap (PRO-R2, аудит 2026-07-27): максимальный суммарный рост
# дневного бюджета кампании за скользящие 7 дней. Защита от каскадных повышений:
# если агент 5 раз поднимет бюджет на 19% (каждый раз чуть меньше 20% блокировки),
# итоговый рост за неделю составит 2.4× — а это уже неконтролируемый расход.
# Порог 2.0× от ИСХОДНОГО бюджета на начало периода (3× = +200% за неделю).
_WEEKLY_BUDGET_CAP_MULTIPLIER: float = 3.0  # максимум ×3 от начального бюджета за неделю

# Blast Radius (P0, аудит 2026-07-27): абсолютный потолок разового ПРИРОСТА дневного
# бюджета кампании. Защита от каскадных повышений, каждое из которых чуть ниже
# _BUDGET_DELTA_BLOCK_PCT (20%) — 5 повышений на 19% каждое дают +140% от базы,
# а не 2.4×, как weekly cap, но всё равно могут быть внезапны для клиента.
# Значение в micros — дефолт $300 (из core.config.daily_budget_increase_limit_units).
# 0 ⇒ лимит выключен (только Δ% PolicyEngine).
_DAILY_BUDGET_INCREASE_LIMIT_MICROS: int = _micros_from_units(300.0)  # lazy init-заглушка


# ── Результат проверки ─────────────────────────────────────────────────────────────
class RiskLevel:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


@dataclass
class PolicyResult:
    """Результат одной проверки политики."""

    allowed: bool  # False = блокирует создание черновика
    risk: str = RiskLevel.LOW  # low/medium/high/blocked
    reason: str | None = None  # человекочитаемая причина для пользователя
    code: str | None = None  # машинный код (для тестов/аудита)


@dataclass
class PolicyCheckSet:
    """Сводный результат всех проверок для одной операции."""

    allowed: bool = True
    checks: list[PolicyResult] = field(default_factory=list)
    max_risk: str = RiskLevel.LOW

    def add(self, result: PolicyResult) -> None:
        self.checks.append(result)
        if not result.allowed:
            self.allowed = False
        risk_order = {
            RiskLevel.LOW: 0,
            RiskLevel.MEDIUM: 1,
            RiskLevel.HIGH: 2,
            RiskLevel.BLOCKED: 3,
        }
        if risk_order.get(result.risk, 0) > risk_order.get(self.max_risk, 0):
            self.max_risk = result.risk

    def blocked_reason(self) -> str | None:
        """Первая причина блокировки для показа пользователю."""
        for ch in self.checks:
            if not ch.allowed:
                return ch.reason
        return None


# ── Вычислители ────────────────────────────────────────────────────────────────────


def _micros_from_units(units: float) -> int:
    """Вспомогательная: единицы валюты → micros (1 единица = 1_000_000 micros)."""
    return int(units * 1_000_000)


def _pct_change(old_micros: int, new_micros: int) -> float:
    """Абсолютное процентное изменение (0..∞)."""
    if old_micros <= 0:
        return 0.0
    return abs(new_micros - old_micros) / old_micros * 100.0


def check_budget_delta(
    campaign_name: str,
    old_micros: int,
    new_micros: int,
    *,
    currency: str | None = None,
) -> PolicyResult:
    """Δ бюджета >20% → BLOCKED; >15% → WARNING.

    Для increase/decrease. Работает на процентах, а не на абсолютных значениях —
    защищает от аномальных прыжков на любом размере бюджета.
    """
    delta = _pct_change(old_micros, new_micros)
    cur_units = f"{old_micros / 1_000_000:.2f}"
    new_units = f"{new_micros / 1_000_000:.2f}"

    if delta >= _BUDGET_DELTA_BLOCK_PCT:
        return PolicyResult(
            allowed=False,
            risk=RiskLevel.BLOCKED,
            reason=(
                f"Бюджет кампании «{campaign_name}» меняется на {delta:.0f}% "
                f"({cur_units} → {new_units} — это >{_BUDGET_DELTA_BLOCK_PCT:.0f}%). "
                f"Политика блокирует изменения бюджета более {_BUDGET_DELTA_BLOCK_PCT:.0f}% за раз. "
                f"Измени бюджет на меньшую величину или сделай несколько шагов."
            ),
            code="budget_delta_exceeded",
        )
    if delta >= _BUDGET_DELTA_WARN_PCT:
        return PolicyResult(
            allowed=True,
            risk=RiskLevel.MEDIUM,
            reason=(
                f"Бюджет кампании «{campaign_name}» меняется на {delta:.0f}% "
                f"({cur_units} → {new_units} — >{_BUDGET_DELTA_WARN_PCT:.0f}%). "
                f"Близко к лимиту {_BUDGET_DELTA_BLOCK_PCT:.0f}%. Будь внимателен."
            ),
            code="budget_delta_near_limit",
        )
    return PolicyResult(allowed=True, risk=RiskLevel.LOW)


def check_bid_delta(
    campaign_name: str,
    old_micros: int,
    new_micros: int,
    *,
    currency: str | None = None,
) -> PolicyResult:
    """Δ ставки >30% → WARNING (ставки менее строгие, чем бюджеты)."""
    delta = _pct_change(old_micros, new_micros)
    if delta >= _BID_DELTA_WARN_PCT:
        return PolicyResult(
            allowed=True,
            risk=RiskLevel.MEDIUM,
            reason=(
                f"Ставка по «{campaign_name}» меняется на {delta:.0f}% "
                f"({old_micros / 1_000_000:.4f} → {new_micros / 1_000_000:.4f}). "
                f"Значительное изменение ({_BID_DELTA_WARN_PCT:.0f}%+)."
            ),
            code="bid_delta_large",
        )
    return PolicyResult(allowed=True, risk=RiskLevel.LOW)


def check_cpa_vs_target(
    campaign_name: str,
    current_cpa_micros: int | None,
    target_cpa_micros: int | None,
) -> PolicyResult:
    """CPA > target × 2 → HIGH; CPA > target × 1.3 → MEDIUM.

    None в CPA/target — недостаточно данных, отдаём LOW (не блокируем,
    только confirm-гейт).
    """
    if current_cpa_micros is None or target_cpa_micros is None or target_cpa_micros <= 0:
        return PolicyResult(allowed=True, risk=RiskLevel.LOW)
    ratio = current_cpa_micros / target_cpa_micros
    if ratio >= _CPA_TARGET_HIGH:
        return PolicyResult(
            allowed=True,
            risk=RiskLevel.HIGH,
            reason=(
                f"CPA кампании «{campaign_name}» в {ratio:.1f}× выше таргета "
                f"({current_cpa_micros / 1_000_000:.2f} vs {target_cpa_micros / 1_000_000:.2f}). "
                f"Высокий риск неэффективного расхода до оптимизации."
            ),
            code="cpa_above_target_2x",
        )
    if ratio >= _CPA_TARGET_MEDIUM:
        return PolicyResult(
            allowed=True,
            risk=RiskLevel.MEDIUM,
            reason=(
                f"CPA кампании «{campaign_name}» в {ratio:.1f}× выше таргета "
                f"({current_cpa_micros / 1_000_000:.2f} vs {target_cpa_micros / 1_000_000:.2f})."
            ),
            code="cpa_above_target_1_3x",
        )
    return PolicyResult(allowed=True, risk=RiskLevel.LOW)


def _cap_multiplier_warning(current_micros: int, new_micros: int) -> PolicyResult | None:
    """Проверить, не превысит ли новый бюджет ×3 от исходного за неделю.

    Это консервативная оценка без БД за прошлые изменения: сравниваем РАЗОВОЕ
    изменение. Если оно уже превышает ×3 — блокируем сразу. Для каскадного
    контроля (5 раз по +19%) нужна сверка с audit-log (TODO: Волна 2).
    """
    if current_micros > 0 and new_micros >= current_micros * _WEEKLY_BUDGET_CAP_MULTIPLIER:
        return PolicyResult(
            allowed=False,
            risk=RiskLevel.BLOCKED,
            reason=(
                f"Новый бюджет в {new_micros / 1_000_000:.2f} более чем в "
                f"{_WEEKLY_BUDGET_CAP_MULTIPLIER:.0f}× превышает текущий "
                f"({current_micros / 1_000_000:.2f}). Еженедельный лимит: "
                f"не более ×{_WEEKLY_BUDGET_CAP_MULTIPLIER:.0f} от одного изменения."
            ),
            code="weekly_cap_exceeded",
        )
    return None


def check_cooldown(
    campaign_name: str,
    last_mutated_hours_ago: float | None,
) -> PolicyResult:
    """Проверить cooldown (PRO-R5): кампанию меняли <4ч назад? WARNING.

    Facebook Ads блокирует изменения на 3 дня, Stripe — 1 час на изменение
    лимита. 4 часа — баланс для Google Ads: не даёт каскадных правок,
    но оставляет оперативность для экстренных оптимизаций.
    last_mutated_hours_ago=None — данных нет, не блокируем.
    """
    if last_mutated_hours_ago is not None and last_mutated_hours_ago < 4.0:
        return PolicyResult(
            allowed=True,
            risk=RiskLevel.MEDIUM,
            reason=(
                f"Кампанию «{campaign_name}» уже меняли {last_mutated_hours_ago:.1f}ч назад. "
                "Cooldown: не более 1 изменения бюджета за 4 часа."
            ),
            code="cooldown_active",
        )
    return PolicyResult(allowed=True, risk=RiskLevel.LOW)


def check_duplicate(
    operation: str,
    campaign_name: str,
    mode: str,
    new_micros: int,
    *,
    last_change_micros: int | None = None,
) -> PolicyResult:
    """Дубликат: та же кампания + тот же mode + ±5% от прошлого изменения.

    last_change_micros — новое значение ПОСЛЕДНЕЙ apply_update_budget/apply_update_bid
    для этой кампании (из audit-log). None ⇒ нет истории, не дубликат.
    """
    if last_change_micros is None:
        return PolicyResult(allowed=True, risk=RiskLevel.LOW)
    delta = _pct_change(last_change_micros, new_micros)
    if delta <= _DUPE_DELTA_PCT:
        return PolicyResult(
            allowed=True,
            risk=RiskLevel.HIGH,
            reason=(
                f"Операция «{operation}» для «{campaign_name}» почти совпадает с предыдущей "
                f"(расхождение {delta:.1f}%) — возможно, повторный вызов."
            ),
            code="possible_duplicate",
        )
    return PolicyResult(allowed=True, risk=RiskLevel.LOW)


# ── Публичный вход ─────────────────────────────────────────────────────────────────


def check_proposal_policy(
    operation: str,
    campaign_name: str,
    old_micros: int | None,
    new_micros: int | None,
    *,
    mode: str | None = None,
    current_cpa_micros: int | None = None,
    target_cpa_micros: int | None = None,
    last_change_micros: int | None = None,
    last_mutated_hours_ago: float | None = None,
    weekly_change_total_micros: int | None = None,
    currency: str | None = None,
) -> PolicyCheckSet:
    """Главный вход PolicyEngine. Проверяет все применимые политики для операции.

    Аргументы:
      operation — 'update_budget' | 'update_bid' | 'update_keyword_bid'
      campaign_name — имя кампании для сообщений
      old_micros — текущее значение (микро-единицы)
      new_micros — предлагаемое значение (микро-единицы)
      mode — 'increase_by_percent' | 'increase_by_amount' | 'decrease_*' | 'set_to'
      current_cpa_micros — опционально, текущий CPA
      target_cpa_micros — опционально, целевой CPA (из кампании/профиля)
      last_change_micros — опционально, предыдущее изменённое значение
      currency — код валюты аккаунта (для отображения)

    Returns PolicyCheckSet.allowed==False ⇒ ProposalRefused (блокировка).
    """
    result = PolicyCheckSet()
    _log = log

    if new_micros is None or old_micros is None or old_micros <= 0:
        _log.debug("policy: %s %s — недостаточно данных (old=%s)", operation, campaign_name, old_micros)
        result.add(PolicyResult(allowed=True, risk=RiskLevel.LOW))
        return result

    # 1) Δ бюджета
    if operation == "update_budget":
        result.add(check_budget_delta(campaign_name, old_micros, new_micros, currency=currency))

    # 2) Δ ставки
    if operation in ("update_bid", "update_keyword_bid"):
        result.add(check_bid_delta(campaign_name, old_micros, new_micros, currency=currency))

    # 3) CPA vs target (для денежных операций если данные есть)
    if current_cpa_micros is not None:
        result.add(check_cpa_vs_target(campaign_name, current_cpa_micros, target_cpa_micros))

    # 3.5) Weekly rolling cap (PRO-R2): разовый скачок >×3 от текущего
    if operation == "update_budget" and new_micros is not None and old_micros is not None:
        cap_warn = _cap_multiplier_warning(int(old_micros), int(new_micros))
        if cap_warn is not None:
            result.add(cap_warn)

    # 3.6) Cooldown (PRO-R5): кампанию уже меняли <4ч назад?
    if last_mutated_hours_ago is not None:
        result.add(check_cooldown(campaign_name, last_mutated_hours_ago))

    # 3.7) Rolling weekly cap (PRO-R7): суммарный рост за 7 дней >×3 от начального?
    if operation == "update_budget" and old_micros is not None and new_micros is not None \
        and weekly_change_total_micros is not None and weekly_change_total_micros > 0:
        total_new = int(old_micros) + int(weekly_change_total_micros)
        if total_new >= int(old_micros) * _WEEKLY_BUDGET_CAP_MULTIPLIER:
            result.add(PolicyResult(
                allowed=False,
                risk=RiskLevel.BLOCKED,
                reason=(
                    f"Суммарный рост бюджета «{campaign_name}» за 7 дней "
                    f"({weekly_change_total_micros / 1_000_000:.2f}) превышает "
                    f"×{_WEEKLY_BUDGET_CAP_MULTIPLIER:.0f} от исходного "
                    f"({old_micros / 1_000_000:.2f}). Еженедельный лимит исчерпан."
                ),
                code="weekly_rolling_cap_exceeded",
            ))

    # 4) Дубликат
    if mode in ("increase_by_amount", "increase_by_percent", "set_to"):
        result.add(
            check_duplicate(operation, campaign_name, mode or "", new_micros, last_change_micros=last_change_micros)
        )

    # Лог сводного результата
    if not result.allowed:
        reason = result.blocked_reason() or "политика заблокировала операцию"
        _log.info("policy BLOCKED: %s %s — %s", operation, campaign_name, reason)
    elif result.max_risk == RiskLevel.HIGH:
        _log.info("policy HIGH: %s %s (risk=high, но не блокирует)", operation, campaign_name)
    elif result.max_risk == RiskLevel.MEDIUM:
        _log.debug("policy MEDIUM: %s %s", operation, campaign_name)

    return result