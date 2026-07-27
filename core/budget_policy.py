"""Budget Policy Middleware — детерминированный гард между LLM и Google Ads API.

Адаптирован из /root/ad-master/src/lib/budget_policy.py для Aimash.
Интегрирован с core.quota, core.auditor, core.config.

Проверяет КАЖДУЮ мутацию до того, как она достигнет Google Ads API:
  - Δ бюджета ≤ MAX_BUDGET_DELTA_PCT (20%)
  - Дневной лимит бюджета
  - Cooldown между изменениями одной кампании (24h)
  - Пауза кампании с активными конверсиями → предупреждение
  - DRY_RUN: все мутации симулируются по умолчанию

Интеграция с аудитором:
  Перед мутацией вызывается heuristic_audit() → если risk=high, операция
  перенаправляется в #approvals-and-audits.

Использование:
  from core.budget_policy import PolicyEngine, PolicyResult
  engine = PolicyEngine()
  result = engine.check_budget_change(
      campaign_id="123", campaign_name="Brand",
      old_budget=400.0, new_budget=500.0, currency="USD",
  )
  if not result.allowed:
      raise PolicyExceeded(result)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from core.logging import log

# ── Config (из core.config.settings, фолбэк на env) ────────────────────

try:
    from core.config import settings

    MAX_BUDGET_DELTA_PCT: float = float(getattr(settings, "MAX_BUDGET_DELTA_PCT", 20))
    MAX_DAILY_BUDGET: float = float(getattr(settings, "MAX_DAILY_BUDGET", 0))
    COOLDOWN_MINUTES: int = int(getattr(settings, "BUDGET_COOLDOWN_MINUTES", 1440))
    PAUSE_WARN_CONVERSIONS: bool = bool(int(getattr(settings, "PAUSE_WARN_CONVERSIONS", 1)))
    DRY_RUN: bool = getattr(settings, "DRY_RUN", True)
    ENVIRONMENT: str = getattr(settings, "ENVIRONMENT", "development")
except Exception:
    import os

    MAX_BUDGET_DELTA_PCT = float(os.getenv("MAX_BUDGET_DELTA_PCT", "20"))
    MAX_DAILY_BUDGET = float(os.getenv("MAX_DAILY_BUDGET", "0"))
    COOLDOWN_MINUTES = int(os.getenv("BUDGET_COOLDOWN_MINUTES", "1440"))
    PAUSE_WARN_CONVERSIONS = bool(int(os.getenv("PAUSE_WARN_CONVERSIONS", "1")))
    DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
    ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# State file для cooldown-трекинга
STATE_DIR = Path.home() / ".hermes" / "admaster"
STATE_FILE = STATE_DIR / "budget_policy_state.json"

# ── Types ───────────────────────────────────────────────────────────────

Platform = Literal["google-ads", "meta-ads", "tiktok-ads"]
Action = Literal["update_budget", "update_cpc", "pause_campaign", "enable_campaign"]


@dataclass
class PolicyResult:
    """Результат проверки политики."""

    allowed: bool
    reason: str
    risk: Literal["low", "medium", "high", "blocked"]
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyCheck:
    """Входные данные для проверки."""

    platform: Platform
    action: Action
    campaign_id: str
    campaign_name: str = ""
    old_value: float = 0.0
    new_value: float = 0.0
    currency: str = "USD"
    conversions_7d: int = 0
    last_change_at: Optional[str] = None


# ── Policy Engine ───────────────────────────────────────────────────────


class PolicyExceeded(Exception):
    """Мутация нарушает бюджетную политику."""

    def __init__(self, result: PolicyResult):
        self.result = result
        super().__init__(result.reason)


class PolicyEngine:
    """Детерминированный проверяльщик политик. Никаких LLM-вызовов — чистые правила."""

    def __init__(self):
        self.max_delta_pct = MAX_BUDGET_DELTA_PCT
        self.max_daily_budget = MAX_DAILY_BUDGET
        self.cooldown_minutes = COOLDOWN_MINUTES
        self.dry_run = DRY_RUN
        STATE_DIR.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────────

    def check_budget_change(
        self,
        campaign_id: str,
        campaign_name: str,
        old_budget: float,
        new_budget: float,
        platform: Platform = "google-ads",
        currency: str = "USD",
    ) -> PolicyResult:
        """Проверить изменение бюджета по всем политикам."""
        return self._evaluate(
            PolicyCheck(
                platform=platform,
                action="update_budget",
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                old_value=old_budget,
                new_value=new_budget,
                currency=currency,
            )
        )

    def check_cpc_change(
        self,
        campaign_id: str,
        campaign_name: str,
        old_cpc: float,
        new_cpc: float,
        platform: Platform = "google-ads",
        currency: str = "USD",
    ) -> PolicyResult:
        """Проверить изменение CPC."""
        return self._evaluate(
            PolicyCheck(
                platform=platform,
                action="update_cpc",
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                old_value=old_cpc,
                new_value=new_cpc,
                currency=currency,
            )
        )

    def check_pause(
        self,
        campaign_id: str,
        campaign_name: str,
        conversions_7d: int,
        platform: Platform = "google-ads",
    ) -> PolicyResult:
        """Проверить возможность паузы кампании."""
        return self._evaluate(
            PolicyCheck(
                platform=platform,
                action="pause_campaign",
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                conversions_7d=conversions_7d,
            )
        )

    def check_with_auditor(
        self,
        campaign_id: str,
        campaign_name: str,
        old_budget: float,
        new_budget: float,
        currency: str = "USD",
        account_context: dict[str, Any] | None = None,
    ) -> PolicyResult:
        """Проверить бюджет с привлечением финансового аудитора (core.auditor).

        Вызывает PolicyEngine.check_budget_change, затем heuristic_audit.
        Результат аудитора влияет на risk-уровень в итоговом PolicyResult.
        """
        policy_result = self.check_budget_change(
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            old_budget=old_budget,
            new_budget=new_budget,
            currency=currency,
        )

        if not policy_result.allowed:
            return policy_result  # уже заблокировано → аудитор не нужен

        try:
            from core.auditor import heuristic_audit

            proposal = {
                "operation": "budget_change",
                "params": {
                    "campaign_id": campaign_id,
                    "new_budget_daily": new_budget,
                    "old_budget_daily": old_budget,
                },
            }
            context = account_context or {"target_cpa": 0, "current_metrics": {}}
            audit_result = heuristic_audit(proposal, context)

            if audit_result.risk == "high":
                return PolicyResult(
                    allowed=True,
                    risk="high",
                    reason=f"Аудитор: {audit_result.reason}",
                    detail={"audit_risk": "high", "audit_warnings": audit_result.warnings},
                )

            return PolicyResult(
                allowed=policy_result.allowed,
                risk=audit_result.risk if audit_result.risk != "low" else policy_result.risk,
                reason=policy_result.reason,
                detail={**policy_result.detail, "audit_risk": audit_result.risk},
            )
        except ImportError:
            log.warning("core.auditor not available — skipping audit step")
            return policy_result

    def record_change(self, campaign_id: str, action: Action) -> None:
        """Записать успешное изменение для cooldown-трекинга."""
        try:
            state = self._load_state()
            state[campaign_id] = {
                "last_action": action,
                "last_change_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_state(state)
        except Exception:
            pass  # Non-critical — cooldown is advisory

    # ── Internal ────────────────────────────────────────────────────

    def _evaluate(self, check: PolicyCheck) -> PolicyResult:
        checks: list[dict[str, Any]] = []

        # 1. Budget delta check
        if check.action in ("update_budget", "update_cpc"):
            if check.old_value > 0:
                delta_pct = abs((check.new_value - check.old_value) / check.old_value * 100)
                if delta_pct > self.max_delta_pct:
                    return PolicyResult(
                        allowed=False,
                        risk="blocked",
                        reason=(
                            f"POLICY_EXCEEDED: Изменение {delta_pct:.1f}% превышает "
                            f"лимит {self.max_delta_pct}%. "
                            f"{check.old_value}{check.currency} → {check.new_value}{check.currency}"
                        ),
                        detail={
                            "rule": "MAX_BUDGET_DELTA_PCT",
                            "delta_pct": round(delta_pct, 1),
                            "limit_pct": self.max_delta_pct,
                            "old_value": check.old_value,
                            "new_value": check.new_value,
                        },
                    )

                if delta_pct > self.max_delta_pct * 0.75:
                    checks.append({
                        "rule": "BUDGET_DELTA_WARNING",
                        "delta_pct": round(delta_pct, 1),
                        "message": (
                            f"Изменение {delta_pct:.1f}% — близко к лимиту "
                            f"{self.max_delta_pct}%"
                        ),
                    })

        # 2. Absolute daily budget cap
        if check.action == "update_budget" and self.max_daily_budget > 0:
            if check.new_value > self.max_daily_budget:
                return PolicyResult(
                    allowed=False,
                    risk="blocked",
                    reason=(
                        f"POLICY_EXCEEDED: Дневной бюджет {check.new_value}{check.currency} "
                        f"превышает максимальный {self.max_daily_budget}{check.currency}"
                    ),
                    detail={
                        "rule": "MAX_DAILY_BUDGET",
                        "limit": self.max_daily_budget,
                        "requested": check.new_value,
                    },
                )

        # 3. Pause with conversions
        if check.action == "pause_campaign" and PAUSE_WARN_CONVERSIONS:
            if check.conversions_7d > 0:
                return PolicyResult(
                    allowed=True,
                    risk="high",
                    reason=(
                        f"⚠️ Кампания `{check.campaign_id}` имеет "
                        f"{check.conversions_7d} конверсий за 7 дней. "
                        f"Пауза остановит конверсии. Подтвердите."
                    ),
                    detail={
                        "rule": "PAUSE_WITH_CONVERSIONS",
                        "conversions_7d": check.conversions_7d,
                    },
                )

        # 4. Cooldown
        last_change = self._get_last_change(check.campaign_id)
        if last_change:
            try:
                last_time = datetime.fromisoformat(last_change["last_change_at"])
                elapsed = (datetime.now(timezone.utc) - last_time).total_seconds() / 60
                if elapsed < self.cooldown_minutes:
                    remaining = self.cooldown_minutes - elapsed
                    checks.append({
                        "rule": "COOLDOWN",
                        "message": (
                            f"Последнее изменение: {elapsed:.0f} мин назад. "
                            f"Cooldown: {remaining:.0f} мин."
                        ),
                    })
            except (ValueError, KeyError):
                pass

        # 5. DRY_RUN
        if self.dry_run:
            checks.append({
                "rule": "DRY_RUN",
                "message": "🔄 DRY_RUN: мутация симулируется, реальные изменения не применяются",
            })

        # ── Assemble result ─────────────────────────────────────────────

        if checks:
            risk_levels = [
                c.get("risk", "low")
                for c in checks
                if "risk" in c and isinstance(c["risk"], str)
            ]
            risk_order = {"low": 0, "medium": 1, "high": 2}
            max_risk: Literal["low", "medium", "high"] = (
                max(risk_levels, key=lambda r: risk_order.get(r, 0))
                if risk_levels
                else "low"
            )

            violations = [c for c in checks if c.get("rule") == "COOLDOWN"]
            if violations:
                return PolicyResult(
                    allowed=True,
                    risk=str(max_risk),
                    reason="; ".join(c["message"] for c in checks),
                    detail={"checks": checks},
                )

        return PolicyResult(
            allowed=True,
            risk="low",
            reason="OK" if not checks else "; ".join(c["message"] for c in checks),
            detail={"checks": checks} if checks else {},
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            if STATE_FILE.exists():
                return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
        return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    def _get_last_change(self, campaign_id: str) -> dict[str, Any] | None:
        state = self._load_state()
        return state.get(campaign_id)


# ── Module-level convenience ────────────────────────────────────────────

_engine: PolicyEngine | None = None


def get_engine() -> PolicyEngine:
    """Ленивый синглтон PolicyEngine."""
    global _engine
    if _engine is None:
        _engine = PolicyEngine()
    return _engine


def check_budget(
    campaign_id: str,
    campaign_name: str,
    old_budget: float,
    new_budget: float,
    currency: str = "USD",
    platform: Platform = "google-ads",
) -> PolicyResult:
    """Удобная функция: проверить бюджет без создания PolicyEngine."""
    return get_engine().check_budget_change(
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        old_budget=old_budget,
        new_budget=new_budget,
        platform=platform,
        currency=currency,
    )


# ── CLI ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = PolicyEngine()

    print("=== Budget Policy Middleware — Self Test ===\n")
    print(
        f"Config: DRY_RUN={DRY_RUN}, MAX_DELTA={MAX_BUDGET_DELTA_PCT}%, "
        f"COOLDOWN={COOLDOWN_MINUTES}min, MAX_DAILY={MAX_DAILY_BUDGET}\n"
    )

    tests = [
        ("Normal 15%", engine.check_budget_change("c1", "Brand", 400, 460)),
        ("Exceeds 50%", engine.check_budget_change("c2", "Search", 400, 600)),
        ("Pause+conv", engine.check_pause("c3", "Display", 5)),
        ("Pause OK", engine.check_pause("c4", "Empty", 0)),
    ]

    for name, result in tests:
        status = "✅" if result.allowed else "❌"
        print(f"{status} {name}: risk={result.risk} | {result.reason}")