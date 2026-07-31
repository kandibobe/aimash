"""Role checks and four-eyes evidence for high-risk proposals.

This module never confirms or executes a proposal. A trusted UI records an independent vote;
``ConfirmStore.claim`` consumes that evidence inside its existing atomic CAS. Four-eyes is an
additional conjunct, not an alternative confirmation route.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, exists, false, or_, select, true, update
from sqlalchemy.exc import IntegrityError

from core.access import is_admin
from core.config import normalize_customer_id, settings
from core.logging import redact_text
from db.models import ApprovalVote, Proposal, RoleAssignment
from db.session import Session, db_dt

ROLES = frozenset({"viewer", "operator", "approver", "admin"})
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "viewer": frozenset({"read"}),
    "operator": frozenset({"read", "propose", "ack", "snooze", "resolve"}),
    "approver": frozenset({"read", "approve", "reject", "ack", "snooze", "resolve"}),
    "admin": frozenset(
        {"read", "propose", "approve", "reject", "ack", "snooze", "resolve", "manage_roles"}
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scope(customer_id: str) -> str:
    if customer_id.strip() == "*":
        return "*"
    normalized = normalize_customer_id(customer_id)
    if not 6 <= len(normalized) <= 20:
        raise ValueError("customer_id must contain 6..20 digits or be '*'")
    return normalized


def four_eyes_claim_condition():
    """Return a SQL condition correlated to ``Proposal`` for the authoritative claim CAS.

    A selected tier requires one active in-scope approver/admin who is not the proposal author,
    and no independent rejection. Missing roles or votes therefore fail closed.
    """
    if not settings.four_eyes_required:
        return true()
    if not settings.four_eyes_risk_tiers:
        return false()

    scoped_role = and_(
        RoleAssignment.user_id == ApprovalVote.approver_user_id,
        RoleAssignment.active.is_(True),
        RoleAssignment.role.in_(("approver", "admin")),
        or_(
            RoleAssignment.customer_id == Proposal.customer_id,
            RoleAssignment.customer_id == "*",
        ),
    )
    # Unknown author cannot prove independence. Treat legacy/unstamped rows as a refusal, not as
    # "nobody authored it" (which would let any vote satisfy four-eyes).
    independent = and_(
        Proposal.author_user_id.is_not(None),
        ApprovalVote.approver_user_id != Proposal.author_user_id,
    )
    approved = exists(
        select(ApprovalVote.id)
        .select_from(ApprovalVote)
        .join(RoleAssignment, scoped_role)
        .where(
            ApprovalVote.confirmation_id == Proposal.confirmation_id,
            ApprovalVote.decision == "approve",
            independent,
        )
    )
    # Reject was authorized and made immutable at record_approval_vote time. Revoking that user's
    # role later must not erase the objection and turn the exact same proposal executable.
    rejected = exists(
        select(ApprovalVote.id).where(
            ApprovalVote.confirmation_id == Proposal.confirmation_id,
            ApprovalVote.decision == "reject",
            independent,
        )
    )
    gated_tier = Proposal.risk_tier.in_(settings.four_eyes_risk_tiers)
    return or_(~gated_tier, and_(approved, ~rejected))


async def has_capability(user_id: int, customer_id: str, capability: str) -> bool:
    if capability not in set().union(*ROLE_CAPABILITIES.values()):
        return False
    async with Session() as session:
        roles = list(
            (
                await session.execute(
                    select(RoleAssignment.role, RoleAssignment.capabilities).where(
                        RoleAssignment.user_id == user_id,
                        RoleAssignment.active.is_(True),
                        RoleAssignment.customer_id.in_((customer_id, "*")),
                    )
                )
            ).all()
        )
    for role, overrides in roles:
        if capability in ROLE_CAPABILITIES.get(role, frozenset()):
            return True
        if isinstance(overrides, list) and capability in overrides:
            return True
    return False


async def assign_role(
    *,
    actor_user_id: int,
    user_id: int,
    role: str,
    customer_id: str = "*",
    capabilities: list[str] | None = None,
) -> RoleAssignment:
    """Admin-only role assignment; env/runtime admins bootstrap the first DB role."""
    normalized_role = role.strip().lower()
    if normalized_role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    customer_id = _scope(customer_id)
    if not await is_admin(actor_user_id) and not await has_capability(
        actor_user_id, customer_id, "manage_roles"
    ):
        raise PermissionError("role assignment requires admin capability")
    extra = sorted({str(value).strip() for value in capabilities or [] if str(value).strip()})
    now = db_dt(_now())
    async with Session() as session:
        row = (
            await session.execute(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == user_id,
                    RoleAssignment.customer_id == customer_id,
                    RoleAssignment.role == normalized_role,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = RoleAssignment(
                user_id=user_id,
                customer_id=customer_id,
                role=normalized_role,
                capabilities=extra or None,
                active=True,
                created_by=actor_user_id,
                created_at=now,
            )
            session.add(row)
        else:
            row.active = True
            row.capabilities = extra or None
            row.created_by = actor_user_id
        await session.commit()
        await session.refresh(row)
        return row


async def revoke_role(
    *, actor_user_id: int, user_id: int, role: str, customer_id: str = "*"
) -> bool:
    customer_id = _scope(customer_id)
    if not await is_admin(actor_user_id) and not await has_capability(
        actor_user_id, customer_id, "manage_roles"
    ):
        raise PermissionError("role revocation requires admin capability")
    async with Session() as session:
        result = await session.execute(
            update(RoleAssignment)
            .where(
                RoleAssignment.user_id == user_id,
                RoleAssignment.customer_id == customer_id,
                RoleAssignment.role == role.strip().lower(),
                RoleAssignment.active.is_(True),
            )
            .values(active=False)
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            await session.rollback()
            return False
        await session.commit()
        return True


async def record_approval_vote(
    *, confirmation_id: str, actor_user_id: int, decision: str, comment: str
) -> ApprovalVote:
    """Record a trusted independent vote without changing proposal state."""
    normalized = decision.strip().lower()
    if normalized not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject")
    clean_comment = redact_text(comment).strip()
    if not clean_comment:
        raise ValueError("approval comment is required")

    async with Session() as session:
        proposal = (
            await session.execute(
                select(Proposal).where(
                    Proposal.confirmation_id == confirmation_id,
                    Proposal.status.in_(("pending", "confirmed")),
                )
            )
        ).scalar_one_or_none()
        if proposal is None:
            raise LookupError("proposal is missing, stale, or already claimed")
        if proposal.author_user_id is None or proposal.author_user_id == actor_user_id:
            raise PermissionError("proposal author cannot provide the independent approval")
        role_exists = (
            await session.execute(
                select(RoleAssignment.id).where(
                    RoleAssignment.user_id == actor_user_id,
                    RoleAssignment.active.is_(True),
                    RoleAssignment.role.in_(("approver", "admin")),
                    RoleAssignment.customer_id.in_((proposal.customer_id, "*")),
                )
            )
        ).first()
        if role_exists is None:
            raise PermissionError("active approver role is required for this account")
        vote = ApprovalVote(
            confirmation_id=confirmation_id,
            approver_user_id=actor_user_id,
            decision=normalized,
            comment=clean_comment,
            created_at=db_dt(_now()),
        )
        session.add(vote)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError("approver has already voted on this proposal") from exc
        await session.refresh(vote)
        return vote
