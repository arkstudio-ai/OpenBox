"""Cold recovery closes an interrupted tail without replaying tools."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from agent.driver import (
    RecoveredDriver,
    RunLease,
    get_driver_state,
    recover_expired_driver_records,
    reserve_recovered_run,
    reserve_run,
)
from agent import recovery as recovery_module
from agent.recovery import (
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    repair_expired_sessions,
)
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from models.message import StepStartPart, ToolPartData, ToolStatus
from session.agent_event_log import verify_agent_event_parity
from session.session import (
    create_assistant_message,
    create_user_message,
    save_part,
    update_message_info,
)


async def _seed_recovery_session(prefix: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"{prefix}-user-{suffix}"
    project_id = f"{prefix}-project-{suffix}"
    session_id = f"{prefix}-session-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=f"{prefix}-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Recovery protocol",
            slug=f"{prefix}-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            agent="build",
            model="openai/test",
            status="idle",
            created_at=now,
            updated_at=now,
        ))
    return user_id, session_id


async def _expire(lease: RunLease) -> None:
    await lease.stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == lease.session_id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )


@pytest.mark.asyncio
async def test_new_prompt_repairs_expired_tool_tail_under_original_event_identity():
    user_id, session_id = await _seed_recovery_session("prompt-takeover")
    trigger_id = f"message_{uuid.uuid4().hex[:16]}"
    old = await reserve_run(
        session_id,
        user_id,
        run_id=f"old-run-{uuid.uuid4().hex[:10]}",
        trigger_message_id=trigger_id,
    )
    fence = (session_id, old.run_id, old.generation)
    user = await create_user_message(
        session_id,
        "first prompt",
        user_id=user_id,
        message_id=trigger_id,
        run_fence=fence,
    )
    assistant = await create_assistant_message(
        session_id,
        user.id,
        model_id="openai/test",
        agent="build",
        user_id=user_id,
        run_fence=fence,
    )
    await save_part(
        StepStartPart(
            step=1,
            session_id=session_id,
            message_id=assistant.id,
        ),
        is_new=True,
        user_id=user_id,
        run_fence=fence,
    )
    tool = ToolPartData(
        tool="write",
        status=ToolStatus.RUNNING,
        input={"path": "report.txt"},
        call_id="call-expired",
        session_id=session_id,
        message_id=assistant.id,
    )
    await save_part(
        tool,
        is_new=True,
        user_id=user_id,
        run_fence=fence,
    )
    await _expire(old)

    from api.sessions import _reserve_prompt_run

    replacement = await _reserve_prompt_run(session_id, user_id)
    assert replacement.generation == old.generation + 2
    assert await old.release(session_status="error") is False

    async with get_db_session() as db:
        repaired_message = (
            await db.execute(select(Message).where(Message.id == assistant.id))
        ).scalar_one()
        repaired_tool = (
            await db.execute(select(Part).where(Part.id == tool.id))
        ).scalar_one()
    assert repaired_message.finish == "aborted"
    assert repaired_tool.data["status"] == "error"
    assert repaired_tool.data["metadata"]["recovery_code"] == TOOL_OUTCOME_UNKNOWN

    report = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert report.ok is True, report.model_dump()
    assert await replacement.release(session_status="idle") is True


@pytest.mark.asyncio
async def test_recovery_materializes_aborted_assistant_for_bound_unanswered_turn():
    user_id, session_id = await _seed_recovery_session("unanswered")
    trigger_id = f"message_{uuid.uuid4().hex[:16]}"
    old = await reserve_run(
        session_id,
        user_id,
        run_id=f"accepted-run-{uuid.uuid4().hex[:10]}",
        trigger_message_id=trigger_id,
    )
    await create_user_message(
        session_id,
        "accepted but not started",
        user_id=user_id,
        message_id=trigger_id,
        run_fence=(session_id, old.run_id, old.generation),
    )
    await _expire(old)
    record = next(
        item
        for item in await recover_expired_driver_records()
        if item.session_id == session_id
    )

    results = await repair_expired_sessions([record])
    assert len(results) == 1
    assert results[0].closed_messages == 1
    async with get_db_session() as db:
        reply = (
            await db.execute(select(Message).where(
                Message.session_id == session_id,
                Message.parent_id == trigger_id,
                Message.role == "assistant",
            ))
        ).scalar_one()
    assert reply.finish == "aborted"
    report = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert report.ok is True, report.model_dump()


@pytest.mark.asyncio
async def test_recovery_only_repairs_the_bound_trigger_turn():
    user_id, session_id = await _seed_recovery_session("scoped-repair")
    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc)
    historical_user = f"historical-user-{suffix}"
    historical_assistant = f"historical-assistant-{suffix}"
    current_user = f"current-user-{suffix}"
    current_assistant = f"current-assistant-{suffix}"
    historical_tool = f"historical-tool-{suffix}"
    current_tool = f"current-tool-{suffix}"
    async with get_db_session() as db:
        db.add_all([
            Message(
                id=historical_user,
                session_id=session_id,
                user_id=user_id,
                role="user",
                created_at=now,
            ),
            Message(
                id=historical_assistant,
                session_id=session_id,
                user_id=user_id,
                role="assistant",
                parent_id=historical_user,
                finish="tool_calls",
                created_at=now + timedelta(microseconds=1),
            ),
            Message(
                id=current_user,
                session_id=session_id,
                user_id=user_id,
                role="user",
                created_at=now + timedelta(microseconds=2),
            ),
            Message(
                id=current_assistant,
                session_id=session_id,
                user_id=user_id,
                role="assistant",
                parent_id=current_user,
                finish="tool_calls",
                created_at=now + timedelta(microseconds=3),
            ),
            Part(
                id=historical_tool,
                message_id=historical_assistant,
                session_id=session_id,
                user_id=user_id,
                type="tool",
                data={
                    "type": "tool",
                    "id": historical_tool,
                    "status": "pending",
                    "session_id": session_id,
                    "message_id": historical_assistant,
                },
                created_at=now + timedelta(microseconds=4),
            ),
            Part(
                id=current_tool,
                message_id=current_assistant,
                session_id=session_id,
                user_id=user_id,
                type="tool",
                data={
                    "type": "tool",
                    "id": current_tool,
                    "status": "running",
                    "session_id": session_id,
                    "message_id": current_assistant,
                },
                created_at=now + timedelta(microseconds=5),
            ),
            AgentDriverState(
                session_id=session_id,
                user_id=user_id,
                generation=3,
                run_id=f"expired-{suffix}",
                owner_id="dead-worker",
                phase="running",
                trigger_message_id=current_user,
                lease_expires_at=now - timedelta(seconds=1),
                started_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(seconds=1),
            ),
        ])

    record = next(
        item
        for item in await recover_expired_driver_records()
        if item.session_id == session_id
    )
    results = await repair_expired_sessions([record])
    assert len(results) == 1
    assert results[0].repaired_tools == 1
    async with get_db_session() as db:
        historical = (
            await db.execute(select(Part).where(Part.id == historical_tool))
        ).scalar_one()
        current = (
            await db.execute(select(Part).where(Part.id == current_tool))
        ).scalar_one()
    assert historical.data["status"] == "pending"
    assert current.data["status"] == "error"
    assert current.data["metadata"]["recovery_code"] == TOOL_OUTCOME_UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_phase", ["running", "finalizing"])
async def test_direct_trigger_late_steer_recovers_claimed_boundary_tail(crash_phase):
    from agent.inbox import accept_inbox_item, claim_inbox_boundary

    user_id, session_id = await _seed_recovery_session(
        f"direct-late-{crash_phase}"
    )
    trigger_id = f"message_{uuid.uuid4().hex[:16]}"
    lease = await reserve_run(
        session_id,
        user_id,
        run_id=f"direct-{uuid.uuid4().hex[:10]}",
        trigger_message_id=trigger_id,
    )
    fence = (session_id, lease.run_id, lease.generation)
    direct = await create_user_message(
        session_id,
        "direct command",
        user_id=user_id,
        message_id=trigger_id,
        run_fence=fence,
    )
    first_assistant = await create_assistant_message(
        session_id,
        direct.id,
        model_id="openai/test",
        user_id=user_id,
        run_fence=fence,
    )
    first_assistant.finish = "tool_calls"
    await update_message_info(first_assistant, user_id=user_id, run_fence=fence)

    await accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="steer",
        prompt="late steer",
    )
    claimed = await claim_inbox_boundary(
        lease,
        step=2,
        include_next_turn=False,
    )
    assert len(claimed.receipts) == 1
    late_id = claimed.receipts[0].message_id
    assert claimed.receipts[0].turn_id == trigger_id
    terminal = await create_assistant_message(
        session_id,
        late_id,
        model_id="openai/test",
        user_id=user_id,
        run_fence=fence,
    )
    tool = ToolPartData(
        tool="write",
        status=ToolStatus.RUNNING,
        input={"path": "late.txt"},
        call_id="late-running",
        session_id=session_id,
        message_id=terminal.id,
    )
    await save_part(tool, is_new=True, user_id=user_id, run_fence=fence)
    await lease.set_phase("running")
    if crash_phase == "finalizing":
        await lease.set_phase("finalizing")
    await _expire(lease)

    record = next(
        item
        for item in await recover_expired_driver_records()
        if item.session_id == session_id
    )
    valid, answered_id, _assets = await recovery_module._trigger_state(record)
    assert valid is True
    assert answered_id == terminal.id
    results = await repair_expired_sessions([record])
    assert len(results) == 1
    assert results[0].repaired_tools == 1
    async with get_db_session() as db:
        repaired = await db.get(Message, terminal.id)
        repaired_tool = await db.get(Part, tool.id)
        inbox_row = (await db.execute(select(AgentInboxItem).where(
            AgentInboxItem.session_id == session_id,
        ))).scalar_one()
    assert repaired is not None and repaired.finish == "aborted"
    assert repaired_tool is not None
    assert repaired_tool.data["metadata"]["recovery_code"] == TOOL_OUTCOME_UNKNOWN
    assert inbox_row.state == "settled"
    assert inbox_row.result_message_id == terminal.id
    assert (await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )).ok is True


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["direct", "inbox"])
async def test_recovery_closes_late_claimed_user_after_prior_terminal(origin):
    """A prior terminal reply cannot settle a later User in the same turn."""
    from agent.inbox import accept_inbox_item, claim_inbox_boundary

    user_id, session_id = await _seed_recovery_session(
        f"late-dangling-{origin}"
    )
    if origin == "inbox":
        await accept_inbox_item(
            session_id=session_id,
            user_id=user_id,
            delivery="followup",
            prompt="initial inbox prompt",
        )
        lease = await reserve_run(
            session_id,
            user_id,
            run_id=f"inbox-{uuid.uuid4().hex[:10]}",
        )
        first_batch = await claim_inbox_boundary(
            lease,
            step=1,
            include_next_turn=True,
        )
        trigger_id = first_batch.receipts[0].message_id
        assert trigger_id is not None
    else:
        trigger_id = f"message_{uuid.uuid4().hex[:16]}"
        lease = await reserve_run(
            session_id,
            user_id,
            run_id=f"direct-{uuid.uuid4().hex[:10]}",
            trigger_message_id=trigger_id,
        )
        await create_user_message(
            session_id,
            "initial direct prompt",
            user_id=user_id,
            message_id=trigger_id,
            run_fence=(session_id, lease.run_id, lease.generation),
        )

    fence = (session_id, lease.run_id, lease.generation)
    first_assistant = await create_assistant_message(
        session_id,
        trigger_id,
        model_id="openai/test",
        user_id=user_id,
        run_fence=fence,
    )
    first_assistant.finish = "stop"
    await update_message_info(first_assistant, user_id=user_id, run_fence=fence)

    await accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="steer",
        prompt="late steer with no assistant reply",
    )
    late_batch = await claim_inbox_boundary(
        lease,
        step=2,
        include_next_turn=False,
    )
    late_id = late_batch.receipts[0].message_id
    assert late_id is not None
    assert late_batch.receipts[0].turn_id == trigger_id
    await lease.set_phase("running")
    await _expire(lease)

    before = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert before.ok is False
    record = next(
        item
        for item in await recover_expired_driver_records()
        if item.session_id == session_id
    )
    valid, answered_id, _assets = await recovery_module._trigger_state(record)
    assert valid is True
    assert answered_id is None

    results = await repair_expired_sessions([record])
    assert len(results) == 1
    assert results[0].closed_messages == 1
    async with get_db_session() as db:
        recovered_reply = (await db.execute(select(Message).where(
            Message.session_id == session_id,
            Message.user_id == user_id,
            Message.role == "assistant",
            Message.parent_id == late_id,
        ))).scalar_one()
        inbox_rows = list((await db.execute(select(AgentInboxItem).where(
            AgentInboxItem.session_id == session_id,
        ).order_by(AgentInboxItem.created_at, AgentInboxItem.id))).scalars().all())
    assert recovered_reply.finish == "aborted"
    assert all(row.state == "settled" for row in inbox_rows)
    assert all(row.result_message_id == recovered_reply.id for row in inbox_rows)
    after = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert after.ok is True, after.model_dump()


@pytest.mark.asyncio
async def test_expired_tool_tail_is_repaired_and_never_replayed():
    suffix = uuid.uuid4().hex[:12]
    user_id = f"repair-user-{suffix}"
    project_id = f"repair-project-{suffix}"
    session_id = f"repair-session-{suffix}"
    message_id = f"repair-message-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=f"repair-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Recovery",
            slug=f"repair-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            status="busy",
            created_at=now,
            updated_at=now,
        ))
        db.add(Message(
            id=message_id,
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            finish="tool_calls",
            created_at=now,
        ))
        db.add_all([
            Part(
                id=f"step-{suffix}",
                message_id=message_id,
                session_id=session_id,
                user_id=user_id,
                type="step-start",
                data={
                    "type": "step-start",
                    "id": f"step-{suffix}",
                    "step": 1,
                    "session_id": session_id,
                    "message_id": message_id,
                },
                created_at=now,
            ),
            Part(
                id=f"pending-{suffix}",
                message_id=message_id,
                session_id=session_id,
                user_id=user_id,
                type="tool",
                data={
                    "type": "tool",
                    "id": f"pending-{suffix}",
                    "tool": "write",
                    "status": "pending",
                    "session_id": session_id,
                    "message_id": message_id,
                },
                created_at=now + timedelta(microseconds=1),
            ),
            Part(
                id=f"running-{suffix}",
                message_id=message_id,
                session_id=session_id,
                user_id=user_id,
                type="tool",
                data={
                    "type": "tool",
                    "id": f"running-{suffix}",
                    "tool": "bash",
                    "status": "running",
                    "session_id": session_id,
                    "message_id": message_id,
                },
                created_at=now + timedelta(microseconds=2),
            ),
        ])
        db.add(AgentDriverState(
            session_id=session_id,
            user_id=user_id,
            generation=1,
            run_id=f"dead-{suffix}",
            owner_id="dead-worker",
            phase="running",
            lease_expires_at=now - timedelta(seconds=1),
            started_at=now - timedelta(minutes=1),
            updated_at=now - timedelta(seconds=1),
        ))

    records = await recover_expired_driver_records()
    record = next(item for item in records if item.session_id == session_id)
    results = await repair_expired_sessions([record])
    assert len(results) == 1
    assert results[0].repaired_tools == 2
    assert results[0].closed_steps == 1
    assert results[0].closed_messages == 1

    async with get_db_session() as db:
        message = (
            await db.execute(select(Message).where(Message.id == message_id))
        ).scalar_one()
        parts = list(
            (
                await db.execute(
                    select(Part)
                    .where(Part.message_id == message_id)
                    .order_by(Part.created_at, Part.id)
                )
            ).scalars().all()
        )
        session = (
            await db.execute(select(Session).where(Session.id == session_id))
        ).scalar_one()

    assert message.finish == "aborted"
    tool_parts = [part.data for part in parts if part.type == "tool"]
    assert {part["status"] for part in tool_parts} == {"error"}
    assert {
        part["metadata"]["recovery_code"] for part in tool_parts
    } == {TOOL_NOT_STARTED, TOOL_OUTCOME_UNKNOWN}
    assert any("Do not retry automatically" in part["error"] for part in tool_parts)
    assert sum(part.type == "step-finish" for part in parts) == 1
    assert session.status == "error"

    state = await get_driver_state(session_id)
    assert state is not None
    assert state.phase == "idle"
    assert state.generation == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_fails", [False, True])
async def test_reserved_accepted_prompt_is_resumed_without_replaying_running_work(
    monkeypatch,
    delivery_fails,
):
    suffix = uuid.uuid4().hex[:12]
    user_id = f"resume-user-{suffix}"
    project_id = f"resume-project-{suffix}"
    session_id = f"resume-session-{suffix}"
    message_id = f"resume-message-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=f"resume-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Resume",
            slug=f"resume-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            status="busy",
            created_at=now,
            updated_at=now,
        ))
        db.add(Message(
            id=message_id,
            session_id=session_id,
            user_id=user_id,
            role="user",
            created_at=now,
        ))
        db.add(Part(
            id=f"file-{suffix}",
            message_id=message_id,
            session_id=session_id,
            user_id=user_id,
            type="file",
            data={
                "type": "file",
                "id": f"file-{suffix}",
                "asset_id": f"asset-{suffix}",
                "session_id": session_id,
                "message_id": message_id,
            },
            created_at=now,
        ))
        db.add(AgentDriverState(
            session_id=session_id,
            user_id=user_id,
            generation=7,
            run_id=f"dead-{suffix}",
            owner_id="dead-worker",
            phase="reserved",
            trigger_message_id=message_id,
            lease_expires_at=now - timedelta(seconds=1),
            started_at=now - timedelta(minutes=1),
            updated_at=now - timedelta(seconds=1),
        ))

    delivered: list[list[str]] = []
    ran: list[tuple[str, str, int]] = []

    async def fake_deliver(
        _session_id,
        _user_id,
        asset_ids,
        *,
        strict=False,
        expected_asset_ids=None,
    ):
        assert strict is True
        assert expected_asset_ids == asset_ids
        delivered.append(asset_ids)
        if delivery_fails:
            from sandbox.assets import AssetDeliveryError

            raise AssetDeliveryError(
                expected_asset_ids=asset_ids,
                missing_asset_ids=asset_ids,
            )
        return [f"/workspace/{asset_id}" for asset_id in asset_ids]

    async def fake_run_loop(run_session_id, user_id, *, lease):
        ran.append((run_session_id, user_id, lease.generation))
        await lease.release(session_status="idle")

    import agent.loop as loop_module
    import sandbox.assets as assets_module

    monkeypatch.setattr(assets_module, "deliver_asset_ids", fake_deliver)
    monkeypatch.setattr(loop_module, "run_loop", fake_run_loop)

    records = await recover_expired_driver_records()
    record = next(item for item in records if item.session_id == session_id)
    resumed, invalid = await recovery_module.resume_reserved_prompts([record])
    tasks = list(recovery_module._resume_tasks)
    if tasks:
        await asyncio.gather(*tasks)

    assert resumed == [session_id]
    assert invalid == []
    assert delivered == [[f"asset-{suffix}"]]
    state = await get_driver_state(session_id)
    assert state is not None
    assert state.generation == 8
    if delivery_fails:
        assert ran == []
        assert state.phase == "reserved"
        assert state.run_id is not None
        expiry = state.lease_expires_at
        assert expiry is not None
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        assert expiry <= datetime.now(timezone.utc)
        async with get_db_session() as db:
            status = (await db.execute(select(Session.status).where(
                Session.id == session_id,
            ))).scalar_one()
        assert status == "error"
        cleanup = await reserve_recovered_run(
            RecoveredDriver(
                session_id=session_id,
                user_id=user_id,
                run_id=state.run_id,
                generation=state.generation,
                phase=state.phase,
                trigger_message_id=state.trigger_message_id,
            ),
            initial_phase="finalizing",
        )
        await cleanup.release(session_status="idle")
    else:
        assert ran == [(session_id, user_id, 8)]
        assert state.phase == "idle"


@pytest.mark.asyncio
async def test_early_return_commit_failure_preserves_marker_for_retry(monkeypatch):
    suffix = uuid.uuid4().hex[:12]
    user_id = f"commit-user-{suffix}"
    project_id = f"commit-project-{suffix}"
    session_id = f"commit-session-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=f"commit-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Commit recovery",
            slug=f"commit-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            status="busy",
            created_at=now,
            updated_at=now,
        ))
        db.add(AgentDriverState(
            session_id=session_id,
            user_id=user_id,
            generation=1,
            run_id=f"dead-{suffix}",
            owner_id="dead-worker",
            phase="running",
            lease_expires_at=now - timedelta(seconds=1),
            started_at=now - timedelta(minutes=1),
            updated_at=now - timedelta(seconds=1),
        ))

    record = next(
        item
        for item in await recover_expired_driver_records()
        if item.session_id == session_id
    )
    original_get_db_session = recovery_module.get_db_session

    @asynccontextmanager
    async def failing_commit_session():
        async with original_get_db_session() as db:
            async def fail_commit():
                raise RuntimeError("simulated commit failure")

            monkeypatch.setattr(db, "commit", fail_commit)
            yield db

    monkeypatch.setattr(
        recovery_module,
        "get_db_session",
        failing_commit_session,
    )
    assert await repair_expired_sessions([record]) == []

    state = await get_driver_state(session_id)
    assert state is not None
    assert state.generation == 2
    assert state.phase == "finalizing"
    assert state.run_id is not None
    assert state.lease_expires_at is not None
    expiry = state.lease_expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    assert expiry <= datetime.now(timezone.utc)

    # The failed maintenance identity is immediately rediscoverable rather
    # than cleared to idle. A later healthy pass can claim and finish it.
    monkeypatch.setattr(
        recovery_module,
        "get_db_session",
        original_get_db_session,
    )
    retry_record = next(
        item
        for item in await recover_expired_driver_records()
        if item.session_id == session_id
    )
    retry_results = await repair_expired_sessions([retry_record])
    assert len(retry_results) == 1
    assert retry_results[0].skipped is False
    state = await get_driver_state(session_id)
    assert state is not None
    assert state.phase == "idle"
    assert state.generation == 3


@pytest.mark.asyncio
async def test_completed_repair_release_failure_expires_marker_atomically(
    monkeypatch,
):
    suffix = uuid.uuid4().hex[:12]
    user_id = f"settle-user-{suffix}"
    project_id = f"settle-project-{suffix}"
    session_id = f"settle-session-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=f"settle-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Settle recovery",
            slug=f"settle-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            status="busy",
            created_at=now,
            updated_at=now,
        ))
        db.add(AgentDriverState(
            session_id=session_id,
            user_id=user_id,
            generation=1,
            run_id=f"dead-{suffix}",
            owner_id="dead-worker",
            phase="running",
            lease_expires_at=now - timedelta(seconds=1),
            started_at=now - timedelta(minutes=1),
            updated_at=now - timedelta(seconds=1),
        ))

    record = next(
        item
        for item in await recover_expired_driver_records()
        if item.session_id == session_id
    )
    observed_before_release: list[tuple[str, str]] = []

    async def fail_release(self, *, session_status=None):
        async with get_db_session() as db:
            status = (
                await db.execute(
                    select(Session.status).where(Session.id == session_id)
                )
            ).scalar_one()
            phase = (
                await db.execute(
                    select(AgentDriverState.phase).where(
                        AgentDriverState.session_id == session_id
                    )
                )
            ).scalar_one()
        observed_before_release.append((status, phase))
        raise RuntimeError("repair release failpoint")

    monkeypatch.setattr(RunLease, "release", fail_release)
    results = await repair_expired_sessions([record])

    assert len(results) == 1
    assert observed_before_release == [("busy", "finalizing")]
    state = await get_driver_state(session_id)
    assert state is not None
    assert state.phase == "finalizing"
    expiry = state.lease_expires_at
    assert expiry is not None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    assert expiry <= datetime.now(timezone.utc)
    async with get_db_session() as db:
        status = (
            await db.execute(
                select(Session.status).where(Session.id == session_id)
            )
        ).scalar_one()
    assert status == "error"
