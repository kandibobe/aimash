from __future__ import annotations

from sqlalchemy import select

from ads.client import DRAFT_ACCOUNT_ID
from confirm.store import ConfirmStore
from core.context import reset_context, set_context
from core.provenance import human_turn
from db.models import AuditLog, Proposal
from db.session import Session, init_db
from mcp_server.tools_plan import cancel_proposal, list_pending_proposals


async def _proposal(*, cid: str, chat_id: int, actor_user_id: int) -> None:
    token = set_context(request_id=f"run-{cid}", chat_id=chat_id)
    try:
        with human_turn(actor_user_id=actor_user_id, run_id=f"run-{cid}"):
            await ConfirmStore().save_proposal(
                confirmation_id=cid,
                operation="pause_campaign",
                customer_id=DRAFT_ACCOUNT_ID,
                summary=f"proposal {cid}",
                params={"campaign": "Test"},
                chat_id=chat_id,
                user_initiated=True,
            )
    finally:
        reset_context(token)


async def test_list_pending_is_scoped_to_trusted_chat_and_author():
    await init_db()
    await _proposal(cid="own", chat_id=100, actor_user_id=7)
    await _proposal(cid="other-author", chat_id=100, actor_user_id=8)
    await _proposal(cid="other-chat", chat_id=200, actor_user_id=7)

    token = set_context(request_id="list", chat_id=100)
    try:
        with human_turn(actor_user_id=7, run_id="list"):
            result = await list_pending_proposals()
    finally:
        reset_context(token)

    assert [row["confirmation_id"] for row in result["rows"]] == ["own"]


async def test_cancel_is_actor_scoped_atomic_and_audited():
    await init_db()
    await _proposal(cid="cancel-me", chat_id=300, actor_user_id=9)

    wrong = set_context(request_id="wrong", chat_id=300)
    try:
        with human_turn(actor_user_id=10, run_id="wrong"):
            refused = await cancel_proposal("cancel-me")
    finally:
        reset_context(wrong)
    assert refused["status"] == "refused"

    right = set_context(request_id="right", chat_id=300)
    try:
        with human_turn(actor_user_id=9, run_id="right"):
            result = await cancel_proposal("cancel-me")
            replay = await cancel_proposal("cancel-me")
    finally:
        reset_context(right)

    assert result["status"] == "rejected"
    assert replay["status"] == "refused"
    async with Session() as session:
        proposal = (
            await session.execute(select(Proposal).where(Proposal.confirmation_id == "cancel-me"))
        ).scalar_one()
        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.confirmation_id == "cancel-me")))
            .scalars()
            .all()
        )
    assert proposal.status == "rejected"
    assert [(row.status, row.actor_user_id) for row in audits] == [("rejected", 9)]


async def test_plan_state_tools_fail_closed_without_human_context():
    listed = await list_pending_proposals()
    cancelled = await cancel_proposal("anything")
    assert listed["status"] == "refused"
    assert cancelled["status"] == "refused"
