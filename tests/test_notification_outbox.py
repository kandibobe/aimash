"""Durable incident delivery: enqueue CAS, lease recovery, retry, and secret-safe errors."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, update

from core.config import Settings
from db.models import NotificationOutbox, NotificationRoute, OpsIncident
from db.session import Session, db_dt
from operations.incidents import record_incident, transition_incident
from operations.outbox import (
    claim_delivery_batch,
    deliver_outbox,
    enqueue_due_escalations,
    mark_delivery_failed,
)
from operations.routing import AlertRouter
from operations.types import IncidentInput
from scheduler.ops_delivery import TelegramConfigTransport


def _customer_id() -> str:
    return str(uuid.uuid4().int)[:10]


@pytest.fixture(autouse=True)
async def _cleanup_test_routes():
    async def _clear() -> None:
        async with Session() as session:
            # enqueue_due_escalations намеренно сканирует ВСЕ due incidents. Значит тест обязан
            # изолировать эти три глобальные очереди целиком, а не только свои route_uid: иначе
            # результат зависит от порядка запуска с tests/test_operations_layer.py.
            await session.execute(delete(NotificationOutbox))
            await session.execute(delete(OpsIncident))
            await session.execute(delete(NotificationRoute))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _incident(
    customer_id: str, *, severity: str = "critical", title: str = "Tracking is down"
) -> OpsIncident:
    return await record_incident(
        IncidentInput(
            customer_id=customer_id,
            kind="tracking",
            severity=severity,
            title=title,
            fingerprint_fields={"marker": uuid.uuid4().hex},
        )
    )


async def _route(*, route_uid: str, customer_id: str, channel: str = "telegram") -> str:
    stored_uid = f"outtest_{route_uid}"
    async with Session() as session:
        session.add(
            NotificationRoute(
                route_uid=stored_uid,
                customer_id=customer_id,
                channel=channel,
                destination_ref="OPS_ALERT_DESTINATION",
                severities=["critical", "warning"],
                enabled=True,
                created_at=db_dt(datetime.now(timezone.utc)),
            )
        )
        await session.commit()
    return stored_uid


async def test_enqueue_is_atomic_concurrent_and_customer_route_overrides_global():
    customer_id = _customer_id()
    incident = await _incident(customer_id, title="Tracking sk-or-1234567890abcdefghij is down")
    await _route(route_uid=f"global_{uuid.uuid4().hex}", customer_id="*")
    customer_route = await _route(route_uid=f"customer_{uuid.uuid4().hex}", customer_id=customer_id)
    point = datetime.now(timezone.utc)
    async with Session() as session:
        await session.execute(
            update(OpsIncident)
            .where(OpsIncident.incident_uid == incident.incident_uid)
            .values(first_seen_at=db_dt(point - timedelta(hours=1)))
        )
        await session.commit()

    batches = await asyncio.gather(
        enqueue_due_escalations(
            now=point,
            critical_after=timedelta(0),
            warning_after=timedelta(0),
        ),
        enqueue_due_escalations(
            now=point,
            critical_after=timedelta(0),
            warning_after=timedelta(0),
        ),
    )
    assert sum(map(len, batches)) == 1
    async with Session() as session:
        rows = list(
            (
                await session.execute(
                    select(NotificationOutbox).where(
                        NotificationOutbox.incident_uid == incident.incident_uid
                    )
                )
            )
            .scalars()
            .all()
        )
        current = (
            await session.execute(
                select(OpsIncident).where(OpsIncident.incident_uid == incident.incident_uid)
            )
        ).scalar_one()
    assert len(rows) == 1
    assert rows[0].route_uid == customer_route
    assert "sk-or-1234567890abcdefghij" not in rows[0].title
    assert current.escalation_level == 1
    assert not await enqueue_due_escalations(
        now=point,
        critical_after=timedelta(0),
        warning_after=timedelta(0),
    )


async def test_incident_without_valid_route_does_not_advance_escalation_cursor():
    incident = await _incident(_customer_id())
    point = datetime.now(timezone.utc)
    assert not await enqueue_due_escalations(
        now=point,
        critical_after=timedelta(0),
        warning_after=timedelta(0),
    )
    async with Session() as session:
        current = (
            await session.execute(
                select(OpsIncident).where(OpsIncident.incident_uid == incident.incident_uid)
            )
        ).scalar_one()
    assert current.escalation_level == 0
    assert current.last_notified_at is None


async def test_concurrent_workers_get_one_live_lease_and_expired_lease_is_bounded():
    customer_id = _customer_id()
    incident = await _incident(customer_id)
    await _route(route_uid=f"route_{uuid.uuid4().hex}", customer_id=customer_id)
    point = datetime.now(timezone.utc)
    await enqueue_due_escalations(
        now=point,
        critical_after=timedelta(0),
        warning_after=timedelta(0),
    )
    batches = await asyncio.gather(
        claim_delivery_batch(now=point, lease_for=timedelta(seconds=10), max_attempts=2),
        claim_delivery_batch(now=point, lease_for=timedelta(seconds=10), max_attempts=2),
    )
    claimed = [row for batch in batches for row in batch]
    assert len(claimed) == 1
    assert claimed[0].attempts == 1

    reclaimed = await claim_delivery_batch(
        now=point + timedelta(seconds=11),
        lease_for=timedelta(seconds=10),
        max_attempts=2,
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].attempts == 2
    assert not await claim_delivery_batch(
        now=point + timedelta(seconds=22),
        lease_for=timedelta(seconds=10),
        max_attempts=2,
    )
    async with Session() as session:
        row = (
            await session.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.incident_uid == incident.incident_uid
                )
            )
        ).scalar_one()
    assert row.state == "dead"
    assert row.last_error_code == "LeaseExpired"


async def test_delivery_retries_then_dead_letters_without_persisting_exception_message():
    customer_id = _customer_id()
    incident = await _incident(customer_id)
    await _route(route_uid=f"route_{uuid.uuid4().hex}", customer_id=customer_id, channel="slack")
    point = datetime.now(timezone.utc)
    await enqueue_due_escalations(
        now=point,
        critical_after=timedelta(0),
        warning_after=timedelta(0),
    )

    class FailingTransport:
        async def send(self, *, destination_ref, notification):
            raise RuntimeError("sk-or-1234567890abcdefghij")

    router = AlertRouter({"slack": FailingTransport()})
    first = await deliver_outbox(
        router,
        now=point,
        max_attempts=2,
        base_delay=timedelta(seconds=10),
    )
    assert first.retrying == 1
    assert (await deliver_outbox(router, now=point + timedelta(seconds=9))).claimed == 0
    second = await deliver_outbox(
        router,
        now=point + timedelta(seconds=10),
        max_attempts=2,
        base_delay=timedelta(seconds=10),
    )
    assert second.dead == 1
    async with Session() as session:
        row = (
            await session.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.incident_uid == incident.incident_uid
                )
            )
        ).scalar_one()
    assert row.state == "dead"
    assert row.attempts == 2
    assert row.last_error_code == "RuntimeError"
    assert "sk-or" not in row.last_error_code


async def test_failure_api_rejects_message_shaped_error_codes():
    customer_id = _customer_id()
    await _incident(customer_id)
    await _route(route_uid=f"route_{uuid.uuid4().hex}", customer_id=customer_id)
    point = datetime.now(timezone.utc)
    await enqueue_due_escalations(
        now=point,
        critical_after=timedelta(0),
        warning_after=timedelta(0),
    )
    row = (await claim_delivery_batch(now=point))[0]
    assert (
        await mark_delivery_failed(
            row.outbox_uid,
            lease_token=row.lease_token or "",
            error_code="RuntimeError: sk-or-1234567890abcdefghij",
            now=point,
        )
        == "pending"
    )
    async with Session() as session:
        current = (
            await session.execute(select(NotificationOutbox).where(NotificationOutbox.id == row.id))
        ).scalar_one()
    assert current.last_error_code == "DeliveryError"


async def test_success_is_persisted_and_resolved_incident_cancels_pending_delivery():
    customer_id = _customer_id()
    incident = await _incident(customer_id)
    await _route(route_uid=f"route_{uuid.uuid4().hex}", customer_id=customer_id)
    point = datetime.now(timezone.utc)
    await enqueue_due_escalations(
        now=point,
        critical_after=timedelta(0),
        warning_after=timedelta(0),
    )
    sent: list[str] = []

    class SuccessfulTransport:
        async def send(self, *, destination_ref, notification):
            sent.append(notification.dedup_key)

    result = await deliver_outbox(AlertRouter({"telegram": SuccessfulTransport()}), now=point)
    assert result.delivered == 1
    assert len(sent) == 1
    async with Session() as session:
        delivered = (
            await session.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.incident_uid == incident.incident_uid
                )
            )
        ).scalar_one()
    assert delivered.state == "delivered"
    assert delivered.delivered_at is not None

    second = await _incident(_customer_id())
    await _route(route_uid=f"route_{uuid.uuid4().hex}", customer_id=second.customer_id)
    second_point = datetime.now(timezone.utc)
    await enqueue_due_escalations(
        now=second_point,
        critical_after=timedelta(0),
        warning_after=timedelta(0),
    )
    assert await transition_incident(second.incident_uid, "resolved", actor_user_id=42)
    assert (
        await deliver_outbox(AlertRouter({"telegram": SuccessfulTransport()}), now=second_point)
    ).claimed == 0
    async with Session() as session:
        cancelled = (
            await session.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.incident_uid == second.incident_uid
                )
            )
        ).scalar_one()
    assert cancelled.state == "cancelled"


def test_outbox_runtime_policy_is_fail_fast():
    with pytest.raises(ValidationError, match="NOTIFICATION_OUTBOX_LEASE_SECONDS"):
        Settings(notification_outbox_lease_seconds=0)
    with pytest.raises(ValidationError, match="INCIDENT_ESCALATION_COOLDOWN_MINUTES"):
        Settings(incident_escalation_cooldown_minutes=0)


async def test_telegram_transport_resolves_reference_only_at_send_time(monkeypatch):
    calls: list[dict] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append({"chat_id": chat_id, "text": text, **kwargs})

    transport = TelegramConfigTransport(FakeBot())
    notification = type(
        "NotificationStub",
        (),
        {"severity": "critical", "title": "<tracking>", "body": "Investigate & repair"},
    )()
    monkeypatch.setenv("OPS_ALERT_DESTINATION", "-1001234567890")
    await transport.send(
        destination_ref="OPS_ALERT_DESTINATION",
        notification=notification,
    )
    assert calls[0]["chat_id"] == -1001234567890
    assert "&lt;tracking&gt;" in calls[0]["text"]
    monkeypatch.delenv("OPS_ALERT_DESTINATION")
    with pytest.raises(RuntimeError, match="missing or invalid"):
        await transport.send(
            destination_ref="OPS_ALERT_DESTINATION",
            notification=notification,
        )
