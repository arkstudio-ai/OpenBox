"""Session producers on the spool: recording markers, deletion, fork, revert and todo writes.

Facts are read back from the spool files the emitter wrote. No business write
may touch a trajectory table or wait on the recorder.
"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session as SyncSession

import db.base as database
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.part import Part
from db.models.question import SessionExecution
from db.models.session import Session
from models.message import FilePart
from question import runtime
from session.session import (create_assistant_message, create_user_message, delete_failed_turn,
                             delete_messages_from, delete_session, record_projection_in_tx, save_part,
                             update_message_info)
from tests.unit.test_durable_questions import read, state  # noqa: F401
from tests.unit.test_run_fencing_api import add_session
from tests.unit.trajectory_producer_support import business_statements, recording_spool  # noqa: F401
from trajectory import TraceContext, bind
from trajectory.artifacts import artifact_event_id
from trajectory.producers import identity


async def _asset(asset_id: str, *, session_id: str = "s1", mime: str = "image/png", size: int = 7) -> FileAsset:
    async with database.get_db_session() as db:
        asset = FileAsset(id=asset_id, user_id="u1", workspace_id="w1", session_id=session_id,
                          name=f"{asset_id}.png", oss_key=f"assets/u1/{asset_id}/{asset_id}.png", mime=mime,
                          size=size, status="ready", created_at=runtime.now())
        db.add(asset)
    return asset


def _no_trajectory_sql(statements) -> bool:
    return not [statement for statement in statements if "trajectory_" in statement.lower()]


async def test_first_recorded_input_opens_period_zero_from_the_prior_history(state, recording_spool,
                                                                           business_statements):
    await _asset("asset_prior", mime="application/pdf", size=42)
    async with database.get_db_session() as db:
        db.add(Message(id="m-prior", session_id="s1", user_id="u1", role="user", created_at=runtime.now()))
        await db.flush()
        db.add(Part(id="p-prior", session_id="s1", message_id="m-prior", user_id="u1", type="file",
                    data={"id": "p-prior", "type": "file", "asset_id": "asset_prior", "path": "brief.pdf"},
                    created_at=runtime.now()))

    prompt = await create_user_message("s1", "Summarize the brief", user_id="u1")

    events = recording_spool.events()
    assert [item["type"] for item in events] == ["artifact.recorded", "baseline.captured", "turn.started",
                                                  "input.accepted", "message.committed", "part.committed"]
    artifact, baseline = events[0], events[1]
    assert artifact["data"]["role"] == "baseline_input"
    assert artifact["data"]["asset_ref"] == {"asset_id": "asset_prior", "oss_key": "assets/u1/asset_prior/asset_prior.png",
                                             "media_type": "application/pdf", "size_bytes": 42,
                                             "name": "asset_prior.png"}
    assert artifact["event_id"] == artifact_event_id("s1", "asset_prior")
    assert baseline["event_id"] == "evt_baseline_s1_0"
    assert [message["id"] for message in baseline["data"]["history"]] == ["m-prior"]
    assert baseline["data"]["artifacts"]["asset_prior"]["availability"] == "available"
    saved = (await read(SessionExecution, "s1")).trace_context
    assert saved["recording_epoch"] == 0 and saved["turn_id"] == prompt.id

    await create_user_message("s1", "And the appendix?", user_id="u1")
    assert len(recording_spool.events("baseline.captured")) == 1
    assert recording_spool.controls() == []
    assert _no_trajectory_sql(business_statements)


async def test_pause_is_reported_once_and_resuming_opens_a_new_period(state, recording_spool, monkeypatch):
    await create_user_message("s1", "First", user_id="u1")
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    await create_user_message("s1", "Unrecorded one", user_id="u1")
    await create_user_message("s1", "Unrecorded two", user_id="u1")
    saved = (await read(SessionExecution, "s1")).trace_context
    epoch = saved["recording_epoch"]
    assert saved["recording_paused"] is True and epoch > 0
    [paused] = recording_spool.controls("recording.state")
    assert (paused["state"], paused["user_id"], paused["session_id"], paused["reason"]) == (
        "paused", "u1", "s1", "recording_disabled")

    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    await create_user_message("s1", "Recorded again", user_id="u1")

    assert [control["state"] for control in recording_spool.controls("recording.state")] == ["paused", "resumed"]
    baselines = recording_spool.events("baseline.captured")
    assert [item["event_id"] for item in baselines] == ["evt_baseline_s1_0", f"evt_baseline_s1_{epoch}"]
    # The new period starts from everything said while recording was off.
    assert len(baselines[1]["data"]["history"]) == 3
    saved = (await read(SessionExecution, "s1")).trace_context
    assert "recording_paused" not in saved and saved["recording_epoch"] == epoch
    # Unrecorded input produced no events of its own.
    assert [item["data"]["text"] for item in recording_spool.events("input.accepted")] == ["First", "Recorded again"]


async def test_a_run_start_resumes_a_paused_session_once_and_epochs_never_repeat(state, recording_spool,
                                                                               monkeypatch):
    await create_user_message("s1", "First", user_id="u1")
    epochs = []
    for cycle in range(2):
        monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
        await create_user_message("s1", f"Off {cycle}", user_id="u1")
        epochs.append((await read(SessionExecution, "s1")).trace_context["recording_epoch"])
        monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
        # A run can start before any session write (a resumed question): its
        # start rewrites the saved identity and drops the markers.
        ticket = await runtime.start_run("s1", "u1")
        assert ticket is not None
        await runtime.finish_run(ticket, completed=True)
        await create_user_message("s1", f"On {cycle}", user_id="u1")

    assert [control["state"] for control in recording_spool.controls("recording.state")] == [
        "paused", "resumed", "paused", "resumed"]
    assert epochs[1] > epochs[0] > 0
    assert [item["event_id"] for item in recording_spool.events("baseline.captured")] == [
        "evt_baseline_s1_0", f"evt_baseline_s1_{epochs[0]}", f"evt_baseline_s1_{epochs[1]}"]


async def test_only_disabled_recording_pauses_a_period_not_an_unresolvable_identity(state, recording_spool):
    await create_user_message("s1", "First", user_id="u1")
    saved = (await read(SessionExecution, "s1")).trace_context
    # Recording stays on, but this write carries an identity that is not the owner's.
    with bind(TraceContext("u2", "s2", turn_id="foreign")):
        await create_user_message("s1", "Second", user_id="u1")
    assert recording_spool.controls("recording.state") == []
    assert "recording_paused" not in (await read(SessionExecution, "s1")).trace_context
    assert saved["recording_epoch"] == 0


async def test_a_run_start_that_is_the_first_recorded_activity_opens_period_zero_once(state, recording_spool,
                                                                                  business_statements):
    async with database.get_db_session() as db:
        db.add(Message(id="m-prior", session_id="s1", user_id="u1", role="user", created_at=runtime.now()))

    # No session write came first (a queued or resumed run): the run start itself opens the period.
    ticket = await runtime.start_run("s1", "u1")
    assert ticket is not None
    await runtime.finish_run(ticket, completed=True)
    await create_user_message("s1", "Next", user_id="u1")

    events = recording_spool.events()
    assert [item["type"] for item in events[:3]] == ["baseline.captured", "turn.started", "run.started"]
    [baseline] = recording_spool.events("baseline.captured")
    assert baseline["event_id"] == "evt_baseline_s1_0"
    assert [message["id"] for message in baseline["data"]["history"]] == ["m-prior"]
    assert _no_trajectory_sql(business_statements)


async def test_answering_a_question_asked_before_recording_opens_period_zero(state, recording_spool, monkeypatch):
    from question import question as q
    from tests.unit.test_durable_questions import checkpoint
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    request_id = await checkpoint(part_id="p-question")
    assert recording_spool.events() == []

    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    await q.reply(request_id, [["Yes"]], "u1")

    baselines = recording_spool.events("baseline.captured")
    assert [item["event_id"] for item in baselines] == ["evt_baseline_s1_0", f"question_adopt:{request_id}"]
    assert [message["id"] for message in baselines[0]["data"]["history"]] == ["m-p-question"]
    assert [item["event_id"] for item in recording_spool.events("question.resolved")] == [
        f"question.resolved:{request_id}:0"]


async def test_first_activity_detection_adds_no_query_for_callers_without_the_execution_row(
        state, recording_spool, business_statements):
    from storage import storage
    await storage.write(["todo", "s1"], {"items": [{"id": "t1", "content": "Plan", "status": "pending"}]})
    assert [item["type"] for item in recording_spool.events()] == ["todo.changed"]
    assert not [statement for statement in business_statements if "session_executions" in statement]


async def test_child_session_writes_leave_the_recording_markers_to_the_root(state, recording_spool):
    await create_user_message("s1", "Delegate", user_id="u1")
    root_saved = (await read(SessionExecution, "s1")).trace_context
    await add_session("child", parent_id="s1")
    child_trace = TraceContext.from_dict(root_saved).derive(
        source_session_id="child", agent_id="agent-child", run_id=None, generation=None,
        message_id=None, part_id=None)
    with bind(child_trace):
        await create_user_message("child", "Sub task", synthetic=True, user_id="u1")

    assert [item["event_id"] for item in recording_spool.events("baseline.captured")] == ["evt_baseline_s1_0"]
    child_events = [item for item in recording_spool.events() if item.get("source_session_id") == "child"]
    assert [item["type"] for item in child_events] == ["input.injected", "message.committed", "part.committed"]
    assert {item["session_id"] for item in child_events} == {"s1"}
    assert "recording_epoch" not in (await read(SessionExecution, "child")).trace_context
    assert (await read(SessionExecution, "s1")).trace_context == root_saved


async def test_a_rolled_back_write_emits_nothing_and_the_next_write_opens_the_period(state, recording_spool):
    with pytest.raises(RuntimeError):
        async with runtime.transaction("s1", "u1") as (db, _, _execution):
            await record_projection_in_tx(db, "s1", "u1", "session.settings_changed",
                                          {"after": {"title": "never"}})
            raise RuntimeError("the business write failed")
    assert recording_spool.lines() == []
    execution = await read(SessionExecution, "s1")
    assert execution is None or not identity(execution.trace_context)

    async with runtime.transaction("s1", "u1") as (db, _, _execution):
        await record_projection_in_tx(db, "s1", "u1", "session.settings_changed", {"after": {"title": "kept"}})
    assert [item["type"] for item in recording_spool.events()] == ["baseline.captured", "session.settings_changed"]


async def test_chat_writes_record_their_facts_with_asset_references(state, recording_spool, business_statements):
    prompt = await create_user_message("s1", "Show me", user_id="u1")
    assistant = await create_assistant_message("s1", prompt.id, user_id="u1")
    await _asset("asset_shot")
    await save_part(FilePart(id="p-shot", path="/workspace/shot.png", mime_type="image/png", asset_id="asset_shot",
                             oss_key="assets/u1/asset_shot/asset_shot.png", session_id="s1",
                             message_id=assistant.id), is_new=True, user_id="u1")
    assistant.summary, assistant.finish = True, "stop"
    await update_message_info(assistant, user_id="u1")

    part = next(item for item in recording_spool.events("part.committed") if item["data"]["part"]["id"] == "p-shot")
    assert part["data"]["role"] == "assistant" and part["message_id"] == assistant.id
    assert part["data"]["artifacts"]["asset_shot"]["event_id"] == artifact_event_id("s1", "asset_shot")
    [artifact] = recording_spool.events("artifact.recorded")
    assert artifact["data"]["role"] == "attachment"
    assert artifact["data"]["asset_ref"]["oss_key"] == "assets/u1/asset_shot/asset_shot.png"
    assert [item["data"]["summary_message_id"] for item in recording_spool.events("context.replaced")] == [assistant.id]

    assert await delete_messages_from("s1", assistant.id, user_id="u1") == prompt.id
    [regenerated] = recording_spool.events("history.regenerated")
    assert regenerated["data"]["removed_message_ids"] == [assistant.id]
    failed = await create_assistant_message("s1", prompt.id, user_id="u1")
    async with database.get_db_session() as db:
        (await db.get(Message, failed.id)).error = {"message": "provider error"}
    assert await delete_failed_turn("s1", failed.id, user_id="u1") == 2
    [dismissed] = recording_spool.events("history.reverted")
    assert dismissed["data"]["reason"] == "dismiss_failed_turn"
    assert _no_trajectory_sql(business_statements)


async def test_session_delete_reports_the_deletion_after_commit_with_recording_off(state, recording_spool,
                                                                                 monkeypatch):
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    monkeypatch.setattr("sandbox.sandbox_manager.release", AsyncMock())
    assert not await delete_session("s2", user_id="u1")  # Another owner's session: nothing happens.
    assert recording_spool.controls() == []

    assert await delete_session("s1", user_id="u1")
    [control] = recording_spool.controls()
    assert (control["type"], control["session_id"], control["user_id"]) == ("session.deleted", "s1", "u1")
    assert control["deleted_at"].endswith("Z")
    assert (await read(Session, "s1")).is_deleted
    assert recording_spool.events() == []


async def test_fork_records_history_forked_with_its_source_root_and_no_trajectory_sql(state, recording_spool,
                                                                                    business_statements):
    from session.fork import fork_session
    await create_user_message("s1", "Original question", user_id="u1")
    forked = await fork_session("s1", user_id="u1")

    events = recording_spool.events()
    fork_events = [item for item in events if item["type"] in {"history.forked", "baseline.captured"}]
    assert [(item["type"], item["event_id"]) for item in fork_events] == [
        ("baseline.captured", "evt_baseline_s1_0"),
        ("history.forked", f"fork:{forked.id}:source"),
        ("baseline.captured", f"evt_baseline_{forked.id}_0"),
        ("history.forked", f"fork:{forked.id}:destination"),
    ]
    outgoing, incoming = fork_events[1], fork_events[3]
    assert outgoing["session_id"] == "s1" and incoming["session_id"] == forked.id
    for relation in (outgoing["data"], incoming["data"]):
        assert relation["source_root_session_id"] == "s1"
        assert relation["target_session_id"] == forked.id
        assert "source_trajectory_id" not in relation and "source_through_seq" not in relation
    assert [message["role"] for message in fork_events[2]["data"]["history"]] == ["user"]
    assert _no_trajectory_sql(business_statements)


async def test_fork_while_recording_is_off_reports_a_pause_for_a_recorded_source(state, recording_spool,
                                                                                monkeypatch):
    from session.fork import fork_session
    await create_user_message("s1", "Recorded first", user_id="u1")
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    await fork_session("s1", user_id="u1")
    assert [control["state"] for control in recording_spool.controls("recording.state")] == ["paused"]
    assert recording_spool.events("history.forked") == []


async def test_revert_reports_the_restore_and_never_waits_on_recording(state, recording_spool, monkeypatch):
    from session import revert
    prompt = await create_user_message("s1", "Edit the files", user_id="u1")
    async with database.get_db_session() as db:
        db.add(Part(id="p-step", session_id="s1", message_id=prompt.id, user_id="u1", type="step-start",
                    data={"id": "p-step", "type": "step-start", "snapshot": "snap-before",
                          "session_id": "s1", "message_id": prompt.id}, created_at=runtime.now()))
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", AsyncMock(return_value=None))
    monkeypatch.setattr(revert.snapshot, "track", AsyncMock(return_value="snap-now"))
    restore = AsyncMock(return_value=True)
    monkeypatch.setattr(revert.snapshot, "restore", restore)

    assert await revert.revert_to_message("s1", prompt.id, user_id="u1")
    reverted = recording_spool.events("history.reverted")
    assert [item["data"]["status"] for item in reverted] == ["requested", "completed"]
    assert reverted[0]["event_id"].endswith(":requested") and reverted[1]["event_id"].endswith(":finished")
    assert reverted[0]["data"]["to_snapshot"] == "snap-before"

    restore.side_effect = RuntimeError("sandbox unavailable")
    assert not await revert.revert_to_message("s1", prompt.id, user_id="u1")
    assert recording_spool.events("history.reverted")[-1]["data"]["status"] == "failed"

    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    restore.side_effect = None
    assert await revert.revert_to_message("s1", prompt.id, user_id="u1")
    assert [control["state"] for control in recording_spool.controls("recording.state")] == ["paused"]
    assert len(recording_spool.events("history.reverted")) == 4


async def test_todo_writes_take_no_session_lock_and_their_fact_waits_for_commit(state, recording_spool):
    from storage import storage
    locks = []

    def inspect_statement(orm_execute_state):
        locks.append(getattr(orm_execute_state.statement, "_for_update_arg", None))
    event.listen(SyncSession, "do_orm_execute", inspect_statement)
    try:
        pending = {"items": [{"id": "t1", "content": "Plan", "status": "pending"}]}
        await storage.write(["todo", "s1"], pending)
        with bind(TraceContext("u1", "s1", turn_id="turn-1")):
            await storage.write(["todo", "s1"], {"items": [{"id": "t1", "content": "Plan", "status": "completed"}]})
    finally:
        event.remove(SyncSession, "do_orm_execute", inspect_statement)

    assert locks and all(lock is None for lock in locks)
    changes = recording_spool.events("todo.changed")
    assert [item["data"]["before"] for item in changes] == [None, pending]
    assert changes[1]["turn_id"] == "turn-1" and changes[1]["data"]["items"][0]["status"] == "completed"
