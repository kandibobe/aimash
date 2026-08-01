from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from confirm.store import ConfirmStore, PendingProposalExists
from core.provenance import human_turn
from db.models import Proposal
from db.session import Session


@pytest.mark.asyncio
async def test_database_allows_only_one_pending_proposal_per_human_turn():
    """The unique partial index closes the SELECT-before-INSERT race between Hermes tool calls."""
    store = ConfirmStore()

    async def save(operation: str) -> None:
        await store.save_proposal(
            confirmation_id=uuid.uuid4().hex,
            operation=operation,
            customer_id="7753643025",
            params={},
            summary=operation,
            chat_id=777,
            user_initiated=True,
        )

    with human_turn(actor_user_id=42, run_id="same_parallel_run"):
        results = await asyncio.gather(
            save("update_campaign"),
            save("update_budget"),
            return_exceptions=True,
        )

    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, PendingProposalExists) for result in results) == 1
    async with Session() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Proposal)
                .where(Proposal.run_id == "same_parallel_ru", Proposal.status == "pending")
            )
        ).scalar_one()
    assert count == 1
