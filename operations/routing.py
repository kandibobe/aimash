"""Channel-neutral alert routing with secret references and injected transports."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select

from core.access import is_admin
from core.logging import redact_text
from db.models import NotificationRoute
from db.session import Session, db_dt
from operations.governance import has_capability


@dataclass(frozen=True)
class Route:
    channel: str
    destination_ref: str
    severities: frozenset[str]

    def __post_init__(self) -> None:
        if self.channel not in {"telegram", "slack", "email", "teams", "webhook"}:
            raise ValueError("unsupported route channel")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", self.destination_ref) is None:
            raise ValueError("destination_ref must be an uppercase config reference, not a value")
        if not self.severities <= {"info", "warning", "critical"}:
            raise ValueError("invalid route severity")


@dataclass(frozen=True)
class Notification:
    dedup_key: str
    severity: str
    title: str
    body: str


class Transport(Protocol):
    async def send(self, *, destination_ref: str, notification: Notification) -> None: ...


class AlertRouter:
    """Dispatch via configured adapters; missing adapter is a visible failure, never a silent skip."""

    def __init__(self, transports: dict[str, Transport]) -> None:
        self._transports = dict(transports)

    async def deliver(self, notification: Notification, routes: list[Route]) -> list[str]:
        if notification.severity not in {"info", "warning", "critical"}:
            raise ValueError("invalid notification severity")
        safe_notification = Notification(
            dedup_key=notification.dedup_key,
            severity=notification.severity,
            title=redact_text(notification.title),
            body=redact_text(notification.body),
        )
        delivered: list[str] = []
        for route in routes:
            if notification.severity not in route.severities:
                continue
            transport = self._transports.get(route.channel)
            if transport is None:
                raise RuntimeError(f"transport not configured: {route.channel}")
            await transport.send(
                destination_ref=route.destination_ref, notification=safe_notification
            )
            delivered.append(route.channel)
        return delivered


async def save_route(*, actor_user_id: int, customer_id: str, route: Route) -> NotificationRoute:
    if not await is_admin(actor_user_id) and not await has_capability(
        actor_user_id, customer_id, "manage_roles"
    ):
        raise PermissionError("notification routing requires admin capability")
    async with Session() as session:
        row = (
            await session.execute(
                select(NotificationRoute).where(
                    NotificationRoute.customer_id == customer_id,
                    NotificationRoute.channel == route.channel,
                    NotificationRoute.destination_ref == route.destination_ref,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = NotificationRoute(
                route_uid=f"route_{secrets.token_hex(12)}",
                customer_id=customer_id,
                channel=route.channel,
                destination_ref=route.destination_ref,
                severities=sorted(route.severities),
                enabled=True,
                created_by=actor_user_id,
                created_at=db_dt(datetime.now(timezone.utc)),
            )
            session.add(row)
        else:
            row.severities = sorted(route.severities)
            row.enabled = True
        await session.commit()
        await session.refresh(row)
        return row


async def load_routes(customer_id: str) -> list[Route]:
    async with Session() as session:
        rows = list(
            (
                await session.execute(
                    select(NotificationRoute).where(
                        NotificationRoute.customer_id.in_((customer_id, "*")),
                        NotificationRoute.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
    # A customer-specific row overrides an identical global route. Without this collapse the same
    # destination receives every alert twice when an agency default is later customized per client.
    effective: dict[tuple[str, str], Route] = {}
    for row in sorted(rows, key=lambda item: (item.customer_id != "*", item.id), reverse=True):
        key = (row.channel, row.destination_ref)
        if key in effective:
            continue
        effective[key] = Route(
            channel=row.channel,
            destination_ref=row.destination_ref,
            severities=frozenset(row.severities),
        )
    return list(effective.values())
