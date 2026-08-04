from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from confirm.store import (
    ConfirmStore,
    PendingProposalExists,
    SavedProposal,
    proposal_idempotency_key,
)
from core.provenance import human_turn
from db.models import Proposal
from db.session import Session


@pytest.mark.asyncio
async def test_database_allows_only_one_pending_proposal_per_human_turn():
    """The unique partial index closes the SELECT-before-INSERT race between Hermes tool calls."""
    store = ConfirmStore()

    async def save(operation: str) -> SavedProposal:
        return await store.save_proposal(
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

    assert sum(isinstance(result, SavedProposal) for result in results) == 1
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


def test_idempotency_key_uses_canonical_json_arguments():
    first = proposal_idempotency_key(
        chat_id=777,
        message_id=313,
        operation="pause_campaign",
        json_args={"account": "7753643025", "campaign": "Search", "labels": ["a", "b"]},
    )
    reordered = proposal_idempotency_key(
        chat_id=777,
        message_id=313,
        operation="pause_campaign",
        json_args={"labels": ["a", "b"], "campaign": "Search", "account": "7753643025"},
    )
    other_message = proposal_idempotency_key(
        chat_id=777,
        message_id=314,
        operation="pause_campaign",
        json_args={"account": "7753643025", "campaign": "Search", "labels": ["a", "b"]},
    )

    assert first == reordered
    assert first != other_message
    assert len(first) == 64


@pytest.mark.asyncio
async def test_replayed_proposal_create_returns_the_existing_row():
    store = ConfirmStore()
    args = {"account": "7753643025", "campaign": "Replay Search"}

    async def save(confirmation_id: str) -> SavedProposal:
        return await store.save_proposal(
            confirmation_id=confirmation_id,
            operation="pause_campaign",
            customer_id="7753643025",
            params={"campaign": "Replay Search"},
            summary="ENABLED → PAUSED",
            chat_id=778,
            user_initiated=True,
            source_message_id=515,
            idempotency_args=args,
        )

    with human_turn(actor_user_id=42, run_id="idempotent_run"):
        results = await asyncio.gather(
            save("a" * 32),
            save("b" * 32),
        )

    assert {result.confirmation_id for result in results} in ({"a" * 32}, {"b" * 32})
    assert sorted(result.reused for result in results) == [False, True]
    key = proposal_idempotency_key(
        chat_id=778,
        message_id=515,
        operation="pause_campaign",
        json_args=args,
    )
    async with Session() as session:
        rows = (
            (await session.execute(select(Proposal).where(Proposal.idempotency_key == key)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
