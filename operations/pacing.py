"""Budget plan import, pacing, month-end projection and advisory recommendations."""

from __future__ import annotations

import csv
import io
import secrets
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from db.models import BudgetPlan, PacingSnapshot
from db.session import Session, db_dt
from operations.decisions import create_or_refresh_decision
from operations.incidents import record_incident
from operations.types import BudgetPlanInput, DecisionInput, IncidentInput


@dataclass(frozen=True)
class PacingResult:
    as_of_date: date
    elapsed_days: int
    total_days: int
    remaining_days: int
    spend_to_date_micros: int
    expected_to_date_micros: int
    projected_spend_micros: int
    variance_micros: int
    variance_ratio: float
    recommended_daily_budget_micros: int | None
    status: str
    ceiling_breached: bool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def money_to_micros(value: str | int | float | Decimal) -> int:
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid money amount: {value!r}") from exc
    if amount < 0:
        raise ValueError("money amount cannot be negative")
    return int((amount * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_pacing(
    plan: BudgetPlanInput,
    *,
    as_of: date,
    spend_to_date_micros: int,
    today_spend_micros: int | None = None,
    tolerance: float = 0.05,
    critical_tolerance: float = 0.15,
) -> PacingResult:
    if spend_to_date_micros < 0 or (today_spend_micros is not None and today_spend_micros < 0):
        raise ValueError("spend cannot be negative")
    if not 0 <= tolerance < critical_tolerance < 1:
        raise ValueError("expected 0 <= tolerance < critical_tolerance < 1")

    total = (plan.period_end - plan.period_start).days + 1
    capped = min(max(as_of, plan.period_start), plan.period_end)
    elapsed = (capped - plan.period_start).days + 1
    remaining = max(0, (plan.period_end - capped).days)
    expected = round(plan.planned_spend_micros * elapsed / total)
    projected = round(spend_to_date_micros / elapsed * total) if elapsed else 0
    variance = projected - plan.planned_spend_micros
    variance_ratio = variance / plan.planned_spend_micros

    required_daily = None
    if remaining:
        required_daily = max(
            0, round((plan.planned_spend_micros - spend_to_date_micros) / remaining)
        )
        if plan.daily_ceiling_micros:
            required_daily = min(required_daily, plan.daily_ceiling_micros)

    ceiling_breached = bool(
        (plan.monthly_ceiling_micros and spend_to_date_micros > plan.monthly_ceiling_micros)
        or (
            plan.daily_ceiling_micros
            and today_spend_micros is not None
            and today_spend_micros > plan.daily_ceiling_micros
        )
    )
    if ceiling_breached or variance_ratio > critical_tolerance:
        status = "critical"
    elif variance_ratio > tolerance:
        status = "overspend"
    elif variance_ratio < -critical_tolerance:
        status = "critical_under"
    elif variance_ratio < -tolerance:
        status = "underspend"
    else:
        status = "on_track"

    return PacingResult(
        as_of_date=as_of,
        elapsed_days=elapsed,
        total_days=total,
        remaining_days=remaining,
        spend_to_date_micros=spend_to_date_micros,
        expected_to_date_micros=expected,
        projected_spend_micros=projected,
        variance_micros=variance,
        variance_ratio=round(variance_ratio, 6),
        recommended_daily_budget_micros=required_daily,
        status=status,
        ceiling_breached=ceiling_breached,
    )


async def save_budget_plan(spec: BudgetPlanInput) -> BudgetPlan:
    """Persist the next plan version; retry unique-version races from concurrent importers."""
    last_error: IntegrityError | None = None
    for _attempt in range(3):
        try:
            return await _save_budget_plan_once(spec)
        except IntegrityError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


async def _save_budget_plan_once(spec: BudgetPlanInput) -> BudgetPlan:
    now = utcnow()
    async with Session() as session:
        current_version = (
            await session.execute(
                select(func.max(BudgetPlan.version)).where(
                    BudgetPlan.customer_id == spec.customer_id,
                    BudgetPlan.scope_type == spec.scope_type,
                    BudgetPlan.scope_id == spec.scope_id,
                    BudgetPlan.period_start == spec.period_start.isoformat(),
                    BudgetPlan.period_end == spec.period_end.isoformat(),
                )
            )
        ).scalar_one()
        await session.execute(
            update(BudgetPlan)
            .where(
                BudgetPlan.customer_id == spec.customer_id,
                BudgetPlan.scope_type == spec.scope_type,
                BudgetPlan.scope_id == spec.scope_id,
                BudgetPlan.period_start == spec.period_start.isoformat(),
                BudgetPlan.period_end == spec.period_end.isoformat(),
                BudgetPlan.active.is_(True),
            )
            .values(active=False, updated_at=db_dt(now))
        )
        row = BudgetPlan(
            plan_uid=f"plan_{secrets.token_hex(12)}",
            customer_id=spec.customer_id,
            scope_type=spec.scope_type,
            scope_id=spec.scope_id,
            name=spec.name,
            period_start=spec.period_start.isoformat(),
            period_end=spec.period_end.isoformat(),
            currency=spec.currency,
            planned_spend_micros=spec.planned_spend_micros,
            monthly_ceiling_micros=spec.monthly_ceiling_micros,
            daily_ceiling_micros=spec.daily_ceiling_micros,
            source=spec.source,
            version=int(current_version or 0) + 1,
            active=True,
            created_by=spec.created_by,
            created_at=db_dt(now),
            updated_at=db_dt(now),
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise
        await session.refresh(row)
        return row


def parse_budget_plan_csv(
    text: str, *, default_customer_id: str | None = None
) -> list[BudgetPlanInput]:
    """Validate the whole CSV before any caller persists rows.

    Required columns: scope_type, scope_id, name, period_start, period_end, currency, planned_spend.
    ``customer_id`` may be supplied per row or as ``default_customer_id``.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {
        "scope_type",
        "scope_id",
        "name",
        "period_start",
        "period_end",
        "currency",
        "planned_spend",
    }
    missing = required - set(reader.fieldnames or ())
    if missing:
        raise ValueError(f"budget CSV missing columns: {sorted(missing)}")
    rows: list[BudgetPlanInput] = []
    for number, row in enumerate(reader, start=2):
        try:
            customer_id = (row.get("customer_id") or default_customer_id or "").strip()
            rows.append(
                BudgetPlanInput(
                    customer_id=customer_id,
                    scope_type=row["scope_type"],
                    scope_id=row["scope_id"],
                    name=row["name"],
                    period_start=date.fromisoformat(row["period_start"]),
                    period_end=date.fromisoformat(row["period_end"]),
                    currency=row["currency"],
                    planned_spend_micros=money_to_micros(row["planned_spend"]),
                    monthly_ceiling_micros=(
                        money_to_micros(row["monthly_ceiling"])
                        if row.get("monthly_ceiling", "").strip()
                        else None
                    ),
                    daily_ceiling_micros=(
                        money_to_micros(row["daily_ceiling"])
                        if row.get("daily_ceiling", "").strip()
                        else None
                    ),
                    source="csv",
                )
            )
        except Exception as exc:  # validation boundary: add the row number, keep no raw row values
            raise ValueError(f"invalid budget CSV row {number}: {type(exc).__name__}") from exc
    if not rows:
        raise ValueError("budget CSV has no data rows")
    return rows


def parse_budget_plan_rows(
    rows: list[dict[str, object]], *, default_customer_id: str | None = None
) -> list[BudgetPlanInput]:
    """Validate rows already read through the project's Google Sheets client.

    This deliberately accepts values, not a spreadsheet URL/token: transport and OAuth stay in
    the existing Sheets layer, while validation is identical and testable here.
    """
    if not rows:
        raise ValueError("budget sheet has no data rows")
    parsed: list[BudgetPlanInput] = []
    for number, row in enumerate(rows, start=2):
        try:
            get = lambda key: str(row.get(key, "") or "").strip()  # noqa: E731
            parsed.append(
                BudgetPlanInput(
                    customer_id=get("customer_id") or default_customer_id or "",
                    scope_type=get("scope_type"),
                    scope_id=get("scope_id"),
                    name=get("name"),
                    period_start=date.fromisoformat(get("period_start")),
                    period_end=date.fromisoformat(get("period_end")),
                    currency=get("currency"),
                    planned_spend_micros=money_to_micros(get("planned_spend")),
                    monthly_ceiling_micros=(
                        money_to_micros(get("monthly_ceiling")) if get("monthly_ceiling") else None
                    ),
                    daily_ceiling_micros=(
                        money_to_micros(get("daily_ceiling")) if get("daily_ceiling") else None
                    ),
                    source="sheets",
                )
            )
        except Exception as exc:
            raise ValueError(f"invalid budget sheet row {number}: {type(exc).__name__}") from exc
    return parsed


def _plan_input(row: BudgetPlan) -> BudgetPlanInput:
    return BudgetPlanInput(
        customer_id=row.customer_id,
        scope_type=row.scope_type,
        scope_id=row.scope_id,
        name=row.name,
        period_start=date.fromisoformat(row.period_start),
        period_end=date.fromisoformat(row.period_end),
        currency=row.currency,
        planned_spend_micros=row.planned_spend_micros,
        monthly_ceiling_micros=row.monthly_ceiling_micros,
        daily_ceiling_micros=row.daily_ceiling_micros,
        source=row.source,
        created_by=row.created_by,
    )


async def evaluate_and_record_pacing(
    plan_uid: str,
    *,
    as_of: date,
    spend_to_date_micros: int,
    today_spend_micros: int | None = None,
) -> PacingResult:
    async with Session() as session:
        plan = (
            await session.execute(
                select(BudgetPlan).where(
                    BudgetPlan.plan_uid == plan_uid, BudgetPlan.active.is_(True)
                )
            )
        ).scalar_one_or_none()
    if plan is None:
        raise LookupError("active budget plan not found")
    result = calculate_pacing(
        _plan_input(plan),
        as_of=as_of,
        spend_to_date_micros=spend_to_date_micros,
        today_spend_micros=today_spend_micros,
    )
    now = utcnow()
    async with Session() as session:
        snap = (
            await session.execute(
                select(PacingSnapshot).where(
                    PacingSnapshot.plan_uid == plan_uid,
                    PacingSnapshot.as_of_date == as_of.isoformat(),
                )
            )
        ).scalar_one_or_none()
        values = asdict(result)
        values.pop("elapsed_days")
        values.pop("total_days")
        values.pop("remaining_days")
        values.pop("ceiling_breached")
        values["as_of_date"] = as_of.isoformat()
        if snap is None:
            snap = PacingSnapshot(
                plan_uid=plan_uid,
                customer_id=plan.customer_id,
                created_at=db_dt(now),
                **values,
            )
            session.add(snap)
        else:
            for key, value in values.items():
                setattr(snap, key, value)
            snap.created_at = db_dt(now)
        await session.commit()

    if result.status != "on_track":
        severity = "critical" if result.status == "critical" else "warning"
        action = (
            f"Review the {plan.scope_type} budget and set a human-approved daily target; "
            f"remaining-plan rate is {result.recommended_daily_budget_micros or 0} micros/day."
        )
        decision = await create_or_refresh_decision(
            DecisionInput(
                customer_id=plan.customer_id,
                source="pacing",
                source_ref=plan_uid,
                category="budget_pacing",
                severity=severity,
                title=f"Budget pacing: {plan.name} is {result.status}",
                rationale=(
                    f"Projected spend is {result.projected_spend_micros} micros against "
                    f"a {plan.planned_spend_micros} micros plan."
                ),
                recommended_action=action,
                recommended_operation=("update_budget" if plan.scope_type == "campaign" else None),
                confidence=1.0,
                evidence={**asdict(result), "plan_uid": plan_uid, "scope_id": plan.scope_id},
                fingerprint_fields={"plan_uid": plan_uid},
            )
        )
        if result.status == "critical":
            await record_incident(
                IncidentInput(
                    customer_id=plan.customer_id,
                    kind="budget_ceiling" if result.ceiling_breached else "budget_pacing",
                    severity="critical",
                    title=f"Critical budget pacing: {plan.name}",
                    evidence={**asdict(result), "plan_uid": plan_uid},
                    decision_uid=decision.decision_uid,
                    fingerprint_fields={"plan_uid": plan_uid},
                )
            )
    return result
