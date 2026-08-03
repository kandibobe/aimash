"""Closed-loop outcome logging and the read-only daily checker."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, update

from confirm.store import ConfirmStore, DueOutcome
from core.config import settings
from db.models import AuditLog, Proposal
from db.session import Session, db_dt
from reports.queries import Metrics
from scheduler import jobs
from scheduler.service import _DEFAULT_OUTCOME_CRON, outcome_check_trigger


async def _executing_proposal(operation: str = "update_budget") -> str:
    confirmation_id = uuid.uuid4().hex
    params = {
        "campaign": "Search Core",
        "mode": "increase_by_percent",
        "value": 10,
        "_before": {"before_micros": 10_000_000, "after_micros": 11_000_000},
    }
    if operation == "add_keywords":
        params = {
            "campaign": "Search Core",
            "ad_group": "Main",
            "keywords": ["one", "two"],
            "match_type": "phrase",
        }
    await ConfirmStore().save_proposal(
        confirmation_id=confirmation_id,
        operation=operation,
        customer_id="7753643025",
        params=params,
        summary="Изменить тестовую оптимизацию",
        chat_id=12345,
        user_initiated=True,
    )
    async with Session() as session:
        await session.execute(
            update(Proposal)
            .where(Proposal.confirmation_id == confirmation_id)
            .values(status="executing")
        )
        await session.commit()
    return confirmation_id


async def test_finalize_freezes_expectation_and_applied_audit():
    confirmation_id = await _executing_proposal()
    before = datetime.now(timezone.utc)

    await ConfirmStore().finalize(
        confirmation_id,
        result={"applied": True, "campaign_id": "987", "new_budget_micros": 11_000_000},
    )

    async with Session() as session:
        proposal = (
            await session.execute(
                select(Proposal).where(Proposal.confirmation_id == confirmation_id)
            )
        ).scalar_one()
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.confirmation_id == confirmation_id,
                    AuditLog.status == "applied",
                )
            )
        ).scalar_one()

    assert proposal.status == "applied"
    assert proposal.outcome_state == "pending"
    assert proposal.outcome_context["campaign_id"] == "987"
    assert proposal.outcome_context["direction"] == "increase"
    assert "conversions" in proposal.outcome_context["metrics"]
    assert proposal.outcome_due_at >= db_dt(before + timedelta(days=7))
    assert audit.result["applied"] is True


async def test_due_claim_is_single_use_and_completion_is_terminal():
    confirmation_id = await _executing_proposal("add_keywords")
    store = ConfirmStore()
    await store.finalize(confirmation_id, result={"applied": True, "count": 2})
    now = datetime.now(timezone.utc)
    async with Session() as session:
        await session.execute(
            update(Proposal)
            .where(Proposal.confirmation_id == confirmation_id)
            .values(outcome_due_at=db_dt(now - timedelta(minutes=1)))
        )
        await session.commit()

    first = await store.claim_due_outcomes(now=now)
    second = await store.claim_due_outcomes(now=now)

    row = next(item for item in first if item.confirmation_id == confirmation_id)
    assert row.attempts == 1
    assert all(item.confirmation_id != confirmation_id for item in second)
    assert await store.complete_outcome(
        confirmation_id, result={"verdict": "improved"}, now=now
    )
    assert not await store.complete_outcome(
        confirmation_id, result={"verdict": "worse"}, now=now
    )
    async with Session() as session:
        proposal = (
            await session.execute(
                select(Proposal).where(Proposal.confirmation_id == confirmation_id)
            )
        ).scalar_one()
    assert proposal.outcome_state == "delivered"
    assert proposal.outcome_result["verdict"] == "improved"


def _measurement() -> dict:
    before = Metrics(impressions=100, clicks=10, cost_micros=10_000_000, conversions=1)
    after = Metrics(impressions=130, clicks=14, cost_micros=11_000_000, conversions=2)
    verdict, verdict_text = jobs._outcome_verdict(
        "update_budget", {"direction": "increase"}, before, after
    )
    return {
        "version": 1,
        "confirmation_id": "outcome-1",
        "operation": "update_budget",
        "campaign": "Search Core",
        "campaign_id": "987",
        "applied_on": "2026-07-27",
        "goal": "Увеличить конверсии",
        "before_period": ["2026-07-20", "2026-07-26"],
        "after_period": ["2026-07-27", "2026-08-02"],
        "before": jobs._outcome_metric_values(before),
        "after": jobs._outcome_metric_values(after),
        "delta_pct": {
            "impressions": 30.0,
            "clicks": 40.0,
            "cost": 10.0,
            "conversions": 100.0,
            "cpa": -45.0,
            "roas": None,
        },
        "verdict": verdict,
        "verdict_text": verdict_text,
    }


async def test_daily_checker_sends_and_completes_only_after_delivery(monkeypatch):
    item = DueOutcome(
        confirmation_id="outcome-1",
        operation="update_budget",
        customer_id="7753643025",
        params={"campaign": "Search Core"},
        summary="Увеличен бюджет Search Core",
        chat_id=12345,
        applied_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        context={"window_days": 7, "goal": "Увеличить конверсии"},
        attempts=1,
    )
    events: list[str] = []

    class FakeStore:
        async def claim_due_outcomes(self, **kwargs):
            return [item]

        async def complete_outcome(self, confirmation_id, **kwargs):
            events.append(f"complete:{confirmation_id}")
            return True

        async def retry_outcome(self, confirmation_id, **kwargs):
            events.append(f"retry:{confirmation_id}")
            return True

    async def allowed(chat_id):
        return True

    async def account_allowed(chat_id, customer_id):
        return None

    async def send(bot, chat_id, text, **kwargs):
        assert "Проверка результата оптимизации" in text
        events.append(f"send:{chat_id}")

    async def measure(item, client, now):
        return _measurement()

    async def note(item, result):
        return "Рост конверсий сопровождается приемлемой динамикой эффективности."

    async def build_client(customer_id):
        return SimpleNamespace()

    monkeypatch.setattr(jobs, "ConfirmStore", FakeStore)
    monkeypatch.setattr(jobs, "build_client_async", build_client)
    monkeypatch.setattr(jobs, "_measure_outcome", measure)
    monkeypatch.setattr(jobs, "_outcome_agent_note", note)
    monkeypatch.setattr(jobs.transport, "send_bot_message", send)
    monkeypatch.setattr("core.access.is_whitelisted", allowed)
    monkeypatch.setattr("core.access.ensure_account_allowed_for_user", account_allowed)

    result = await jobs.run_outcome_checker(
        object(), now=datetime(2026, 8, 3, 10, tzinfo=timezone.utc)
    )

    assert result == {"claimed": 1, "delivered": 1, "retrying": 0, "failed": 0}
    assert events == ["send:12345", "complete:outcome-1"]


def test_outcome_prompt_is_compact_and_schedule_is_daily(monkeypatch):
    item = DueOutcome(
        confirmation_id="outcome-1",
        operation="update_budget",
        customer_id="7753643025",
        params={},
        summary="Увеличен бюджет",
        chat_id=1,
        applied_at=datetime.now(timezone.utc),
        context={"window_days": 7},
        attempts=1,
    )
    prompt = jobs._outcome_prompt(item, _measurement())
    assert "7 дней назад была выполнена оптимизация" in prompt
    assert len(prompt) < 4000

    monkeypatch.setattr(settings, "outcome_check_schedule", "0 10 * * *")
    assert str(outcome_check_trigger()) == str(CronTrigger.from_crontab("0 10 * * *"))
    monkeypatch.setattr(settings, "outcome_check_schedule", "invalid")
    assert str(outcome_check_trigger()) == str(
        CronTrigger(**_DEFAULT_OUTCOME_CRON)
    )
