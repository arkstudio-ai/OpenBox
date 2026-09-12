"""Auxiliary provider calls and executor failures at real persistence boundaries."""
import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from db.models.billing import UsageEvent
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.trajectory import SessionTrajectory, TrajectoryEvent
from tests.integration.test_trajectory_storage import tracedb
from tool.tool import ToolContext
from trajectory import TraceContext, bind
from trajectory.payload import expand, read_payload
from trajectory.types import RecordingError, now


async def test_image_edit_captures_actual_parameters_owned_versions_and_existing_bill(tracedb, monkeypatch):
    from tool import image_gen as images

    factory, _ = tracedb
    source = b"\x89PNG\r\n\x1a\nsource-image"
    output = b"\x89PNG\r\n\x1a\nprovider-output"
    trace = TraceContext("a", "session_a_1", workspace_id="ws_a", turn_id="image_turn",
                         run_id="image_run", request_id="parent_chat_request",
                         call_id="image_call", message_id="image_message")
    ctx = ToolContext(user_id="a", session_id="session_a_1", workspace_id="ws_a",
                      project_id="prj_a", message_id="image_message", part_id="image_part",
                      trace_context=trace)
    async with factory.begin() as db:
        db.add(Message(id=ctx.message_id, session_id=ctx.session_id, user_id="a",
                       role="assistant", created_at=now()))
        db.add(FileAsset(id="image_input", user_id="a", workspace_id="ws_a",
                         session_id=ctx.session_id, name="source.png", oss_key="assets/a/source",
                         mime="image/png", size=len(source), status="ready", is_deleted=False,
                         created_at=now()))

    target = images.ProviderTarget("fixture", "gpt-image-2", "fixture-private-api-key", None, 30)
    settings = SimpleNamespace(default_size="1024x1024", default_quality="high",
                               output_format="png", dedupe=False)
    monkeypatch.setattr(images, "_configured_target", lambda: (target, settings))
    monkeypatch.setenv("BILLING_MODE", "shadow")
    oss = SimpleNamespace(delete=AsyncMock())
    monkeypatch.setattr("core.oss.get_oss", lambda: oss)
    monkeypatch.setattr(images, "_load_inputs", AsyncMock(return_value=(
        [images.InputImage("image_input", "source.png", "image/png", source)], None)))
    monkeypatch.setattr(images, "_upload_bytes", AsyncMock(return_value=len(output)))
    response = SimpleNamespace(created=123, usage={"input_tokens": 8, "output_tokens": 2},
        data=[SimpleNamespace(b64_json=base64.b64encode(output).decode(), url=None,
                              revised_prompt="draw a blue square")])
    client = SimpleNamespace(images=SimpleNamespace(edit=AsyncMock(return_value=response),
                                                     generate=AsyncMock()), close=AsyncMock())
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **_kwargs: client)
    args = images.ImageGenArgs(prompt="draw a blue square", input_images=["image_input"],
                               size="1024x1024", quality="high", background="opaque")
    with bind(trace):
        result = await images.execute(args, ctx)
    assert result.metadata["asset_id"]
    client.images.generate.assert_not_called()
    client.images.edit.assert_awaited_once()
    wire = client.images.edit.await_args.kwargs
    assert wire == {"model": "gpt-image-2", "prompt": args.prompt, "n": 1, "size": "1024x1024",
                    "quality": "high", "output_format": "png", "background": "opaque",
                    "image": ("source.png", source, "image/png")}
    client.close.assert_awaited_once()

    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        rows = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        events = [(row, await expand(db, trajectory.id, row.data, through_seq=trajectory.committed_seq))
                  for row in rows]
        prepared = [(row, data) for row, data in events if row.type == "request.prepared"]
        assert len(prepared) == 1
        request_id = prepared[0][0].context["request_id"]
        assert request_id != trace.request_id
        assert prepared[0][0].context["parent_call_id"] == trace.call_id
        assert prepared[0][1]["input"]["prompt"] == args.prompt
        assert prepared[0][1]["input"]["input"]["images"][0]["sha256"] == hashlib.sha256(source).hexdigest()
        finished = next(data for row, data in events if row.type == "request.finished")
        assert finished["status"] == "completed" and finished["duration_ms"] >= 0
        assert finished["ttft_ms"] is None  # A nonstreaming image has no observed token latency.
        artifacts = [(row, data) for row, data in events if row.type == "artifact.recorded"]
        output_event, retained = next((row, data) for row, data in artifacts
                                      if data["artifact_id"] == result.metadata["asset_id"])
        assert output_event.context["request_id"] == request_id
        assert output_event.context["call_id"] == trace.call_id
        _, content = await read_payload(db, trajectory.id, retained["payload"]["payload_id"],
                                        through_seq=trajectory.committed_seq)
        assert content == output
        bills = (await db.scalars(select(UsageEvent))).all()
        assert len(bills) == 1 and bills[0].kind == "image_gen"
        billed = [data for row, data in events if row.type == "request.usage"
                  and data.get("source") == "existing_billing_ledger"]
        assert len(billed) == 1 and billed[0]["billing_usage_event_id"] == bills[0].id
        assert billed[0]["usage"]["input_tokens"] == 8 and billed[0]["usage"]["images"] == 1
        encoded = json.dumps([data for _, data in events])
        assert target.api_key not in encoded and base64.b64encode(output).decode() not in encoded


