"""Hypothesis-first experiment registry and deterministic result evaluation."""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select, update

from db.models import ManagedExperiment
from db.session import Session, db_dt
from operations.decisions import create_or_refresh_decision
from operations.types import DecisionInput, ExperimentInput


@dataclass(frozen=True)
class ExperimentMetric:
    value: float
    sample: int


@dataclass(frozen=True)
class ExperimentResult:
    control: ExperimentMetric
    treatment: ExperimentMetric
    relative_change: float
    verdict: str
    reason: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def evaluate_result(
    spec: ExperimentInput,
    *,
    control: ExperimentMetric,
    treatment: ExperimentMetric,
    as_of: date,
) -> ExperimentResult:
    if control.sample < 0 or treatment.sample < 0:
        raise ValueError("sample cannot be negative")
    if control.value == 0:
        change = 0.0 if treatment.value == 0 else (1.0 if treatment.value > 0 else -1.0)
    else:
        change = (treatment.value - control.value) / abs(control.value)
    enough = min(control.sample, treatment.sample) >= spec.minimum_sample
    if not enough or as_of < spec.end_date:
        verdict = "continue"
        reason = "minimum sample or planned end date has not been reached"
    else:
        improvement = change if spec.success_direction == "up" else -change
        if improvement >= spec.success_threshold:
            verdict = "keep"
            reason = "pre-registered success threshold was met"
        elif _rollback_hit(spec.rollback_trigger, control.value, treatment.value):
            verdict = "rollback"
            reason = "pre-registered rollback trigger was hit"
        else:
            verdict = "inconclusive"
            reason = "success and rollback thresholds were not met"
    return ExperimentResult(
        control=control,
        treatment=treatment,
        relative_change=round(change, 6),
        verdict=verdict,
        reason=reason,
    )


def _rollback_hit(trigger: dict | None, control: float, treatment: float) -> bool:
    if not trigger:
        return False
    operator = trigger.get("operator")
    threshold = float(trigger.get("relative_change", 0.0))
    change = 0.0 if control == 0 else (treatment - control) / abs(control)
    if operator == "lte":
        return change <= threshold
    if operator == "gte":
        return change >= threshold
    raise ValueError("rollback trigger operator must be lte or gte")


async def create_experiment(spec: ExperimentInput) -> ManagedExperiment:
    now = utcnow()
    row = ManagedExperiment(
        experiment_uid=f"exp_{secrets.token_hex(12)}",
        customer_id=spec.customer_id,
        name=spec.name,
        hypothesis=spec.hypothesis,
        control=spec.control,
        treatment=spec.treatment,
        primary_metric=spec.primary_metric,
        success_direction=spec.success_direction,
        success_threshold=spec.success_threshold,
        minimum_sample=spec.minimum_sample,
        start_date=spec.start_date.isoformat(),
        end_date=spec.end_date.isoformat(),
        rollback_trigger=spec.rollback_trigger,
        status="draft",
        created_by=spec.created_by,
        created_at=db_dt(now),
        updated_at=db_dt(now),
    )
    async with Session() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def transition_experiment(experiment_uid: str, target: str) -> bool:
    allowed = {
        "running": {"draft"},
        "completed": {"running"},
        "cancelled": {"draft", "running"},
    }
    if target not in allowed:
        raise ValueError(f"unsupported experiment state: {target}")
    async with Session() as session:
        result = await session.execute(
            update(ManagedExperiment)
            .where(
                ManagedExperiment.experiment_uid == experiment_uid,
                ManagedExperiment.status.in_(allowed[target]),
            )
            .values(status=target, updated_at=db_dt(utcnow()))
        )
        if int(getattr(result, "rowcount", 0) or 0) == 1:
            await session.commit()
            return True
        await session.rollback()
        return False


async def record_experiment_result(
    experiment_uid: str,
    *,
    control: ExperimentMetric,
    treatment: ExperimentMetric,
    as_of: date,
) -> ExperimentResult:
    async with Session() as session:
        row = (
            await session.execute(
                select(ManagedExperiment).where(
                    ManagedExperiment.experiment_uid == experiment_uid,
                    ManagedExperiment.status == "running",
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise LookupError("running experiment not found")
    spec = ExperimentInput(
        customer_id=row.customer_id,
        name=row.name,
        hypothesis=row.hypothesis,
        control=row.control,
        treatment=row.treatment,
        primary_metric=row.primary_metric,
        success_direction=row.success_direction,
        success_threshold=row.success_threshold,
        minimum_sample=row.minimum_sample,
        start_date=date.fromisoformat(row.start_date),
        end_date=date.fromisoformat(row.end_date),
        rollback_trigger=row.rollback_trigger,
        created_by=row.created_by,
    )
    result = evaluate_result(spec, control=control, treatment=treatment, as_of=as_of)
    payload = asdict(result)
    async with Session() as session:
        await session.execute(
            update(ManagedExperiment)
            .where(ManagedExperiment.experiment_uid == experiment_uid)
            .values(
                result=payload,
                verdict=result.verdict,
                status=("completed" if result.verdict != "continue" else "running"),
                updated_at=db_dt(utcnow()),
            )
        )
        await session.commit()
    if result.verdict in {"keep", "rollback", "inconclusive"}:
        await create_or_refresh_decision(
            DecisionInput(
                customer_id=row.customer_id,
                source="experiment",
                source_ref=experiment_uid,
                category="experiment",
                severity="warning" if result.verdict == "rollback" else "info",
                title=f"Experiment {row.name}: {result.verdict}",
                rationale=result.reason,
                recommended_action=(
                    "Prepare a human-approved rollback proposal."
                    if result.verdict == "rollback"
                    else "Review the pre-registered result and decide whether to keep or scale it."
                ),
                confidence=1.0,
                evidence=payload,
                fingerprint_fields={"experiment_uid": experiment_uid, "verdict": result.verdict},
            )
        )
    return result
