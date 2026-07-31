"""Trusted-gateway identity mapping for future OIDC/SAML transports.

Token/signature verification belongs at the gateway. This module refuses unverified claims and
stores only hashes of issuer/subject, never raw SSO claims or tokens.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.access import is_admin
from core.config import settings
from db.models import ExternalIdentity
from db.session import Session, db_dt


def _digest(value: str, *, domain: bytes) -> str:
    root_key = settings.pseudonymization_hmac_key.get_secret_value()
    if len(root_key.encode()) < 32:
        raise RuntimeError("PSEUDONYMIZATION_HMAC_KEY must contain at least 32 bytes")
    digest_key = hmac.new(root_key.encode(), domain, hashlib.sha256).digest()
    return hmac.new(digest_key, value.encode(), hashlib.sha256).hexdigest()


def _verified_digests(*, issuer: str, subject: str, verified: bool) -> tuple[str, str]:
    if not verified:
        raise PermissionError("unverified identity claims are rejected")
    if not issuer.strip() or not subject.strip():
        raise ValueError("verified issuer and subject are required")
    return _digest(issuer.strip(), domain=b"aimash:identity-issuer:v1"), _digest(
        subject.strip(), domain=b"aimash:identity-subject:v1"
    )


async def bind_verified_identity(
    *,
    actor_user_id: int,
    provider: str,
    issuer: str,
    subject: str,
    mapped_user_id: int,
    verified: bool,
) -> ExternalIdentity:
    if not await is_admin(actor_user_id):
        raise PermissionError("binding an external identity requires an admin")
    issuer_hash, subject_hash = _verified_digests(issuer=issuer, subject=subject, verified=verified)
    now = db_dt(datetime.now(timezone.utc))
    row = ExternalIdentity(
        provider=provider.strip().lower(),
        issuer_hash=issuer_hash,
        subject_hash=subject_hash,
        user_id=mapped_user_id,
        active=True,
        created_by=actor_user_id,
        created_at=now,
        last_seen_at=now,
    )
    async with Session() as session:
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError("external identity is already bound") from exc
        await session.refresh(row)
        return row


async def resolve_verified_identity(
    *, provider: str, issuer: str, subject: str, verified: bool
) -> int | None:
    issuer_hash, subject_hash = _verified_digests(issuer=issuer, subject=subject, verified=verified)
    async with Session() as session:
        row = (
            await session.execute(
                select(ExternalIdentity).where(
                    ExternalIdentity.provider == provider.strip().lower(),
                    ExternalIdentity.issuer_hash == issuer_hash,
                    ExternalIdentity.subject_hash == subject_hash,
                    ExternalIdentity.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.last_seen_at = db_dt(datetime.now(timezone.utc))
        await session.commit()
        return row.user_id
