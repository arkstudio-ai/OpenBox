"""Durable Task descriptor, outbox, fencing, and parent rejoin tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update

from agent.driver import (
    get_driver_state,
    recover_expired_driver_records,
    reserve_recovered_run,
    reserve_run,
)
from agent.recovery import (
    reconcile_completed_task_handoffs,
    repair_interrupted_session,
    resume_unbound_task_children,
)
from agent.task_handoff import (
    TASK_CHILD_INTERRUPTED,
    TASK_CHILD_NO_TERMINAL_RESULT,
    TaskHandoffFenceError,
    bind_task_handoff_child,
    complete_task_handoff_from_transcript,
    create_task_handoff,
    recover_task_handoff_outboxes,
    unbound_task_handoffs,
)
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.task_handoff import TaskHandoff
from db.models.user import User
from session.agent_event_log import verify_agent_event_parity


@pytest.fixture(autouse=True)
async def _isolate_task_handoff_rows(ensure_test_db):
    # The shared in-memory test database intentionally survives between unit
    # tests. Global startup sweep functions must not consume descriptors left
    # by a different fault scenario in this module.
    async with get_db_session() as db:
        await db.execute(delete(TaskHandoff))


async def _seed_handoff():
    suffix = uuid.uuid4().hex[:12]
    user_id = f"handoff-user-{suffix}"
    project_id = f"handoff-project-{suffix}"
    parent_session_id = f"handoff-parent-{suffix}"
    parent_message_id = f"handoff-parent-message-{suffix}"
    parent_part_id = f"handoff-parent-part-{suffix}"
    child_session_id = f"handoff-child-{suffix}"
    child_trigger_id = f"handoff-trigger-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=f"handoff-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Task handoff",
            slug=f"handoff-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add_all([
            Session(
                id=parent_session_id,
                user_id=user_id,
                project_id=project_id,
                status="idle",
                created_at=now,
                updated_at=now,
            ),
            Session(
                id=child_session_id,
                user_id=user_id,
                project_id=project_id,
                parent_id=parent_session_id,
                status="idle",
                created_at=now,
                updated_at=now,
            ),
        ])
        db.add_all([
            Message(
                id=parent_message_id,
                session_id=parent_session_id,
                user_id=user_id,
                role="assistant",
                finish="tool_calls",
                created_at=now,
            ),
            Message(
                id=child_trigger_id,
                session_id=child_session_id,
                user_id=user_id,
                role="user",
                created_at=now,
            ),
        ])
        db.add_all([
            Part(
                id=f"handoff-step-{suffix}",
                message_id=parent_message_id,
                session_id=parent_session_id,
                user_id=user_id,
                type="step-start",
                data={
                    "type": "step-start",
                    "id": f"handoff-step-{suffix}",
                    "step": 1,
                    "session_id": parent_session_id,
                    "message_id": parent_message_id,
                },
                created_at=now,
            ),
            Part(
                id=parent_part_id,
                message_id=parent_message_id,
                session_id=parent_session_id,
                user_id=user_id,
                type="tool",
                canonical_tool_id="task",
                wire_tool_name="task",
                provider_binding_digest="c" * 64,
                provider_dialect="litellm",
                stream_seq=1,
                data={
                    "type": "tool",
                    "id": parent_part_id,
                    "tool": "task",
                    "status": "running",
                    "input": {"description": "durable child"},
                    "session_id": parent_session_id,
                    "message_id": parent_message_id,
                },
                created_at=now + timedelta(microseconds=1),
            ),
        ])

    parent_lease = await reserve_run(parent_session_id, user_id)
    await parent_lease.set_phase("running")
    handoff = await create_task_handoff(
        user_id=user_id,
        parent_session_id=parent_session_id,
        parent_message_id=parent_message_id,
        parent_part_id=parent_part_id,
        parent_run_id=parent_lease.run_id,
        parent_generation=parent_lease.generation,
        child_session_id=child_session_id,
        child_trigger_message_id=child_trigger_id,
        task_title="durable child",
        subagent_type="explore",
    )
    return {
        "user_id": user_id,
        "project_id": project_id,
        "parent_session_id": parent_session_id,
        "parent_message_id": parent_message_id,
        "parent_part_id": parent_part_id,
        "child_session_id": child_session_id,
        "child_trigger_id": child_trigger_id,
        "parent_lease": parent_lease,
        "handoff": handoff,
        "now": now,
    }


async def _add_child_answer(
    seed,
    text: str = "durable answer",
    *,
    finish: str = "stop",
    parent_id: str | None = None,
) -> None:
    message_id = f"answer-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Message(
            id=message_id,
            session_id=seed["child_session_id"],
            user_id=seed["user_id"],
            role="assistant",
            parent_id=parent_id or seed["child_trigger_id"],
            finish=finish,
            created_at=now,
        ))
        db.add(Part(
            id=f"text-{uuid.uuid4().hex[:12]}",
            message_id=message_id,
            session_id=seed["child_session_id"],
            user_id=seed["user_id"],
            type="text",
            data={
                "type": "text",
                "id": f"text-data-{uuid.uuid4().hex[:12]}",
                "text": text,
                "session_id": seed["child_session_id"],
                "message_id": message_id,
            },
            created_at=now,
        ))


@pytest.mark.asyncio
async def test_descriptor_and_parent_pointer_are_persisted_under_parent_fence():
    seed = await _seed_handoff()
    try:
        async with get_db_session() as db:
            row = (
                await db.execute(
                    select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
                )
            ).scalar_one()
            part = (
                await db.execute(
                    select(Part).where(Part.id == seed["parent_part_id"])
                )
            ).scalar_one()

        assert row.parent_run_id == seed["parent_lease"].run_id
        assert row.parent_generation == seed["parent_lease"].generation
        assert row.state == "accepted"
        assert part.data["metadata"] == {
            "child_session_id": seed["child_session_id"],
            "subagent_type": "explore",
            "task_handoff_id": seed["handoff"].id,
        }

        with pytest.raises(TaskHandoffFenceError, match="no longer live"):
            await create_task_handoff(
                user_id="another-user",
                parent_session_id=seed["parent_session_id"],
                parent_message_id=seed["parent_message_id"],
                parent_part_id=seed["parent_part_id"],
                parent_run_id=seed["parent_lease"].run_id,
                parent_generation=seed["parent_lease"].generation,
                child_session_id=seed["child_session_id"],
                child_trigger_message_id=seed["child_trigger_id"],
                task_title="cross tenant",
                subagent_type="explore",
            )
    finally:
        await seed["parent_lease"].release(session_status="error")


@pytest.mark.asyncio
async def test_completed_outbox_rejoins_exact_parent_part_and_is_idempotent():
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    await child_lease.set_phase("running")
    await _add_child_answer(seed)
    await child_lease.release(session_status="idle")

    raw = await complete_task_handoff_from_transcript(
        seed["handoff"].id,
        child_run_id=child_lease.run_id,
        child_generation=child_lease.generation,
    )
    assert "durable answer" in raw["output"]

    await seed["parent_lease"].release(session_status="error")
    first = await repair_interrupted_session(
        seed["parent_session_id"], seed["user_id"]
    )
    second = await repair_interrupted_session(
        seed["parent_session_id"], seed["user_id"]
    )
    assert first.rejoined_tasks == 1
    assert second.rejoined_tasks == 0
    parity = await verify_agent_event_parity(
        seed["parent_session_id"],
        user_id=seed["user_id"],
        require_closed=True,
    )
    assert parity.ok is True, parity.model_dump()

    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
        part = (
            await db.execute(
                select(Part).where(Part.id == seed["parent_part_id"])
            )
        ).scalar_one()
        message = (
            await db.execute(
                select(Message).where(Message.id == seed["parent_message_id"])
            )
        ).scalar_one()
        step_finishes = list(
            (
                await db.execute(
                    select(Part).where(
                        Part.message_id == seed["parent_message_id"],
                        Part.type == "step-finish",
                    )
                )
            ).scalars().all()
        )

    assert handoff.state == "rejoined"
    assert part.data["status"] == "completed"
    assert "durable answer" in part.data["output"]
    assert part.data["metadata"]["child_session_id"] == seed["child_session_id"]
    assert part.data["metadata"]["task_handoff_id"] == seed["handoff"].id
    assert part.data["metadata"]["task_outbox_completed"] is True
    assert message.finish == "aborted"
    assert len(step_finishes) == 1


@pytest.mark.asyncio
async def test_stale_child_generation_cannot_complete_after_takeover():
    seed = await _seed_handoff()
    child_one = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_one.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_one)
    await child_one.release(session_status="error")

    child_two = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_two.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(
        seed["handoff"].id,
        child_two,
        mode="takeover",
    )
    try:
        with pytest.raises(TaskHandoffFenceError, match="stale child result"):
            await complete_task_handoff_from_transcript(
                seed["handoff"].id,
                child_run_id=child_one.run_id,
                child_generation=child_one.generation,
            )
    finally:
        await child_two.release(session_status="error")
        await seed["parent_lease"].release(session_status="error")


@pytest.mark.asyncio
async def test_running_child_recovery_writes_unknown_outcome_without_replay():
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    await child_lease.set_phase("running")
    await child_lease.stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == seed["child_session_id"])
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    await seed["parent_lease"].release(session_status="error")

    records = await recover_expired_driver_records()
    child_record = next(
        record for record in records
        if record.session_id == seed["child_session_id"]
    )
    assert child_record.phase == "running"
    assert child_record.run_id == child_lease.run_id
    assert await recover_task_handoff_outboxes([child_record]) == 1

    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
    projected = handoff.result_payload["projected"]
    assert handoff.state == "completed"
    assert projected["metadata"]["error"] is True
    assert projected["metadata"]["recovery_code"] == TASK_CHILD_INTERRUPTED
    assert "was not replayed" in projected["output"]
    await child_lease.release(session_status="error")


@pytest.mark.asyncio
async def test_child_result_overwrites_parent_recovery_error_when_tail_is_unchanged():
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    await child_lease.set_phase("running")

    # The parent dies first. Its deterministic repair cannot know the healthy
    # child is still running, so it temporarily records outcome_unknown.
    await seed["parent_lease"].release(session_status="error")
    first = await repair_interrupted_session(
        seed["parent_session_id"], seed["user_id"]
    )
    assert first.repaired_tools == 1
    async with get_db_session() as db:
        repaired_part = (
            await db.execute(
                select(Part).where(Part.id == seed["parent_part_id"])
            )
        ).scalar_one()
    assert repaired_part.data["status"] == "error"
    assert repaired_part.data["metadata"]["recovery_code"] == "tool_outcome_unknown"
    assert repaired_part.data["metadata"].get("task_outbox_completed") is not True

    # The child then completes normally. The outbox completion marker proves
    # the real result, so a later maintenance generation replaces only this
    # exact recovery error without replaying either loop.
    await _add_child_answer(seed, "answer after parent repair")
    await child_lease.release(session_status="idle")
    await complete_task_handoff_from_transcript(
        seed["handoff"].id,
        child_run_id=child_lease.run_id,
        child_generation=child_lease.generation,
    )
    second = await repair_interrupted_session(
        seed["parent_session_id"], seed["user_id"]
    )
    assert second.rejoined_tasks == 1
    parity = await verify_agent_event_parity(
        seed["parent_session_id"],
        user_id=seed["user_id"],
        require_closed=True,
    )
    assert parity.ok is True, parity.model_dump()
    async with get_db_session() as db:
        delivered_part = (
            await db.execute(
                select(Part).where(Part.id == seed["parent_part_id"])
            )
        ).scalar_one()
    assert delivered_part.data["status"] == "completed"
    assert "answer after parent repair" in delivered_part.data["output"]
    assert delivered_part.data["metadata"]["task_outbox_completed"] is True


@pytest.mark.asyncio
async def test_completed_outbox_does_not_rewrite_history_after_parent_advances():
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    await _add_child_answer(seed, "late result")
    await child_lease.release(session_status="idle")
    await complete_task_handoff_from_transcript(
        seed["handoff"].id,
        child_run_id=child_lease.run_id,
        child_generation=child_lease.generation,
    )
    await seed["parent_lease"].release(session_status="error")

    later_message_id = f"later-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    async with get_db_session() as db:
        db.add(Message(
            id=later_message_id,
            session_id=seed["parent_session_id"],
            user_id=seed["user_id"],
            role="user",
            created_at=now,
        ))
    result = await repair_interrupted_session(
        seed["parent_session_id"], seed["user_id"]
    )
    assert result.rejoined_tasks == 0

    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
        part = (
            await db.execute(
                select(Part).where(Part.id == seed["parent_part_id"])
            )
        ).scalar_one()
    assert handoff.state == "completed"
    assert part.data["status"] == "error"
    assert part.data["metadata"]["recovery_code"] == "tool_outcome_unknown"


@pytest.mark.asyncio
async def test_outbox_stores_only_bounded_projection_for_large_child_output(
    monkeypatch,
    tmp_path,
):
    import tool.truncation as truncation

    monkeypatch.setattr(truncation, "_data_dir", str(tmp_path))
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    large_text = "x" * (80 * 1024)
    await _add_child_answer(seed, large_text)
    await child_lease.release(session_status="idle")
    await complete_task_handoff_from_transcript(
        seed["handoff"].id,
        child_run_id=child_lease.run_id,
        child_generation=child_lease.generation,
    )

    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
    assert set(handoff.result_payload) == {"projected"}
    projected = handoff.result_payload["projected"]
    assert projected["metadata"]["truncated"] is True
    assert len(projected["output"].encode("utf-8")) < len(large_text.encode("utf-8"))
    assert len(projected["output"].encode("utf-8")) < 55 * 1024
    await seed["parent_lease"].release(session_status="error")


@pytest.mark.asyncio
async def test_normal_child_bind_fails_after_parent_generation_expires():
    seed = await _seed_handoff()
    await seed["parent_lease"].stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == seed["parent_session_id"])
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    try:
        with pytest.raises(TaskHandoffFenceError, match="lost before child bind"):
            await bind_task_handoff_child(seed["handoff"].id, child_lease)
    finally:
        await child_lease.release(session_status="error")
        await seed["parent_lease"].release(session_status="error")


@pytest.mark.asyncio
async def test_unbound_recovery_waits_for_parent_expiry_and_rechecks_after_reserve(
    monkeypatch,
):
    import agent.recovery as recovery_module

    seed = await _seed_handoff()
    assert await resume_unbound_task_children() == []
    assert seed["handoff"].id not in {
        item.id for item in await unbound_task_handoffs()
    }

    await seed["parent_lease"].stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == seed["parent_session_id"])
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    assert seed["handoff"].id in {
        item.id for item in await unbound_task_handoffs()
    }

    started: list[str] = []

    async def fake_recovered_prompt(lease, _assets):
        started.append(lease.session_id)
        await lease.release(session_status="error")

    monkeypatch.setattr(
        recovery_module,
        "_run_recovered_prompt",
        fake_recovered_prompt,
    )
    resumed = await resume_unbound_task_children()
    tasks = list(recovery_module._resume_tasks)
    if tasks:
        import asyncio

        await asyncio.gather(*tasks)
    assert resumed == [seed["child_session_id"]]
    assert started == [seed["child_session_id"]]

    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
    assert handoff.child_generation is not None
    assert handoff.child_run_id is not None
    await seed["parent_lease"].release(session_status="error")


@pytest.mark.asyncio
async def test_unbound_bind_rejects_parent_generation_created_after_candidate_scan():
    seed = await _seed_handoff()
    await seed["parent_lease"].stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == seed["parent_session_id"])
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    assert seed["handoff"].id in {
        item.id for item in await unbound_task_handoffs()
    }

    recovered_parent = next(
        item
        for item in await recover_expired_driver_records()
        if item.session_id == seed["parent_session_id"]
    )
    replacement_parent = await reserve_recovered_run(
        recovered_parent,
        initial_phase="reserved",
    )
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    try:
        with pytest.raises(TaskHandoffFenceError, match="lost its parent/tail fence"):
            await bind_task_handoff_child(
                seed["handoff"].id,
                child_lease,
                mode="unbound_recovery",
            )
    finally:
        await child_lease.release(session_status="error")
        await replacement_parent.release(session_status="idle")
        await seed["parent_lease"].release(session_status="error")


@pytest.mark.asyncio
async def test_partial_child_text_is_an_error_for_live_completion():
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    await _add_child_answer(seed, "partial text must not escape", finish="aborted")
    await child_lease.release(session_status="error")

    raw = await complete_task_handoff_from_transcript(
        seed["handoff"].id,
        child_run_id=child_lease.run_id,
        child_generation=child_lease.generation,
    )
    assert raw["metadata"]["error"] is True
    assert raw["metadata"]["recovery_code"] == TASK_CHILD_NO_TERMINAL_RESULT
    assert "partial text must not escape" not in raw["output"]
    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
    projected = handoff.result_payload["projected"]
    assert projected["metadata"]["error"] is True
    assert (
        projected["metadata"]["recovery_code"]
        == TASK_CHILD_NO_TERMINAL_RESULT
    )
    assert "partial text must not escape" not in projected["output"]
    await seed["parent_lease"].release(session_status="error")


@pytest.mark.asyncio
async def test_child_error_status_rejects_even_a_terminal_stop_reply():
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    await _add_child_answer(seed, "stop text from a failed child", finish="stop")
    await child_lease.release(session_status="error")

    raw = await complete_task_handoff_from_transcript(
        seed["handoff"].id,
        child_run_id=child_lease.run_id,
        child_generation=child_lease.generation,
    )
    assert raw["metadata"]["error"] is True
    assert raw["metadata"]["recovery_code"] == TASK_CHILD_NO_TERMINAL_RESULT
    assert "stop text from a failed child" not in raw["output"]
    await seed["parent_lease"].release(session_status="error")


@pytest.mark.asyncio
async def test_idle_child_without_terminal_reply_converges_to_honest_outbox():
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    await _add_child_answer(seed, "startup partial", finish="aborted")
    await child_lease.release(session_status="error")

    assert await recover_task_handoff_outboxes([]) == 1
    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
    assert handoff.state == "completed"
    projected = handoff.result_payload["projected"]
    assert projected["metadata"]["error"] is True
    assert (
        projected["metadata"]["recovery_code"]
        == TASK_CHILD_NO_TERMINAL_RESULT
    )
    assert "startup partial" not in projected["output"]
    await seed["parent_lease"].release(session_status="error")


@pytest.mark.asyncio
async def test_recovered_prompt_failure_before_loop_finally_preserves_expired_marker(
    monkeypatch,
):
    import agent.loop as loop_module
    import agent.recovery as recovery_module

    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])

    async def fail_before_finally(*_args, **_kwargs):
        raise RuntimeError("before loop finally")

    monkeypatch.setattr(loop_module, "run_loop", fail_before_finally)
    await recovery_module._run_recovered_prompt(child_lease, [])
    state = await get_driver_state(seed["child_session_id"])
    assert state is not None
    assert state.phase == "reserved"
    assert state.run_id == child_lease.run_id
    assert state.generation == child_lease.generation
    recovered = [
        item
        for item in await recover_expired_driver_records()
        if item.session_id == seed["child_session_id"]
    ]
    assert len(recovered) == 1
    assert recovered[0].run_id == child_lease.run_id
    assert recovered[0].generation == child_lease.generation
    assert recovered[0].phase == "reserved"
    retry_lease = await reserve_recovered_run(
        recovered[0],
        initial_phase="reserved",
    )
    assert retry_lease.generation == child_lease.generation + 1
    assert await retry_lease.release(session_status="error") is True
    await seed["parent_lease"].release(session_status="error")


@pytest.mark.asyncio
async def test_normally_delivered_parent_part_converges_handoff_to_rejoined():
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    await _add_child_answer(seed, "normally committed")
    await child_lease.release(session_status="idle")
    await complete_task_handoff_from_transcript(
        seed["handoff"].id,
        child_run_id=child_lease.run_id,
        child_generation=child_lease.generation,
    )

    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
        part = (
            await db.execute(
                select(Part).where(Part.id == seed["parent_part_id"])
            )
        ).scalar_one()
        projected = handoff.result_payload["projected"]
        data = dict(part.data)
        data.update({
            "status": "completed",
            "output": projected["output"],
            "title": projected["title"],
            "error": None,
            "metadata": {
                key: value
                for key, value in projected["metadata"].items()
                if key != "error"
            },
        })
        part.data = data
        parent_message = (
            await db.execute(
                select(Message).where(Message.id == seed["parent_message_id"])
            )
        ).scalar_one()
        parent_message.finish = "stop"
        balanced_finish_id = f"balanced-finish-{uuid.uuid4().hex[:12]}"
        db.add(Part(
            id=balanced_finish_id,
            message_id=seed["parent_message_id"],
            session_id=seed["parent_session_id"],
            user_id=seed["user_id"],
            type="step-finish",
            data={
                "type": "step-finish",
                "id": balanced_finish_id,
                "step": 1,
                "session_id": seed["parent_session_id"],
                "message_id": seed["parent_message_id"],
            },
            created_at=datetime.now(timezone.utc),
        ))
    await seed["parent_lease"].release(session_status="idle")

    assert await reconcile_completed_task_handoffs() == [
        seed["parent_session_id"]
    ]
    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
        session = (
            await db.execute(
                select(Session).where(Session.id == seed["parent_session_id"])
            )
        ).scalar_one()
    assert handoff.state == "rejoined"
    assert session.status == "idle"


@pytest.mark.asyncio
async def test_delivered_task_with_open_parent_tail_is_closed_without_result_loss():
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    await _add_child_answer(seed, "committed before parent crash")
    await child_lease.release(session_status="idle")
    await complete_task_handoff_from_transcript(
        seed["handoff"].id,
        child_run_id=child_lease.run_id,
        child_generation=child_lease.generation,
    )

    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
        part = (
            await db.execute(
                select(Part).where(Part.id == seed["parent_part_id"])
            )
        ).scalar_one()
        projected = handoff.result_payload["projected"]
        data = dict(part.data)
        data.update({
            "status": "completed",
            "output": projected["output"],
            "title": projected["title"],
            "error": None,
            "metadata": {
                key: value
                for key, value in projected["metadata"].items()
                if key != "error"
            },
        })
        part.data = data
    await seed["parent_lease"].release(session_status="idle")

    assert await reconcile_completed_task_handoffs() == [
        seed["parent_session_id"]
    ]
    async with get_db_session() as db:
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
        session = (
            await db.execute(
                select(Session).where(Session.id == seed["parent_session_id"])
            )
        ).scalar_one()
        message = (
            await db.execute(
                select(Message).where(Message.id == seed["parent_message_id"])
            )
        ).scalar_one()
        part = (
            await db.execute(
                select(Part).where(Part.id == seed["parent_part_id"])
            )
        ).scalar_one()
        finishes = list(
            (
                await db.execute(
                    select(Part).where(
                        Part.message_id == seed["parent_message_id"],
                        Part.type == "step-finish",
                    )
                )
            ).scalars().all()
        )
    assert handoff.state == "rejoined"
    assert session.status == "error"
    assert message.finish == "aborted"
    assert part.data["status"] == "completed"
    assert "committed before parent crash" in part.data["output"]
    assert len(finishes) == 1


@pytest.mark.asyncio
async def test_rejoin_only_is_complete_noop_when_parent_transcript_advanced():
    seed = await _seed_handoff()
    child_lease = await reserve_run(seed["child_session_id"], seed["user_id"])
    await child_lease.bind_trigger_message(seed["child_trigger_id"])
    await bind_task_handoff_child(seed["handoff"].id, child_lease)
    await _add_child_answer(seed, "late child result")
    await child_lease.release(session_status="idle")
    await complete_task_handoff_from_transcript(
        seed["handoff"].id,
        child_run_id=child_lease.run_id,
        child_generation=child_lease.generation,
    )
    await seed["parent_lease"].release(session_status="idle")

    later_message_id = f"later-assistant-{uuid.uuid4().hex[:12]}"
    later_part_id = f"later-part-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    later_data = {
        "type": "tool",
        "id": later_part_id,
        "tool": "bash",
        "status": "running",
        "session_id": seed["parent_session_id"],
        "message_id": later_message_id,
    }
    async with get_db_session() as db:
        db.add(Message(
            id=later_message_id,
            session_id=seed["parent_session_id"],
            user_id=seed["user_id"],
            role="assistant",
            finish="tool_calls",
            created_at=now,
        ))
        db.add(Part(
            id=later_part_id,
            message_id=later_message_id,
            session_id=seed["parent_session_id"],
            user_id=seed["user_id"],
            type="tool",
            data=later_data,
            created_at=now,
        ))

    assert await reconcile_completed_task_handoffs() == []
    async with get_db_session() as db:
        session = (
            await db.execute(
                select(Session).where(Session.id == seed["parent_session_id"])
            )
        ).scalar_one()
        old_part = (
            await db.execute(
                select(Part).where(Part.id == seed["parent_part_id"])
            )
        ).scalar_one()
        later_part = (
            await db.execute(select(Part).where(Part.id == later_part_id))
        ).scalar_one()
        handoff = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == seed["handoff"].id)
            )
        ).scalar_one()
    assert session.status == "idle"
    assert old_part.data["status"] == "running"
    assert later_part.data == later_data
    assert handoff.state == "completed"