@pytest.mark.parametrize("failure_at", ["idle_judge", "output"])
async def test_bash_recording_failure_does_not_kill_or_reexecute(monkeypatch, failure_at):
    from tool import bash
    from sandbox.client import IdleNotification

    failure = RecordingError("fixture recording failure")
    async def stream(**_kwargs):
        if failure_at == "idle_judge":
            yield IdleNotification(pid=77, idle_seconds=60, total_seconds=60)
        else:
            yield SimpleNamespace(content="work already happened")
    sandbox = SimpleNamespace(execute_stream=stream, execute=AsyncMock(), kill_command=AsyncMock())
    ctx = ToolContext(sandbox=sandbox)
    if failure_at == "idle_judge":
        monkeypatch.setattr("core.config.get_config", lambda: SimpleNamespace(model="fixture"))
        monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _model: {})
        monkeypatch.setattr("agent.llm.metered_completion", AsyncMock(side_effect=failure))
    else:
        ctx._on_output = AsyncMock(side_effect=failure)
    with pytest.raises(RecordingError):
        await bash.execute(bash.BashArgs(command="fixture-command"), ctx)
    sandbox.execute.assert_not_called()
    sandbox.kill_command.assert_not_called()


async def test_retained_file_hash_matches_redacted_content(tracedb):
    from trajectory.files import record_file_change
    factory, _ = tracedb
    ctx = ToolContext(user_id="a", session_id="session_a_1", trace_context=TraceContext("a", "session_a_1"))
    await record_file_change(ctx, "/workspace/config.json", operation="edit",
                             before='{"password":"old-secret","name":"before"}',
                             after='{"password":"new-secret","name":"after"}')
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        event = await db.scalar(select(TrajectoryEvent).where(TrajectoryEvent.type == "artifact.recorded"))
        data = await expand(db, trajectory.id, event.data, through_seq=trajectory.committed_seq)
        for key in ("before", "after"):
            assert data[key]["redacted"] is True
            assert data[key]["sha256"] == hashlib.sha256(data[key]["text"].encode()).hexdigest()
        assert "old-secret" not in json.dumps(data) and "new-secret" not in json.dumps(data)


async def test_shutdown_stops_archive_after_receipt_failure(monkeypatch):
    from main import _shutdown_trajectory
    flush = AsyncMock(side_effect=RecordingError("fixture"))
    archive, exports = AsyncMock(), AsyncMock()
    monkeypatch.setattr("trajectory.flush", flush)
    monkeypatch.setattr("trajectory.payload.stop_archive_worker", archive)
    monkeypatch.setattr("trajectory.export.stop_exports", exports)
    with pytest.raises(RecordingError):
        await _shutdown_trajectory()
    archive.assert_awaited_once()
    exports.assert_awaited_once()


