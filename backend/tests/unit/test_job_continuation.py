"""A finished pipeline stage may wake its session to carry the workflow on.

Ordinary completion writes a receipt and stops — deliberately, because an
unconditional wake manufactures fake user turns, burns tokens and can loop. But
a multi-stage pipeline stalls without one: generate finishes, and nothing ever
starts transcription until a human nudges it.

This mirrors codex's `trigger_turn`: the queue takes the item either way, and
the declaration only decides whether an idle session is woken for it.
"""
import uuid

import pytest

from db.base import get_db_session
from db.models.session_inbox import SessionInbox
from skill_runtime import repository as repo
from skill_runtime.inbox import _is_write_back_kind
from skill_runtime.types import Succeeded
from sqlalchemy import select


async def _inbox_rows(job_id: str) -> list[SessionInbox]:
    async with get_db_session() as db:
        return list(
            (
                await db.execute(
                    select(SessionInbox).where(SessionInbox.source_job_id == job_id)
                )
            ).scalars()
        )


async def _admit(*, continue_on_success: bool, session_id: str | None):
    job, _ = await repo.admit_job(
        user_id="u_" + uuid.uuid4().hex[:8],
        skill_key="builtin:demo-echo",
        operation="echo",
        runtime_kind="internal",
        input_data={"text": "x"},
        idempotency_key="k-" + uuid.uuid4().hex[:8],
        # Tests share one database; a per-call queue keeps claim_next from
        # picking up another test's queued job.
        queue_name="q_" + uuid.uuid4().hex[:8],
        session_id=session_id,
        continue_agent_on_success=continue_on_success,
    )
    return job


async def _settle_success(job):
    claimed = await repo.claim_next(
        queues=(job.queue_name,), worker_id="w1", lease_seconds=60, limit=1
    )
    assert len(claimed) == 1 and claimed[0].job.id == job.id
    handle = claimed[0]
    await repo.settle_invocation(
        job.id, handle.lease_token, Succeeded(result={"echo": "x"})
    )
    return handle


@pytest.mark.asyncio
async def test_a_declared_stage_enqueues_a_continuation_on_success():
    session_id = "session_" + uuid.uuid4().hex[:10]
    job = await _admit(continue_on_success=True, session_id=session_id)
    await _settle_success(job)

    rows = await _inbox_rows(job.id)
    assert len(rows) == 1
    assert rows[0].kind == "job_completed"
    assert rows[0].status == "pending"
    assert rows[0].session_id == session_id
    # The result travels with the notice so the turn needs no extra lookup.
    assert rows[0].payload["result"] == {"echo": "x"}
    assert rows[0].payload["operation"] == "echo"


@pytest.mark.asyncio
async def test_an_undeclared_operation_still_just_writes_a_receipt():
    """The default must not change: silence is not consent to spend tokens."""
    job = await _admit(continue_on_success=False, session_id="session_" + uuid.uuid4().hex[:10])
    await _settle_success(job)
    assert await _inbox_rows(job.id) == []


@pytest.mark.asyncio
async def test_a_job_with_no_session_has_nowhere_to_wake():
    """Cron and API-started jobs have no conversation to continue."""
    job = await _admit(continue_on_success=True, session_id=None)
    await _settle_success(job)
    assert await _inbox_rows(job.id) == []


@pytest.mark.asyncio
async def test_exactly_one_notice_per_job():
    """Consume-on-read plus one row per settle is what bounds the loop.

    A second notice for the same job would let one finished stage wake the
    session twice, and each wake can start another job.
    """
    session_id = "session_" + uuid.uuid4().hex[:10]
    job = await _admit(continue_on_success=True, session_id=session_id)
    handle = await _settle_success(job)
    # A stale lease token cannot settle again, so no second row appears.
    with pytest.raises(Exception):
        await repo.settle_invocation(job.id, handle.lease_token, Succeeded(result={}))
    assert len(await _inbox_rows(job.id)) == 1


def test_only_a_parked_job_is_owed_an_agent_result():
    """A terminal job cannot accept inputs; writing one back would be rejected."""
    assert _is_write_back_kind(SessionInbox(kind="job_needs_agent")) is True
    assert _is_write_back_kind(SessionInbox(kind="job_completed")) is False
