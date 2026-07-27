"""core.llm_budget cost-cap (A.3): дневной потолок СТОИМОСТИ. Граница + fail-open при непрочитанной
трате. Реальную трату мокаем (or_activity.fetch_daily_cost_usd) — без сети."""

from __future__ import annotations

import pytest

from core import llm_budget, or_activity
from core.config import settings


def _mock_spent(monkeypatch: pytest.MonkeyPatch, value: float | None) -> None:
    async def _fake(*, client=None):  # noqa: ANN001, ANN202
        return value

    monkeypatch.setattr(or_activity, "fetch_daily_cost_usd", _fake)


def setup_function() -> None:
    llm_budget.reset()


async def test_cap_off_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_daily_cost_cap_usd", 0.0)
    _mock_spent(monkeypatch, 9999.0)  # трата огромна, но потолок выключен
    await llm_budget.check_daily_cost_cap()  # не бросает


async def test_under_cap_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_daily_cost_cap_usd", 10.0)
    _mock_spent(monkeypatch, 5.0)
    await llm_budget.check_daily_cost_cap()  # 5 < 10 → OK
    st = await llm_budget.daily_cost_status()
    assert st["cap_usd"] == 10.0 and st["spent_usd"] == 5.0
    assert abs(st["pct"] - 0.5) < 1e-9


async def test_at_cap_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_daily_cost_cap_usd", 10.0)
    _mock_spent(monkeypatch, 10.0)  # граница: >= потолка → отказ (fail-closed)
    with pytest.raises(llm_budget.LLMCostCapExceededError):
        await llm_budget.check_daily_cost_cap()


async def test_just_under_cap_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_daily_cost_cap_usd", 10.0)
    _mock_spent(monkeypatch, 9.99)
    await llm_budget.check_daily_cost_cap()  # 9.99 < 10 → OK (граница строгая)


async def test_unreadable_spend_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_daily_cost_cap_usd", 10.0)
    _mock_spent(monkeypatch, None)  # OpenRouter недоступен → fail-OPEN (не бросает)
    await llm_budget.check_daily_cost_cap()
    st = await llm_budget.daily_cost_status()
    assert st["spent_usd"] is None and st["pct"] == 0.0