async def test_desktop_initializes_dedicated_tickets_without_enabling_auth(monkeypatch):
    from main import _init_infrastructure
    from core.config import OpenBoxConfig
    import auth.middleware as middleware
    import auth.ticket as ticket
    monkeypatch.setattr(middleware, "_auth_enabled", False)
    monkeypatch.setattr(ticket, "_cache", None)
    config = OpenBoxConfig(jwt_secret="")
    _init_infrastructure(config)
    try:
        value = await ticket.create_ticket("default", "admin", audience="admin_trajectories")
        assert await ticket.consume_ticket(value) is None
        assert (await ticket.consume_ticket(value, audience="admin_trajectories"))["user_id"] == "default"
        assert middleware._auth_enabled is False
    finally:
        await config._cache.close()


async def test_fork_retains_baseline_only_and_new_root_records_future_input(tracedb):
    from db.models.part import Part
    from db.models.session import Session
    from session.fork import fork_session
    from session.session import create_user_message

    factory, _ = tracedb
    async with factory.begin() as db:
        db.add(Message(id="before_launch", session_id="session_a_1", user_id="a",
                       role="user", created_at=now()))
        await db.flush()
        db.add(Part(id="before_launch_text", message_id="before_launch", session_id="session_a_1",
                    user_id="a", type="text", data={"id": "before_launch_text", "type": "text",
                    "text": "old conversation", "message_id": "before_launch", "session_id": "session_a_1"},
                    created_at=now()))
    fork = await fork_session("session_a_1", up_to_message_id="before_launch", user_id="a")
    async with factory() as db:
        target = await db.get(Session, fork.id)
        assert target.parent_id is None  # A fork owns a new root; it is not a child executor.
        roots = (await db.scalars(select(SessionTrajectory))).all()
        assert {root.session_id for root in roots} == {"session_a_1", fork.id}
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        assert not any(event.type in {"input.accepted", "tool.started", "request.started"} for event in events)
        fork_events = [event for event in events if event.type == "history.forked"]
        assert len(fork_events) == 2
        assert {event.data["direction"] for event in fork_events} == {"incoming", "outgoing"}
        baseline = next(event for event in events if event.session_id == fork.id and event.type == "baseline.captured")
        assert baseline.data["history"][0]["parts"][0]["text"] == "old conversation"
    await create_user_message(fork.id, "new monitored question", user_id="a")
    async with factory() as db:
        inputs = (await db.scalars(select(TrajectoryEvent).where(TrajectoryEvent.type == "input.accepted"))).all()
        assert len(inputs) == 1 and inputs[0].session_id == fork.id


async def test_snapshot_restore_and_undo_have_observed_terminal_events(tracedb, monkeypatch):
    from session import revert
    factory, _ = tracedb
    message = SimpleNamespace(id="snapshot_message", role="assistant", parent_id=None,
                               parts=[{"type": "step-start", "snapshot": "before_snapshot"}])
    monkeypatch.setattr(revert, "get_messages", AsyncMock(return_value=[message]))
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(revert.snapshot, "track", AsyncMock(return_value="current_snapshot"))
    restore = AsyncMock(return_value=True)
    monkeypatch.setattr(revert.snapshot, "restore", restore)
    monkeypatch.setattr(revert, "_revert_snapshots", {})
    assert await revert.revert_to_message("session_a_1", "snapshot_message", user_id="a")
    assert await revert.unrevert("session_a_1", user_id="a")
    assert restore.await_count == 2
    async with factory() as db:
        rows = (await db.scalars(select(TrajectoryEvent).where(
            TrajectoryEvent.type == "history.reverted").order_by(TrajectoryEvent.seq))).all()
        assert [row.data["status"] for row in rows] == ["requested", "completed", "requested", "completed"]
        assert rows[0].data["from_snapshot"] == "current_snapshot"
        assert rows[1].data["to_snapshot"] == "before_snapshot"
        assert rows[3].data["operation"] == "undo_restore"
