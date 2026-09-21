"""Finish and coordinator recovery report outcomes without losing work or cost."""
from decimal import Decimal

import pytest

from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.billing import UsageEvent
from db.models.session import Session
from team import commands, scheduler
from team.errors import TeamError
from team.journal import command, snapshot, utcnow
from team.service import finish
from tests.unit.test_team_commands import make_task, setup_team
from tests.unit.test_team_runtime import config


async def test_explicit_failed_finish_preserves_blocked_goal_and_failure_reason(config):
    run, actor, server, root, members = await setup_team()
    task = await make_task(run, server, members[0], deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    async def block(writer):
        task_now = writer.state["tasks"][task["id"]]
        return await commands.update_task(writer, task["id"], task_now["revision"], "block", summary="Required source is unavailable")
    await command(run, server, "block", {}, block)
    with pytest.raises(TeamError, match="requires a concrete reason"):
        await command(run, server, "bad-failure", {}, lambda writer: finish(writer, "Unable to deliver", [], status="failed"))
    await command(run, server, "failure", {}, lambda writer: finish(writer, "The required source remains unavailable; no result was fabricated.",
        [], status="failed", reason="User declined a substitute source"))
    await scheduler.tick(run, actor)
    state = await snapshot(run, actor)
    assert state["run"]["state"] == "failed"
    assert state["run"]["failure_reason"] == "User declined a substitute source"
    assert state["tasks"][task["id"]]["state"] == "blocked"
    assert state == await snapshot(run, actor, rebuild=True)


async def test_accepted_work_finishes_without_forgiving_unknown_account_usage(config):
    run, actor, server, root, members = await setup_team()
    task = await make_task(run, server, members[0], deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    async def update(writer, action):
        task_now = writer.state["tasks"][task["id"]]
        return await commands.update_task(writer, task["id"], task_now["revision"], action, summary="Verified result")
    await command(run, server, "submit", {}, lambda writer: update(writer, "submit"))
    await command(run, server, "accept", {}, lambda writer: update(writer, "accept"))
    usage_id = f"usage-{run}"
    async with get_db_session() as db:
        db.add(UsageEvent(id=usage_id, idempotency_key=usage_id, user_id=actor.owner_user_id, workspace_id=actor.workspace_id,
            session_id=root, session_title="Completion test", model_id="openai/test", kind="chat", tokens={},
            total_tokens=0, credits=None, status="unreported", pricing={}, created_at=utcnow()))
    await command(run, server, "finish", {}, lambda writer: finish(writer, "Verified result", []))
    await scheduler.tick(run, actor)
    state = await snapshot(run, actor)
    assert state["run"]["state"] == "completed"
    await scheduler.tick(run, actor)
    assert await snapshot(run, actor) == state
    async with get_db_session() as db:
        meter = await db.get(UsageEvent, usage_id)
        assert meter.credits is None and meter.status == "unreported"



async def test_recovered_coordinator_failures_are_counted_once_and_pause_after_three(config):
    run, actor, server, root, _ = await setup_team()
    for generation in range(1, 4):
        async with get_db_session() as db:
            driver = await db.get(AgentDriverState, root)
            if driver is None:
                driver = AgentDriverState(session_id=root, user_id=actor.owner_user_id, generation=generation,
                    phase="idle", updated_at=utcnow())
                db.add(driver)
            driver.generation = generation
            (await db.get(Session, root)).status = "error"
        await scheduler.tick(run, actor)
        state = await snapshot(run, actor)
        assert state["members"][root]["consecutive_failures"] == generation
        await scheduler.tick(run, actor)
        assert (await snapshot(run, actor))["members"][root]["consecutive_failures"] == generation
    assert state["run"]["state"] == "paused"
    assert state["run"]["pause_reason"] == "coordinator_errors"
    assert state == await snapshot(run, actor, rebuild=True)


@pytest.mark.parametrize("question_status", ["pending", "answered", "rejected"])
async def test_cancel_closes_suspended_questions_and_never_cancels_later_conversation(config, monkeypatch, question_status):
    from db.models.question import QuestionCheckpoint, SessionExecution
    from question import question
    from team.service import cancel_waiting_questions, control
    monkeypatch.setattr(scheduler, "schedule", lambda *_: None)
    run, actor, _, root, _ = await setup_team()
    question_id = "cancel-question-" + run
    async with get_db_session() as db:
        (await db.get(Session, root)).status = "waiting_input"
        db.add(SessionExecution(session_id=root, user_id=actor.owner_user_id, generation=1,
            resume_pending=question_status != "pending", updated_at=utcnow()))
        db.add(QuestionCheckpoint(id=question_id, session_id=root, user_id=actor.owner_user_id,
            generation=1, status=question_status, questions=[], draft=[], answers=[],
            continuation={"kind": "team_lineup", "mode": "amend", "run_id": run},
            applied=False, created_at=utcnow(), updated_at=utcnow()))
    before = await snapshot(run, actor)
    await control(run, actor, "cancel", "cancel", before["run"]["revision"])
    await scheduler.tick(run, actor)
    assert (await snapshot(run, actor))["run"]["state"] == "canceled"
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, question_id)
        assert row.status == "cancelled" and not row.applied
        execution = await db.get(SessionExecution, root)
        assert execution.generation == 2 and not execution.resume_pending
        assert (await db.get(Session, root)).status == "idle"
        db.add(QuestionCheckpoint(id=question_id + "-later", session_id=root, user_id=actor.owner_user_id,
            generation=2, status="pending", questions=[], draft=[], continuation={"kind": "question"},
            applied=False, created_at=utcnow(), updated_at=utcnow()))
    assert [q.id for q in await question.list_pending(actor.owner_user_id)] == [question_id + "-later"]
    await cancel_waiting_questions(run, actor)
    async with get_db_session() as db:
        assert (await db.get(QuestionCheckpoint, question_id + "-later")).status == "pending"
        assert (await db.get(SessionExecution, root)).generation == 2


async def test_crash_after_finish_receipt_recovers_one_canonical_final_part(config):
    from db.models.message import Message
    from db.models.part import Part
    from team.completion import recover_final_response
    from tool.tool import final_response_part_id
    from session.session import get_messages
    run, actor, server, root, _ = await setup_team()
    message_id, tool_id = "message-" + run, "part-" + run
    async with get_db_session() as db:
        db.add(Message(id=message_id, session_id=root, user_id=actor.owner_user_id,
            role="assistant", finish="aborted", created_at=utcnow()))
        await db.flush()
        db.add(Part(id=tool_id, message_id=message_id, session_id=root, user_id=actor.owner_user_id,
            type="tool", data={"id": tool_id, "type": "tool", "tool": "team_finish", "status": "running",
                "session_id": root, "message_id": message_id}, created_at=utcnow()))
    await command(run, server, "finish-crash", {}, lambda writer: finish(writer,
        "Source unavailable; the requested result remains incomplete.", [], status="failed",
        reason="User declined an alternative", response_message_id=message_id, response_tool_part_id=tool_id))
    await recover_final_response(run, server)
    before = await get_messages(root, user_id=actor.owner_user_id)
    await recover_final_response(run, server)
    assert await get_messages(root, user_id=actor.owner_user_id) == before
    finals = [p for message in before for p in message.parts if p.type == "text" and p.channel == "final"]
    assert len(finals) == 1 and finals[0].id == final_response_part_id(tool_id)
    assert finals[0].text == "Source unavailable; the requested result remains incomplete."
    await scheduler.tick(run, actor)
    assert (await snapshot(run, actor))["run"]["state"] == "failed"
