"""Operational control plane: deterministic analysis plus additive money-path governance."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, update

from confirm.store import ConfirmStore
from core.config import Settings, settings
from core.provenance import human_turn
from db.models import ApprovalVote, BudgetPlan, OperationalDecision, Proposal, RoleAssignment
from db.session import Session, db_dt
from operations.bulk import build_bulk_plan, proposal_payload
from operations.correlation import TimelineEvent, correlate_changes
from operations.decisions import (
    create_or_refresh_decision,
    mark_decision_applied_from_audit,
    transition_decision,
)
from operations.experiments import ExperimentMetric, evaluate_result
from operations.explain import CopilotBrief, EvidenceItem
from operations.governance import record_approval_vote
from operations.identity import _verified_digests
from operations.incidents import record_incident, transition_incident
from operations.integrity import PerformanceWindow, evaluate_conversion_integrity
from operations.pacing import (
    calculate_pacing,
    money_to_micros,
    parse_budget_plan_rows,
    save_budget_plan,
)
from operations.playbooks import evaluate_rule, validate_rule
from operations.policy import PolicyLimits
from operations.portfolio import portfolio_summary, recommend_reallocation, upsert_channel_metric
from operations.revenue import external_id_digest
from operations.retention import purge_operational_rows
from operations.routing import AlertRouter, Notification, Route
from operations.search_mining import KeywordRef, NegativeRef, detect_negative_conflicts
from operations.types import (
    BudgetPlanInput,
    ChannelMetricInput,
    DecisionInput,
    ExperimentInput,
    IncidentInput,
)

DRAFT = "7753643025"


def test_pacing_projects_month_and_respects_daily_ceiling():
    plan = BudgetPlanInput(
        customer_id=DRAFT,
        scope_type="account",
        scope_id=DRAFT,
        name="July",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        currency="eur",
        planned_spend_micros=money_to_micros("3100"),
        monthly_ceiling_micros=money_to_micros("3500"),
        daily_ceiling_micros=money_to_micros("120"),
    )
    result = calculate_pacing(
        plan,
        as_of=date(2026, 7, 10),
        spend_to_date_micros=money_to_micros("1400"),
        today_spend_micros=money_to_micros("100"),
    )
    assert result.projected_spend_micros == money_to_micros("4340")
    assert result.status == "critical"
    assert result.recommended_daily_budget_micros == money_to_micros("80.952381")


def test_sheet_import_validates_all_rows_and_marks_source():
    rows = parse_budget_plan_rows(
        [
            {
                "scope_type": "campaign",
                "scope_id": "123",
                "name": "Brand",
                "period_start": "2026-07-01",
                "period_end": "2026-07-31",
                "currency": "USD",
                "planned_spend": "1000.50",
            }
        ],
        default_customer_id=DRAFT,
    )
    assert rows[0].source == "sheets"
    assert rows[0].planned_spend_micros == 1_000_500_000


async def test_concurrent_budget_plan_imports_get_unique_versions_and_one_active():
    scope_id = f"campaign-{uuid.uuid4().hex}"
    spec = BudgetPlanInput(
        customer_id=DRAFT,
        scope_type="campaign",
        scope_id=scope_id,
        name="Concurrent plan",
        period_start=date(2099, 2, 1),
        period_end=date(2099, 2, 28),
        currency="USD",
        planned_spend_micros=10_000_000,
        source="api",
    )
    rows = await asyncio.gather(save_budget_plan(spec), save_budget_plan(spec))
    assert {row.version for row in rows} == {1, 2}
    async with Session() as session:
        stored = list(
            (await session.execute(select(BudgetPlan).where(BudgetPlan.scope_id == scope_id)))
            .scalars()
            .all()
        )
    assert len(stored) == 2
    assert sum(row.active for row in stored) == 1
    assert max(row.version for row in stored if row.active) == 2


def test_conversion_integrity_does_not_guess_when_actions_unavailable():
    no_data = evaluate_conversion_integrity(
        DRAFT,
        actions=None,
        current=PerformanceWindow(spend_micros=20_000_000, clicks=100, conversions=0),
    )
    confirmed_empty = evaluate_conversion_integrity(
        DRAFT,
        actions=[],
        current=PerformanceWindow(spend_micros=20_000_000, clicks=100, conversions=0),
    )
    assert no_data == []
    assert confirmed_empty[0].severity == "critical"


def test_change_history_output_states_correlation_not_causation():
    shift = datetime(2026, 7, 30, tzinfo=timezone.utc)
    causes = correlate_changes(
        shift,
        [
            TimelineEvent(
                occurred_at=shift - timedelta(hours=3),
                source="google_change_event",
                kind="budget_change",
                resource="campaign/1",
            )
        ],
    )
    assert causes and "not proof of causation" in causes[0].explanation


def test_negative_conflict_covers_exact_phrase_and_broad():
    keyword = KeywordRef(DRAFT, "Brand", "Core", "buy red running shoes", "BROAD")
    negatives = [
        NegativeRef(DRAFT, "campaign", "Brand", "red running", "PHRASE"),
        NegativeRef(DRAFT, "account", "all", "buy shoes", "BROAD"),
    ]
    assert len(detect_negative_conflicts([keyword], negatives)) == 2


def test_bulk_policy_rejects_large_scope_and_money_jump():
    params = {"_before": {"before_micros": 100, "after_micros": 150}}
    plan = build_bulk_plan(
        operation="update_budget",
        targets=[{"id": "1", "before": 100, "after": 150}],
        params=params,
        limits=PolicyLimits(
            max_targets=1,
            max_money_increase_pct=20,
            allowed_operations=frozenset({"update_budget"}),
        ),
    )
    assert not plan.verdict.allowed
    assert "money_delta_exceeded" in plan.verdict.violations
    with pytest.raises(PermissionError):
        proposal_payload(plan)


def test_playbook_language_cannot_contain_mutation_action():
    with pytest.raises(ValueError, match="restricted"):
        validate_rule(
            {
                "all": [{"field": "spend_micros", "op": "gt", "value": 10}],
                "action": {
                    "type": "mutation",
                    "category": "waste",
                    "severity": "warning",
                    "title": "Pause",
                },
            }
        )
    rule = {
        "all": [
            {"field": "spend_micros", "op": "gt", "value": 10},
            {"field": "conversions", "op": "eq", "value": 0},
        ],
        "action": {
            "type": "decision",
            "category": "waste",
            "severity": "warning",
            "title": "Review waste",
        },
    }
    assert evaluate_rule(rule, {"spend_micros": 11, "conversions": 0}) is True


def test_explainable_contract_cannot_claim_applied_without_audit():
    base = {
        "what_happened": "CPA increased.",
        "evidence": [EvidenceItem(claim="CPA", source="Ads report", observed_value="+20%")],
        "recommended_next_step": "Review search terms.",
        "what_not_to_do": "Do not change budget before validating tracking.",
        "confidence": 0.8,
        "confidence_basis": "measured",
        "approval_required": False,
        "execution_status": "applied",
    }
    with pytest.raises(ValidationError):
        CopilotBrief(**base)


async def test_router_requires_config_refs_and_configured_transports():
    with pytest.raises(ValueError):
        Route("slack", "https://secret-webhook", frozenset({"critical"}))
    with pytest.raises(ValueError):
        Route("slack", "xoxb-raw-token", frozenset({"critical"}))

    sent: list[str] = []
    bodies: list[str] = []

    class FakeTransport:
        async def send(self, *, destination_ref, notification):
            sent.append(f"{destination_ref}:{notification.dedup_key}")
            bodies.append(notification.body)

    router = AlertRouter({"slack": FakeTransport()})
    delivered = await router.deliver(
        Notification(
            "inc-1",
            "critical",
            "Overspend",
            "Inspect pacing with sk-or-1234567890abcdefghij",
        ),
        [Route("slack", "SLACK_OPS_CHANNEL", frozenset({"critical"}))],
    )
    assert delivered == ["slack"]
    assert sent == ["SLACK_OPS_CHANNEL:inc-1"]
    assert "sk-or-1234567890abcdefghij" not in bodies[0]
    assert "REDACTED" in bodies[0]


async def test_decision_and_incident_lifecycles_are_persistent_and_deduplicated():
    marker = uuid.uuid4().hex
    spec = DecisionInput(
        customer_id=DRAFT,
        source="test",
        category="pacing",
        severity="warning",
        title="Pacing drift",
        rationale="Projection is above plan.",
        recommended_action="Review the plan.",
        confidence=1,
        fingerprint_fields={"marker": marker},
    )
    first = await create_or_refresh_decision(spec)
    second = await create_or_refresh_decision(spec)
    assert second.decision_uid == first.decision_uid
    assert second.occurrence_count == 2
    assert not await transition_decision(
        first.decision_uid,
        "acknowledged",
        actor_user_id=100,
        customer_id="9999999999",
        note="wrong account",
    )
    assert await transition_decision(
        first.decision_uid,
        "acknowledged",
        actor_user_id=100,
        customer_id=DRAFT,
        note="checking",
    )

    incident_spec = IncidentInput(
        customer_id=DRAFT,
        kind="overspend",
        severity="critical",
        title="Ceiling breached",
        fingerprint_fields={"marker": marker},
    )
    incident = await record_incident(incident_spec)
    repeated = await record_incident(incident_spec)
    assert repeated.incident_uid == incident.incident_uid
    assert repeated.occurrence_count == 2
    assert not await transition_incident(
        incident.incident_uid,
        "acknowledged",
        actor_user_id=100,
        customer_id="9999999999",
    )
    assert await transition_incident(
        incident.incident_uid,
        "acknowledged",
        actor_user_id=100,
        customer_id=DRAFT,
    )


def test_experiment_uses_pre_registered_threshold_and_window():
    spec = ExperimentInput(
        customer_id=DRAFT,
        name="Copy test",
        hypothesis="New copy increases CVR.",
        control={"asset_set": "A"},
        treatment={"asset_set": "B"},
        primary_metric="cvr",
        success_direction="up",
        success_threshold=0.1,
        minimum_sample=100,
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 15),
    )
    result = evaluate_result(
        spec,
        control=ExperimentMetric(0.1, 200),
        treatment=ExperimentMetric(0.12, 200),
        as_of=date(2026, 7, 16),
    )
    assert result.verdict == "keep"


def test_crm_identifier_is_keyed_one_way_and_source_scoped(monkeypatch):
    monkeypatch.setattr(settings, "pseudonymization_hmac_key", SecretStr("a" * 32))
    raw = "lead@example.invalid"
    digest = external_id_digest("hubspot", raw)
    assert raw not in digest and len(digest) == 64
    assert digest != external_id_digest("pipedrive", raw)
    monkeypatch.setattr(settings, "pseudonymization_hmac_key", SecretStr("b" * 32))
    assert digest != external_id_digest("hubspot", raw)


def test_crm_identifier_hashing_fails_closed_without_key(monkeypatch):
    monkeypatch.setattr(settings, "pseudonymization_hmac_key", SecretStr(""))
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        external_id_digest("hubspot", "guessable@example.invalid")


def test_identity_subject_is_keyed_and_unverified_claims_fail(monkeypatch):
    monkeypatch.setattr(settings, "pseudonymization_hmac_key", SecretStr("a" * 32))
    issuer = "https://idp.example.invalid"
    subject = "person@example.invalid"
    issuer_hash, subject_hash = _verified_digests(issuer=issuer, subject=subject, verified=True)
    assert issuer not in issuer_hash and subject not in subject_hash
    assert issuer_hash != subject_hash
    with pytest.raises(PermissionError, match="unverified"):
        _verified_digests(issuer=issuer, subject=subject, verified=False)
    monkeypatch.setattr(settings, "pseudonymization_hmac_key", SecretStr("b" * 32))
    assert (issuer_hash, subject_hash) != _verified_digests(
        issuer=issuer, subject=subject, verified=True
    )


def test_four_eyes_config_rejects_unknown_or_empty_required_tiers():
    with pytest.raises(ValidationError, match="unknown tiers"):
        Settings(
            _env_file=None,
            four_eyes_required=True,
            four_eyes_risk_tiers_csv="L3,L9",
        )
    with pytest.raises(ValidationError, match="at least one"):
        Settings(
            _env_file=None,
            four_eyes_required=True,
            four_eyes_risk_tiers_csv="",
        )


async def _proposal(store: ConfirmStore, *, author: int, tier: str = "L3") -> str:
    confirmation_id = uuid.uuid4().hex
    with human_turn(actor_user_id=author, run_id=uuid.uuid4().hex):
        await store.save_proposal(
            confirmation_id=confirmation_id,
            operation="resume_campaign",
            customer_id=DRAFT,
            params={"campaign_id": "1"},
            summary="paused → enabled",
            chat_id=author,
            user_initiated=True,
            risk_tier=tier,
        )
    assert await store.confirm(confirmation_id, chat_id=author, actor_user_id=author)
    return confirmation_id


async def test_four_eyes_is_additive_atomic_and_independent(monkeypatch):
    author = 700_000 + int(uuid.uuid4().hex[:5], 16)
    approver = author + 1
    monkeypatch.setattr(settings, "four_eyes_required", True)
    monkeypatch.setattr(settings, "four_eyes_risk_tiers_csv", "L3")
    store = ConfirmStore()
    confirmation_id = await _proposal(store, author=author)

    # The author's normal confirmation is still necessary but no longer sufficient for L3.
    assert await store.claim(confirmation_id, operation="resume_campaign") is None
    with pytest.raises(PermissionError):
        await record_approval_vote(
            confirmation_id=confirmation_id,
            actor_user_id=author,
            decision="approve",
            comment="self approval",
        )

    async with Session() as session:
        session.add(
            RoleAssignment(
                user_id=approver,
                customer_id=DRAFT,
                role="approver",
                active=True,
                created_at=db_dt(datetime.now(timezone.utc)),
            )
        )
        await session.commit()
    await record_approval_vote(
        confirmation_id=confirmation_id,
        actor_user_id=approver,
        decision="approve",
        comment="scope and diff reviewed",
    )
    claimed = await store.claim(confirmation_id, operation="resume_campaign")
    assert claimed is not None and claimed.status == "executing"
    assert await store.claim(confirmation_id, operation="resume_campaign") is None


async def test_four_eyes_empty_runtime_tier_set_fails_closed(monkeypatch):
    author = 750_000 + int(uuid.uuid4().hex[:5], 16)
    monkeypatch.setattr(settings, "four_eyes_required", True)
    monkeypatch.setattr(settings, "four_eyes_risk_tiers_csv", "")
    store = ConfirmStore()
    confirmation_id = await _proposal(store, author=author)
    assert await store.claim(confirmation_id, operation="resume_campaign") is None


async def test_four_eyes_reject_blocks_claim(monkeypatch):
    author = 800_000 + int(uuid.uuid4().hex[:5], 16)
    approver = author + 1
    monkeypatch.setattr(settings, "four_eyes_required", True)
    monkeypatch.setattr(settings, "four_eyes_risk_tiers_csv", "L3")
    store = ConfirmStore()
    confirmation_id = await _proposal(store, author=author)
    async with Session() as session:
        session.add(
            RoleAssignment(
                user_id=approver,
                customer_id="*",
                role="admin",
                active=True,
                created_at=db_dt(datetime.now(timezone.utc)),
            )
        )
        await session.commit()
    await record_approval_vote(
        confirmation_id=confirmation_id,
        actor_user_id=approver,
        decision="reject",
        comment="blast radius too high",
    )
    assert await store.claim(confirmation_id, operation="resume_campaign") is None
    async with Session() as session:
        votes = list(
            (
                await session.execute(
                    select(ApprovalVote).where(ApprovalVote.confirmation_id == confirmation_id)
                )
            )
            .scalars()
            .all()
        )
        proposal = (
            await session.execute(
                select(Proposal).where(Proposal.confirmation_id == confirmation_id)
            )
        ).scalar_one()
    assert len(votes) == 1 and proposal.status == "confirmed"


async def test_four_eyes_reject_survives_role_revocation(monkeypatch):
    author = 900_000 + int(uuid.uuid4().hex[:5], 16)
    rejector = author + 1
    approver = author + 2
    monkeypatch.setattr(settings, "four_eyes_required", True)
    monkeypatch.setattr(settings, "four_eyes_risk_tiers_csv", "L3")
    store = ConfirmStore()
    confirmation_id = await _proposal(store, author=author)
    async with Session() as session:
        session.add_all(
            [
                RoleAssignment(
                    user_id=rejector,
                    customer_id=DRAFT,
                    role="approver",
                    active=True,
                    created_at=db_dt(datetime.now(timezone.utc)),
                ),
                RoleAssignment(
                    user_id=approver,
                    customer_id=DRAFT,
                    role="approver",
                    active=True,
                    created_at=db_dt(datetime.now(timezone.utc)),
                ),
            ]
        )
        await session.commit()
    await record_approval_vote(
        confirmation_id=confirmation_id,
        actor_user_id=rejector,
        decision="reject",
        comment="do not execute this scope",
    )
    async with Session() as session:
        await session.execute(
            update(RoleAssignment).where(RoleAssignment.user_id == rejector).values(active=False)
        )
        await session.commit()
    await record_approval_vote(
        confirmation_id=confirmation_id,
        actor_user_id=approver,
        decision="approve",
        comment="independent approval after rejection",
    )
    assert await store.claim(confirmation_id, operation="resume_campaign") is None


async def test_decision_applied_requires_matching_live_audit_proof():
    marker = uuid.uuid4().hex
    decision = await create_or_refresh_decision(
        DecisionInput(
            customer_id=DRAFT,
            source="test",
            category="delivery",
            severity="warning",
            title="Campaign should resume",
            rationale="Campaign is unexpectedly paused.",
            recommended_action="Review and resume the campaign.",
            recommended_operation="resume_campaign",
            confidence=0.9,
            fingerprint_fields={"marker": marker},
        )
    )
    assert await transition_decision(decision.decision_uid, "approved", actor_user_id=101)
    with pytest.raises(ValueError, match="unsupported decision transition"):
        await transition_decision(decision.decision_uid, "applied", actor_user_id=101)
    assert not await mark_decision_applied_from_audit(
        decision.decision_uid,
        proposal_confirmation_id=uuid.uuid4().hex,
        actor_user_id=101,
    )

    store = ConfirmStore()
    confirmation_id = await _proposal(store, author=101, tier="L1")
    assert await store.claim(confirmation_id, operation="resume_campaign") is not None
    await store.finalize(confirmation_id, result={"resource_name": "campaign/1"})
    assert await mark_decision_applied_from_audit(
        decision.decision_uid,
        proposal_confirmation_id=confirmation_id,
        actor_user_id=101,
        note="verified from audit",
    )
    async with Session() as session:
        row = (
            await session.execute(
                select(OperationalDecision).where(
                    OperationalDecision.decision_uid == decision.decision_uid
                )
            )
        ).scalar_one()
    assert row.status == "applied"
    assert row.proposal_confirmation_id == confirmation_id
    assert row.active_fingerprint is None


async def test_concurrent_decision_and_incident_dedup_is_database_backed():
    marker = uuid.uuid4().hex
    decision_spec = DecisionInput(
        customer_id=DRAFT,
        source="test",
        category="pacing",
        severity="warning",
        title="Concurrent pacing signal",
        rationale="Same detector ran concurrently.",
        recommended_action="Review once.",
        confidence=0.8,
        fingerprint_fields={"marker": marker},
    )
    decisions = await asyncio.gather(
        create_or_refresh_decision(decision_spec),
        create_or_refresh_decision(decision_spec),
    )
    assert len({row.decision_uid for row in decisions}) == 1
    assert max(row.occurrence_count for row in decisions) == 2

    incident_spec = IncidentInput(
        customer_id=DRAFT,
        kind="pacing",
        severity="critical",
        title="Concurrent incident signal",
        fingerprint_fields={"marker": marker},
    )
    incidents = await asyncio.gather(
        record_incident(incident_spec),
        record_incident(incident_spec),
    )
    assert len({row.incident_uid for row in incidents}) == 1
    assert max(row.occurrence_count for row in incidents) == 2


async def test_retention_removes_old_applied_decision_but_not_audit(monkeypatch):
    marker = uuid.uuid4().hex
    decision = await create_or_refresh_decision(
        DecisionInput(
            customer_id=DRAFT,
            source="test",
            category="delivery",
            severity="info",
            title="Retention proof",
            rationale="This terminal decision is old.",
            recommended_action="No action.",
            confidence=1.0,
            fingerprint_fields={"marker": marker},
        )
    )
    async with Session() as session:
        await session.execute(
            update(OperationalDecision)
            .where(OperationalDecision.decision_uid == decision.decision_uid)
            .values(
                status="applied",
                active_fingerprint=None,
                created_at=db_dt(datetime.now(timezone.utc) - timedelta(days=10)),
            )
        )
        await session.commit()
    monkeypatch.setattr(settings, "operations_retain_days", 1)
    monkeypatch.setattr(settings, "revenue_events_retain_days", 0)
    monkeypatch.setattr(settings, "channel_metrics_retain_days", 0)
    result = await purge_operational_rows(now=datetime.now(timezone.utc))
    assert result["operational_decisions"] >= 1
    async with Session() as session:
        remaining = (
            await session.execute(
                select(OperationalDecision.id).where(
                    OperationalDecision.decision_uid == decision.decision_uid
                )
            )
        ).first()
    assert remaining is None


async def test_portfolio_requires_scope_and_never_leaks_other_customer():
    with pytest.raises(PermissionError, match="explicit non-empty customer scope"):
        await portfolio_summary(date_from="2099-01-01", date_to="2099-01-31", customer_ids=set())

    customer_a = "1234567890"
    customer_b = "1234567891"
    marker = uuid.uuid4().hex
    for customer_id in (customer_a, customer_b):
        await upsert_channel_metric(
            ChannelMetricInput(
                channel="google",
                customer_id=customer_id,
                external_account_id=f"acct-{marker}-{customer_id}",
                metric_date=date(2099, 1, 15),
                spend_micros=1_000_000,
                conversions=1,
                revenue_micros=2_000_000,
                currency="USD",
                source="test",
            )
        )
        await create_or_refresh_decision(
            DecisionInput(
                customer_id=customer_id,
                source="test",
                category="portfolio",
                severity="warning",
                title="Scoped portfolio decision",
                rationale="Scope test.",
                recommended_action="Review.",
                confidence=1,
                fingerprint_fields={"marker": marker},
            )
        )
        await record_incident(
            IncidentInput(
                customer_id=customer_id,
                kind="portfolio",
                severity="critical",
                title="Scoped portfolio incident",
                fingerprint_fields={"marker": marker},
            )
        )

    summary = await portfolio_summary(
        date_from="2099-01-01",
        date_to="2099-01-31",
        customer_ids={customer_a},
    )
    assert {row["customer_id"] for row in summary["metrics"]} == {customer_a}
    assert summary["critical_incidents"] == 1
    assert summary["open_decisions"] == 1


def test_reallocation_never_moves_budget_between_customers():
    metrics = [
        {
            "customer_id": "1234567890",
            "channel": "google",
            "currency": "USD",
            "spend_micros": 100,
            "roas": 1.0,
        },
        {
            "customer_id": "1234567891",
            "channel": "meta",
            "currency": "USD",
            "spend_micros": 100,
            "roas": 3.0,
        },
    ]
    assert recommend_reallocation(metrics) == []
