from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from ads.service import SUPPORTED_OPERATIONS
from confirm.policy import (
    AUTONOMOUS_ADS_OPS,
    AUTONOMOUS_MEMORY_OPS,
    CONFIRM_REQUIRED_ADS_OPS,
    may_execute_autonomously,
    requires_confirmation,
)
from confirm.store import ConfirmStore
from db.models import AuditLog
from db.session import Session


def test_policy_is_total_disjoint_and_unknown_fails_closed():
    assert AUTONOMOUS_ADS_OPS.isdisjoint(CONFIRM_REQUIRED_ADS_OPS)
    assert AUTONOMOUS_ADS_OPS | CONFIRM_REQUIRED_ADS_OPS == SUPPORTED_OPERATIONS
    assert may_execute_autonomously("update_campaign") is True
    assert may_execute_autonomously("create_search_campaign") is True
    assert requires_confirmation("add_keywords") is True
    assert requires_confirmation("set_geo_location") is True
    assert requires_confirmation("create_rsa") is True
    assert requires_confirmation("update_budget") is True
    assert AUTONOMOUS_MEMORY_OPS == {"profile_save", "profile_update"}
    assert requires_confirmation("profile_save") is False
    assert requires_confirmation("profile_update") is False
    assert requires_confirmation("profile_clear") is True
    assert requires_confirmation("future_unknown_operation") is True


@pytest.mark.asyncio
async def test_store_auto_authorizes_only_allowlisted_non_spend_operation():
    store = ConfirmStore()
    autonomous_cid = uuid.uuid4().hex
    money_cid = uuid.uuid4().hex
    for cid, operation in (
        (autonomous_cid, "update_campaign"),
        (money_cid, "update_budget"),
    ):
        await store.save_proposal(
            confirmation_id=cid,
            operation=operation,
            customer_id="7753643025",
            params={},
            summary=operation,
            chat_id=777,
        )

    assert await store.authorize_autonomous(autonomous_cid, operation="update_campaign") is True
    assert (await store.get_confirmed(autonomous_cid)).status == "confirmed"
    assert await store.authorize_autonomous(money_cid, operation="update_budget") is False
    assert (await store.get_confirmed(money_cid)).status == "pending"

    async with Session() as session:
        statuses = list(
            (
                await session.execute(
                    select(AuditLog.status).where(AuditLog.confirmation_id == autonomous_cid)
                )
            ).scalars()
        )
    assert statuses == ["auto_authorized"]
