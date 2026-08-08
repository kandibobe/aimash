from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

import mcp_server.tools_read as tools_read
from operations.pacing import ActiveBudgetPlan
from operations.types import BudgetPlanInput
from reports.pacing import fetch_period_spend_micros
from reports.period import custom


DRAFT = "7753643025"


def _plan() -> ActiveBudgetPlan:
    return ActiveBudgetPlan(
        plan_uid="plan_august",
        spec=BudgetPlanInput(
            customer_id=DRAFT,
            scope_type="campaign",
            scope_id="123",
            name="August campaign plan",
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            currency="USD",
            planned_spend_micros=3_100_000_000,
            monthly_ceiling_micros=3_500_000_000,
            source="api",
        ),
    )


async def _direct_read(fn, *args, **kwargs):
    kwargs.pop("label", None)
    kwargs.pop("account", None)
    return fn(*args, **kwargs)


async def _async_value(value):
    return value


@pytest.mark.parametrize(
    ("today", "sample_sufficient", "alert"),
    [
        (date(2026, 8, 10), True, True),
        (date(2026, 8, 4), False, False),
    ],
)
async def test_check_budget_pacing_alert_requires_five_completed_days(
    monkeypatch, today, sample_sufficient, alert
):
    import operations.pacing as pacing

    async def _find(*args, **kwargs):  # noqa: ARG001
        return _plan()

    async def _today(*args, **kwargs):  # noqa: ARG001
        return today

    monkeypatch.setattr(tools_read, "ensure_read_allowed", lambda account: None)
    monkeypatch.setattr(tools_read, "build_client_async", lambda account: _async_value(object()))
    monkeypatch.setattr(tools_read, "account_today", _today)
    monkeypatch.setattr(pacing, "find_active_budget_plan", _find)
    monkeypatch.setattr(tools_read, "run_ads_read_call", _direct_read)
    monkeypatch.setattr(
        tools_read, "fetch_period_spend_micros", lambda *args, **kwargs: 1_200_000_000
    )
    result = await tools_read.check_budget_pacing(account=DRAFT, campaign_id="123")
    row = result["rows"][0]
    assert result["ok"] is True
    assert row["spent"] == 1200.0
    assert row["plan"] == 3100.0
    assert row["plan_to_date"] > 0
    assert row["forecast_overspend"] == row["forecast"] - row["plan"]
    assert row["remaining_plan"] == 1900.0
    assert row["forecast"] > row["plan"] * 1.10
    assert row["sample_sufficient"] is sample_sufficient
    assert row["alert"] is alert
    assert result["alert"] is alert
    assert row["proposal_created"] is False


async def test_check_budget_pacing_without_plan_does_not_query_google(monkeypatch):
    import operations.pacing as pacing

    async def _find(*args, **kwargs):  # noqa: ARG001
        return None

    async def _today(*args, **kwargs):  # noqa: ARG001
        return date(2026, 8, 10)

    async def _unexpected(*args, **kwargs):  # pragma: no cover
        raise AssertionError("Google spend must not be queried without a confirmed plan")

    monkeypatch.setattr(tools_read, "ensure_read_allowed", lambda account: None)
    monkeypatch.setattr(tools_read, "build_client_async", lambda account: _async_value(object()))
    monkeypatch.setattr(tools_read, "account_today", _today)
    monkeypatch.setattr(pacing, "find_active_budget_plan", _find)
    monkeypatch.setattr(tools_read, "run_ads_read_call", _unexpected)
    result = await tools_read.check_budget_pacing(account=DRAFT)
    assert result["ok"] is True
    assert result["has_plan"] is False
    assert result["pacing_message"] == "месячный план не задан"
    assert result["alert"] is False
    assert result["proposal_created"] is False


def test_fetch_period_spend_uses_search_stream_without_limit(monkeypatch):
    captured = {}

    class _Service:
        def search_stream(self, *, customer_id, query):
            captured.update(customer_id=customer_id, query=query)
            return [
                SimpleNamespace(
                    results=[
                        SimpleNamespace(metrics=SimpleNamespace(cost_micros=100)),
                        SimpleNamespace(metrics=SimpleNamespace(cost_micros=250)),
                    ]
                )
            ]

    client = SimpleNamespace(get_service=lambda name: _Service())
    monkeypatch.setattr("reports.pacing.ensure_read_allowed", lambda account: None)
    total = fetch_period_spend_micros(
        client,
        DRAFT,
        custom(date(2026, 8, 1), date(2026, 8, 9)),
        campaign_id="123",
    )
    assert total == 350
    assert captured["customer_id"] == DRAFT
    assert "segments.date BETWEEN '2026-08-01' AND '2026-08-09'" in captured["query"]
    assert "campaign.id = 123" in captured["query"]
    assert "LIMIT" not in captured["query"]


def test_budget_pacing_is_registered_as_read_tool():
    assert tools_read.READ_TOOL_FUNCS["check_budget_pacing"] is tools_read.check_budget_pacing
