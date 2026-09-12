"""Recorded chat commits, old-session cutover and asynchronous ownership."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from db.models.trajectory import SessionTrajectory, TrajectoryEvent
from trajectory import TraceContext, bind


async def make_session(*, owner=None, parent=None):
    identifier = uuid4().hex
    owner = owner or "owner-" + uuid4().hex
    stamp = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Session(id=identifier, user_id=owner, workspace_id="workspace", project_id="project",
            title="Session", agent="build", model="test/model", created_at=stamp, updated_at=stamp,
            parent_id=parent))
    return identifier, owner


async def events_for(session_id):
    async with get_db_session() as db:
        return (await db.scalars(select(TrajectoryEvent).where(
            TrajectoryEvent.session_id == session_id).order_by(TrajectoryEvent.seq))).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_context", [None, {}], ids=["null", "empty-object"])
@pytest.mark.parametrize("change_model", [False, True], ids=["send", "change-model-and-send"])
async def test_existing_session_records_only_new_input_and_captures_actual_baseline(
        monkeypatch, legacy_context, change_model):
    from db.models.question import SessionExecution
    from session.session import create_user_message, update_session
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    session, owner = await make_session()
    old = await create_user_message(session, "before rollout", user_id=owner)
    async with get_db_session() as db:
        execution = await db.get(SessionExecution, session)
        execution.trace_context = legacy_context
    assert await events_for(session) == []

    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    if change_model:
        await update_session(session, user_id=owner, model="test/updated")
    new = await create_user_message(session, "after rollout", user_id=owner)
    events = await events_for(session)
    accepted = [event for event in events if event.type == "input.accepted"]
    assert len(accepted) == 1
    assert accepted[0].data["text"] == "after rollout"
    assert accepted[0].context["turn_id"] == new.id
    baseline = next(event for event in events if event.type == "baseline.captured")
    assert baseline.data["legacy_session"] is True
    assert [message["id"] for message in baseline.data["history"]] == [old.id]
    assert baseline.data["history"][0]["parts"][0]["text"] == "before rollout"
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    async with get_db_session() as db:
        context = TraceContext.from_dict((await db.get(SessionExecution, session)).trace_context)
        assert (context.user_id, context.session_id, context.source_session_id) == (owner, session, session)
        assert context.turn_id == new.id


@pytest.mark.asyncio
async def test_input_and_compatibility_projection_rollback_with_journal(monkeypatch):
    import trajectory
    from session.session import create_user_message
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    session, owner = await make_session()
    original = trajectory.record

    async def record_then_fail(kind, data, **kwargs):
        result = await original(kind, data, **kwargs)
        if kind == "part.committed":
            raise trajectory.RecordingError("simulated journal failure")
        return result
    monkeypatch.setattr(trajectory, "record", record_then_fail)
    with pytest.raises(trajectory.RecordingError):
        await create_user_message(session, "must rollback", user_id=owner)
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(Message).where(Message.session_id == session)) == 0
        assert await db.scalar(select(func.count()).select_from(Part).where(Part.session_id == session)) == 0
        assert await db.scalar(select(func.count()).select_from(SessionTrajectory).where(SessionTrajectory.session_id == session)) == 0
    assert await events_for(session) == []


@pytest.mark.asyncio
async def test_child_prompt_and_messages_join_parent_trajectory_before_child_runs(monkeypatch):
    from session.session import create_user_message
    from db.models.question import SessionExecution
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    parent, owner = await make_session()
    first = await create_user_message(parent, "delegate", user_id=owner)
    child, _ = await make_session(owner=owner, parent=parent)
    context = TraceContext(user_id=owner, session_id=parent, source_session_id=child,
        workspace_id="workspace", turn_id=first.id, agent_id="child-agent", parent_agent_id="parent-agent",
        parent_call_id="delegation")
    with bind(context):
        prompt = await create_user_message(child, "child task", synthetic=True, user_id=owner)
    async with get_db_session() as db:
        assert await db.scalar(select(SessionTrajectory.id).where(SessionTrajectory.session_id == child)) is None
        saved = (await db.get(SessionExecution, child)).trace_context
        assert saved["session_id"] == parent
        assert saved["source_session_id"] == child
    injected = next(event for event in await events_for(parent) if event.type == "input.injected")
    assert injected.context["message_id"] == prompt.id
    assert injected.context["turn_id"] == first.id
    assert injected.source_session_id == child


@pytest.mark.asyncio
async def test_superseded_generation_cannot_commit_a_late_chat_part(monkeypatch):
    from session.session import create_user_message, create_assistant_message, save_part
    from question import runtime
    from models.message import TextPart
    from trajectory import TrajectoryError
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    session, owner = await make_session()
    first = await create_user_message(session, "first", user_id=owner)
    ticket = await runtime.start_run(session, owner)
    trace = await runtime.get_run_trace(ticket)
    with bind(trace):
        assistant = await create_assistant_message(session, first.id, user_id=owner)
    await create_user_message(session, "new independent input", user_id=owner)
    part = TextPart(text="late overwrite", session_id=session, message_id=assistant.id)
    before = len(await events_for(session))
    with bind(trace), pytest.raises(TrajectoryError):
        await save_part(part, is_new=True, user_id=owner)
    async with get_db_session() as db:
        assert await db.get(Part, part.id) is None
    assert len(await events_for(session)) == before


@pytest.mark.asyncio
async def test_delayed_cron_injection_restores_original_turn_and_is_idempotent(monkeypatch):
    from cron.injector import try_inject_result
    from db.models.cron import CronRun
    from trajectory import current
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    session, owner = await make_session()
    run_id = "cron-run-" + uuid4().hex
    original = TraceContext(user_id=owner, session_id=session, workspace_id="workspace", turn_id=run_id)
    async with get_db_session() as db:
        db.add(CronRun(id=run_id, job_id="job", user_id=owner, session_id=session,
            trace_context=original.to_dict(), status="ok", task_prompt="scheduled task",
            summary_text="scheduled result", started_at=datetime.now(timezone.utc)))
    ambient = original.derive(turn_id="unrelated-new-turn", run_id="unrelated-run", generation=999)
    job = {"id": "job", "session_id": session, "user_id": owner, "name": "Monitor", "task_prompt": "scheduled task"}
    with bind(ambient):
        assert await try_inject_result(run_id, job, "scheduled result")
        assert await try_inject_result(run_id, job, "scheduled result")
        assert current() == ambient
    events = await events_for(session)
    assert {event.context.get("turn_id") for event in events} == {run_id}
    assert len([event for event in events if event.type == "input.injected"]) == 1
    assert len([event for event in events if event.type == "job.progress"]) == 1
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(Message).where(Message.session_id == session)) == 2


@pytest.mark.asyncio
async def test_auxiliary_request_chunks_can_follow_finished_run_with_original_identity(monkeypatch):
    from session.session import create_user_message
    from question import runtime
    from agent.trajectory import RequestCapture
    from tool.tool import ToolContext
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    monkeypatch.setenv("TRAJECTORY_BATCH_MS", "1")
    session, owner = await make_session()
    first = await create_user_message(session, "request", user_id=owner)
    ticket = await runtime.start_run(session, owner)
    trace = await runtime.get_run_trace(ticket)
    await runtime.finish_run(ticket, completed=True)
    capture = await RequestCapture.start(ToolContext(session_id=session, user_id=owner, trace_context=trace),
        purpose="suggestions", model_id="provider/model", payload={"model": "provider/model"}, capture_level="adapter_input")

    async def chunks():
        for text in ("a", "b"):
            yield {"type": "content", "delta": text}
    async for _ in capture.stream_chunks(chunks(), lambda item: [{"type": "text", "delta": item["delta"]}]):
        pass
    await capture.finish("completed")
    requests = [event for event in await events_for(session) if event.type.startswith("request.")]
    assert len({event.request_id for event in requests}) == 1
    assert {event.context["turn_id"] for event in requests} == {first.id}
    assert {event.context["run_id"] for event in requests} == {ticket.run_id}
    assert [event.data["chunk_index"] for event in requests if event.type == "request.delta"] == [1, 2]


@pytest.mark.asyncio
async def test_cron_pipeline_uses_saved_target_for_summary_child_run_and_completion(monkeypatch):
    from cron import executor
    from trajectory import current
    from db.models.cron import CronRun
    import cron.runlog
    import cron.injector
    import sandbox
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    session, owner = await make_session()
    child, _ = await make_session(owner=owner, parent=session)
    job_id = "job-" + uuid4().hex
    observed = []

    async def summary(job):
        observed.append(("summary", current()))
        return "prior history"

    async def temp(job, locale):
        return child

    async def execute(temp_id, user_id, job, locale):
        observed.append(("agent", current()))
        return "result"

    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(executor, "_get_session_summary", summary)
    monkeypatch.setattr(executor, "_create_temp_session", temp)
    monkeypatch.setattr(executor, "_run_agent_loop", execute)
    monkeypatch.setattr(cron.runlog, "append_run_log", noop)
    monkeypatch.setattr(cron.injector, "try_inject_result", noop)
    monkeypatch.setattr(executor, "_dispatch_delivery", noop)
    monkeypatch.setattr(sandbox.sandbox_manager, "release", noop)
    monkeypatch.setattr(sandbox.provider, "routes_per_user", False)
    foreign = TraceContext(user_id="someone-else", session_id="unrelated", turn_id="other-turn")
    with bind(foreign):
        result = await executor.execute_cron_job({"id": job_id, "user_id": owner, "session_id": session,
            "workspace_id": "workspace", "project_id": "project", "name": "Monitor", "task_prompt": "Check"})
        assert current() == foreign
    assert result["status"] == "ok"
    assert observed[0][1].source_session_id == session
    assert observed[1][1].source_session_id == child
    assert {trace.session_id for _, trace in observed} == {session}
    assert {trace.user_id for _, trace in observed} == {owner}
    assert {trace.turn_id for _, trace in observed} == {result["run_id"]}
    async with get_db_session() as db:
        row = await db.get(CronRun, result["run_id"])
        assert row.trace_context["session_id"] == session
        assert row.temp_session_id == child
        assert row.status == "ok"
    events = await events_for(session)
    assert any(event.type == "input.injected" and event.source_session_id == child for event in events)
    assert any(event.type == "job.finished" and event.data["status"] == "completed" for event in events)
