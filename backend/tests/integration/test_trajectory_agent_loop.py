"""The application input route reaches the real loop, hooks and durable journal."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update, text

from auth.jwt import create_access_token
from db.models.message import Message
from db.models.part import Part
from db.models.question import SessionExecution
from db.models.session import Session
from db.models.trajectory import SessionTrajectory, TrajectoryEvent
from db.models.workspace import WorkspaceMember
from tests.integration.test_trajectory_boundaries import app_client
from tests.integration.test_trajectory_storage import tracedb
from trajectory.repository import state_at
from trajectory.types import now


class Chunk(SimpleNamespace):
    def model_dump(self, **kwargs):
        def public(value):
            if isinstance(value, SimpleNamespace):
                return {key: public(item) for key, item in vars(value).items()}
            if isinstance(value, list):
                return [public(item) for item in value]
            return value
        return public(self)


def chunk(*, content=None, reasoning=None, arguments=None, tool="trace_echo", call_id="provider_call", usage=None):
    calls = [] if arguments is None else [SimpleNamespace(index=0, id=call_id,
        function=SimpleNamespace(name=tool, arguments=arguments))]
    return Chunk(choices=[SimpleNamespace(index=0, finish_reason=None, delta=SimpleNamespace(
        content=content, reasoning_content=reasoning, tool_calls=calls))], usage=usage)


@pytest.mark.asyncio
async def test_user_input_through_application_agent_loop_and_auxiliary_request(app_client, tracedb, monkeypatch):
    import litellm
    import sandbox
    import tool.registry
    from agent import loop
    from core.config import OpenBoxConfig
    from tool.tool import ToolResult, define_tool

    app, client, _, _ = app_client
    factory, _ = tracedb
    config = OpenBoxConfig.model_validate({
        "model": "openai/trajectory-test", "jwt_secret": "isolated-trajectory-boundary-tests-key",
        "provider": {"openai": {"api_key": "test-private-key", "base_url": "https://provider.invalid"}},
        "models": [{"id": "openai/trajectory-test"}],
        "agent": {"trace_fixture": {"prompt": "Use the provided tool, then answer.",
            "tools": ["trace_echo"], "max_steps": 4,
            "permission": [{"permission": "*", "pattern": "*", "action": "allow"}]}},
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("api.sessions.get_config", lambda: config)
    monkeypatch.setattr("agent.suggestions.get_config", lambda: config)
    suggestion_diagnostics = []
    monkeypatch.setattr("agent.suggestions.log.debug", lambda message, *args:
                        suggestion_diagnostics.append(message % args))
    monkeypatch.setenv("BILLING_MODE", "shadow")
    monkeypatch.setenv("TRAJECTORY_BATCH_MS", "1")
    monkeypatch.setenv("TRAJECTORY_INLINE_BYTES", "500000")
    monkeypatch.setattr(sandbox.sandbox_manager, "get_client", AsyncMock(return_value=None))
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: False)
    # Local instruction discovery is unrelated to this fixture's inputs.
    monkeypatch.setattr("session.instruction.instruction_system_with_config", AsyncMock(return_value=[]))
    executed = []

    class Arguments(BaseModel):
        value: str

    async def execute(arguments, context):
        executed.append((arguments.value, context.trace_context))
        await context.update_output("working: " + arguments.value)
        return ToolResult(title="Echo", output="tool result: " + arguments.value)
    fixture_tool = define_tool("trace_echo", description="Return a test value", parameters=Arguments,
                               execute=execute, sandbox_required=False)
    monkeypatch.setattr(tool.registry, "_tools", {"trace_echo": fixture_tool})
    provider_calls = []
    emitted = []

    async def completion(**kwargs):
        provider_calls.append(kwargs)
        number = len(provider_calls)
        async def stream():
            if number == 1:
                values = [chunk(reasoning="Inspecting input"),
                    chunk(arguments='{"value":"'), chunk(arguments='captured"}')]
            elif number == 2:
                values = [chunk(content="Completed "), chunk(content="with the tool.")]
            else:
                assert number == 3, "Unexpected extra model dispatch"
                name = kwargs["tools"][0]["function"]["name"]
                values = [chunk(arguments='{"items":[],"context_summary":"Task finished"}', tool=name)]
            values.append(chunk(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=6, total_tokens=18)))
            for value in values:
                emitted.append(number)
                yield value
        return stream()
    monkeypatch.setattr(litellm, "acompletion", completion)
    async with factory.begin() as db:
        await db.execute(text("CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP NOT NULL)"))
        db.add(WorkspaceMember(workspace_id="ws_a", user_id="a", role="owner", status="active",
                               created_at=now(), updated_at=now()))
        session = await db.get(Session, "session_a_1")
        session.model, session.agent = "openai/trajectory-test", "trace_fixture"
    response = await client.post("/api/agent/session/session_a_1/message",
        headers={"Authorization": "Bearer " + create_access_token("a", "user"), "X-Workspace-Id": "ws_a"},
        json={"text": "Use the echo tool and report its result", "agent": "trace_fixture"})
    assert response.status_code == 200, response.text
    if loop._background_tasks:
        await asyncio.wait_for(asyncio.gather(*tuple(loop._background_tasks)), timeout=10)
    if len(provider_calls) != 3:
        async with factory() as db:
            execution = await db.get(SessionExecution, "session_a_1")
            session = await db.get(Session, "session_a_1")
            saved = (await db.scalars(select(Message).where(Message.session_id == session.id)
                .order_by(Message.created_at, Message.id))).all()
            suggestion_diagnostics.append((session.status, session.kind, session.parent_id,
                execution.generation, execution.run_id, execution.resume_pending,
                [(m.role, m.finish, m.error, m.summary) for m in saved]))
    assert len(provider_calls) == 3, json.dumps(suggestion_diagnostics)
    assert executed[0][0] == "captured"
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).where(
            TrajectoryEvent.session_id == "session_a_1").order_by(TrajectoryEvent.seq))).all()
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        accepted = [event for event in events if event.type == "input.accepted"]
        assert len(accepted) == 1
        turn_id = accepted[0].context["turn_id"]
        assert {event.context.get("turn_id") for event in events} == {turn_id}
        requests = [event for event in events if event.type == "request.prepared"]
        assert len({event.request_id for event in requests}) == 3
        assert [event.data["purpose"] for event in requests] == ["chat", "chat", "suggestions"]
        assert len([event for event in events if event.type == "request.delta"]) == len(emitted)
        assert len([event for event in events if event.type == "step.started"]) == 2
        assert len([event for event in events if event.type == "step.finished"]) == 2
        tool_events = [event for event in events if event.type.startswith("tool.")]
        assert [event.type for event in tool_events] == [
            "tool.requested", "tool.started", "tool.output", "tool.output", "tool.finished"]
        assert len({event.call_id for event in tool_events}) == 1
        assert tool_events[0].data["schema_source"] == "provider_request"
        assert tool_events[0].data["schema"] == provider_calls[0]["tools"][0]
        assert tool_events[-1].data["status"] == "completed"
        assert tool_events[-1].data["duration_ms"] is not None
        assert any(event.type == "run.finished" and event.data["status"] == "completed" for event in events)
        assert any(event.type == "turn.finished" for event in events)
        assert "test-private-key" not in json.dumps([event.data for event in events])
        from db.models.billing import UsageEvent
        ledger_usage = (await db.scalars(select(UsageEvent).where(UsageEvent.session_id == "session_a_1"))).all()
        settlements = [event for event in events if event.type == "request.usage" and
                       event.data.get("source") == "existing_billing_ledger"]
        assert len(ledger_usage) == len(settlements) == 3
        assert {event.data["billing_usage_event_id"] for event in settlements} == {row.id for row in ledger_usage}
        assert all(row.total_tokens == 18 for row in ledger_usage)
        assert all(event.data["usage"]["total"] == 18 for event in settlements)
        execution = await db.get(SessionExecution, "session_a_1")
        assert execution.run_id is None
        messages = (await db.scalars(select(Message).where(Message.session_id == "session_a_1"))).all()
        assert len(messages) == 3
        parts = (await db.scalars(select(Part).where(Part.session_id == "session_a_1"))).all()
        assert any(part.type == "text" and part.data.get("text") == "Completed with the tool." for part in parts)
        trajectory = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.session_id == "session_a_1"))
        state = await state_at(db, trajectory)
        assert state["records"]["user:" + accepted[0].context["message_id"]]["preview"]
        assert len([record for record in state["records"].values() if record["kind"] == "tool"]) == 1
        assert all(record["record_id"].split(":", 1)[1] in {event.request_id for event in requests}
                   for record in state["records"].values() if record["kind"] == "assistant")


@pytest.mark.asyncio
async def test_pause_resume_captures_new_baseline_and_reads_attachments_before_write_lock(tracedb, monkeypatch):
    from db.models.file_asset import FileAsset
    from models.message import FilePart
    from session.session import create_user_message, save_part
    from trajectory.payload import read_payload
    factory, _ = tracedb
    async with factory.begin() as db:
        for identifier in ("old_asset", "new_asset"):
            db.add(FileAsset(id=identifier, user_id="a", workspace_id="ws_a", session_id="session_a_1",
                name=identifier + ".png", oss_key="assets/a/" + identifier, mime="image/png", size=3,
                status="ready", is_deleted=False, created_at=now()))
    fetched = []

    async def source_bytes(asset):
        # A second connection can write while the source object is being
        # downloaded: no session or journal write transaction is holding it.
        async with asyncio.timeout(2), factory.begin() as db:
            await db.execute(update(Session).where(Session.id == "session_a_2").values(title="source-read"))
        fetched.append(asset.id)
        return b"immutable " + asset.id.encode()
    monkeypatch.setattr("trajectory.artifacts.read_asset_bytes", source_bytes)
    await create_user_message("session_a_1", "first recorded input", user_id="a")
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    gap_message = await create_user_message("session_a_1", "input while paused", user_id="a")
    await save_part(FilePart(session_id="session_a_1", message_id=gap_message.id, asset_id="old_asset"),
                    is_new=True, user_id="a")
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        assert events[-1].type == "recording.gap" and events[-1].data["phase"] == "paused"
        assert "input while paused" not in json.dumps([event.data for event in events])
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    resumed = await create_user_message("session_a_1", "input after resume", user_id="a")
    for identifier in ("old_asset", "new_asset"):
        await save_part(FilePart(session_id="session_a_1", message_id=resumed.id, asset_id=identifier),
                        is_new=True, user_id="a")
    assert fetched == ["old_asset", "new_asset"]
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        baselines = [event for event in events if event.type == "baseline.captured"]
        assert len(baselines) == 2
        assert [event.data["text"] for event in events if event.type == "input.accepted"] == [
            "first recorded input", "input after resume"]
        assert [event.data["phase"] for event in events if event.type == "recording.gap"] == ["paused", "resumed"]
        reference = baselines[-1].data["artifacts"]["old_asset"]
        _, content = await read_payload(db, baselines[-1].trajectory_id, reference["payload_id"], through_seq=events[-1].seq)
        assert content == b"immutable old_asset"
        assert len([event for event in events if event.type == "artifact.recorded"]) == 2
        trajectory = await db.scalar(select(SessionTrajectory))
        assert trajectory.recording_status == "gap"


@pytest.mark.asyncio
async def test_multilevel_task_uses_one_root_and_independent_child_runs(app_client, tracedb, monkeypatch):
    import litellm
    import sandbox
    import tool.registry
    from agent import loop
    from core.config import OpenBoxConfig
    from tool.task import task_tool
    app, client, _, _ = app_client
    factory, _ = tracedb
    agent = {"prompt": "Complete the delegated task.", "max_steps": 3,
             "permission": [{"permission": "*", "pattern": "*", "action": "allow"}]}
    config = OpenBoxConfig.model_validate({
        "model": "openai/trajectory-test", "jwt_secret": "isolated-trajectory-boundary-tests-key",
        "provider": {"openai": {"api_key": "test-private-key", "base_url": "https://provider.invalid"}},
        "models": [{"id": "openai/trajectory-test"}],
        "agent": {"trace_root": {**agent, "tools": ["task"]},
                  "trace_middle": {**agent, "mode": "subagent", "tools": ["task"]},
                  "trace_leaf": {**agent, "mode": "subagent", "tools": []}},
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("api.sessions.get_config", lambda: config)
    monkeypatch.setattr("agent.suggestions.get_config", lambda: config)
    monkeypatch.setenv("BILLING_MODE", "off")
    monkeypatch.setenv("TRAJECTORY_BATCH_MS", "1")
    monkeypatch.setattr(sandbox.sandbox_manager, "get_client", AsyncMock(return_value=None))
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: False)
    monkeypatch.setattr("session.instruction.instruction_system_with_config", AsyncMock(return_value=[]))
    monkeypatch.setattr(tool.registry, "_tools", {"task": task_tool})
    provider_calls = []

    async def completion(**kwargs):
        provider_calls.append(kwargs)
        number = len(provider_calls)
        async def stream():
            if number in {1, 2}:
                target = "trace_middle" if number == 1 else "trace_leaf"
                yield chunk(tool="task", call_id=f"delegate_{number}", arguments=json.dumps({
                    "description": target, "prompt": "Finish " + target, "subagent_type": target}))
            elif number in {3, 4, 5}:
                yield chunk(content={3: "leaf result", 4: "middle result", 5: "root result"}[number])
            else:
                assert number == 6, "Delegation dispatched an unexpected model request"
                yield chunk(tool=kwargs["tools"][0]["function"]["name"],
                            arguments='{"items":[],"context_summary":"delegation complete"}')
            yield chunk(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=6, total_tokens=18))
        return stream()
    monkeypatch.setattr(litellm, "acompletion", completion)
    async with factory.begin() as db:
        await db.execute(text("CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP NOT NULL)"))
        db.add(WorkspaceMember(workspace_id="ws_a", user_id="a", role="owner", status="active",
                               created_at=now(), updated_at=now()))
        session = await db.get(Session, "session_a_1")
        session.model, session.agent = "openai/trajectory-test", "trace_root"
    response = await client.post("/api/agent/session/session_a_1/message",
        headers={"Authorization": "Bearer " + create_access_token("a", "user"), "X-Workspace-Id": "ws_a"},
        json={"text": "Delegate through two levels", "agent": "trace_root"})
    assert response.status_code == 200, response.text
    if loop._background_tasks:
        await asyncio.wait_for(asyncio.gather(*tuple(loop._background_tasks)), timeout=10)
    assert len(provider_calls) == 6
    async with factory() as db:
        trajectories = (await db.scalars(select(SessionTrajectory))).all()
        assert [trajectory.session_id for trajectory in trajectories] == ["session_a_1"]
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        assert {event.user_id for event in events} == {"a"}
        assert {event.session_id for event in events} == {"session_a_1"}
        assert len({event.context.get("turn_id") for event in events}) == 1
        spawned = [event for event in events if event.type == "agent.spawned"]
        finished = [event for event in events if event.type == "agent.finished"]
        assert len(spawned) == len(finished) == 2
        assert {event.data["status"] for event in finished} == {"completed"}
        assert len({event.source_session_id for event in spawned}) == 2
        assert spawned[1].context["parent_agent_id"] == spawned[0].context["agent_id"]
        for event in spawned:
            source = await db.get(Session, event.source_session_id)
            assert source.parent_id == ("session_a_1" if event is spawned[0] else spawned[0].source_session_id)
            execution = await db.get(SessionExecution, source.id)
            assert execution.trace_context["session_id"] == "session_a_1"
            assert execution.trace_context["source_session_id"] == source.id
            assert execution.run_id is None
            assert event.context["parent_call_id"]
        runs = [event for event in events if event.type == "run.started"]
        assert len(runs) == len({event.context["run_id"] for event in runs}) == 3
        run_by_source = {event.source_session_id: event for event in runs}
        requests = [event for event in events if event.type == "request.started"]
        requested = [event for event in events if event.type == "tool.requested"]
        assert len(requested) == 2 and len({event.call_id for event in requested}) == 2
        for event in requests:
            run = run_by_source[event.source_session_id]
            assert event.context["run_id"] == run.context["run_id"]
            assert event.context["agent_id"] == run.context["agent_id"]
            assert event.context["generation"] == run.context["generation"]
        for child in spawned:
            parent_call = next(event for event in requested if event.call_id == child.context["parent_call_id"])
            assert parent_call.context["agent_id"] == child.context["parent_agent_id"]
            assert parent_call.context["run_id"] == run_by_source[parent_call.source_session_id].context["run_id"]
            assert parent_call.request_id in {event.request_id for event in requests
                                              if event.source_session_id == parent_call.source_session_id}
            child_requests = [event for event in requests if event.source_session_id == child.source_session_id]
            assert child_requests and all(event.context["agent_id"] == child.context["agent_id"]
                                          for event in child_requests)
            assert all(event.context["parent_call_id"] == parent_call.call_id for event in child_requests)
        assert len([event for event in events if event.type == "turn.started"]) == 1
        assert len([event for event in events if event.type == "turn.finished"]) == 1
        assert len({event.request_id for event in events if event.type == "request.started"}) == 6
        assert len([event for event in events if event.type == "request.delta"]) == 12
        assert len([event for event in events if event.type == "run.finished"
                    and event.data["status"] == "completed"]) == 3
        preparations = [event for event in events if event.type == "request.prepared"]
        assert all(event.data["capture_level"] == "adapter_input"
                   and event.data["sdk_internal_attempts"] == "not_observed" for event in preparations)
        replies = [event.data["output"] for event in events if event.type == "agent.message"]
        assert replies == ["leaf result", "middle result"]


@pytest.mark.asyncio
async def test_real_compaction_prunes_effective_context_but_preserves_replay(
    app_client, tracedb, monkeypatch, tmp_path,
):
    import re
    import litellm
    import sandbox
    import tool.registry
    from agent import loop
    from agent.compaction import CHUNK_SUMMARY_PROMPT, COMPACTION_PROMPT
    from core.config import OpenBoxConfig
    from session.compaction import filter_compacted
    from session.session import get_messages
    from tool.tool import ToolResult, define_tool

    app, client, _, _ = app_client
    factory, _ = tracedb
    config = OpenBoxConfig.model_validate({
        "model": "openai/trajectory-test", "mcp_filter_model": "openai/trajectory-test",
        "jwt_secret": "isolated-trajectory-boundary-tests-key",
        "provider": {"openai": {"api_key": "test-private-key", "base_url": "https://provider.invalid"}},
        "models": [{"id": "openai/trajectory-test", "context_limit": 10000}],
        "compaction": {"auto": False, "prune": True},
        "agent": {"trace_archive": {"prompt": "Read the requested archive, then answer.",
            "tools": ["trace_archive"], "max_steps": 3,
            "permission": [{"permission": "*", "pattern": "*", "action": "allow"}]}},
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("api.sessions.get_config", lambda: config)
    monkeypatch.setattr("agent.suggestions.get_config", lambda: config)
    monkeypatch.setenv("BILLING_MODE", "off")
    monkeypatch.setenv("TRAJECTORY_BATCH_MS", "1")
    monkeypatch.setenv("TRAJECTORY_INLINE_BYTES", "1000000")
    monkeypatch.setattr(sandbox.sandbox_manager, "get_client", AsyncMock(return_value=None))
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: False)
    monkeypatch.setattr("session.instruction.instruction_system_with_config", AsyncMock(return_value=[]))
    truncation_dir = tmp_path / "tool-output"
    truncation_dir.mkdir()
    monkeypatch.setattr("tool.truncation._data_dir", str(truncation_dir))
    # Larger than the real model-output truncation limit. The final line can
    # only survive in the explicit executor-result fact, not in the chat Part.
    archives = {index: "".join(f"ORIGINAL-ARCHIVE-{index}-{line}: " + "x" * 70 + "\n"
                              for line in range(1000)) for index in range(1, 4)}

    class Arguments(BaseModel):
        index: int

    async def execute(arguments, context):
        return ToolResult(title="Archived result", output=archives[arguments.index])

    archive_tool = define_tool("trace_archive", description="Read an archive", parameters=Arguments,
                               execute=execute, sandbox_required=False)
    monkeypatch.setattr(tool.registry, "_tools", {"trace_archive": archive_tool})
    provider_calls = []
    ordinary_calls = {}

    async def completion(**kwargs):
        provider_calls.append(kwargs)
        messages = kwargs["messages"]
        last = messages[-1].get("content", "")
        names = [definition.get("function", {}).get("name") for definition in (kwargs.get("tools") or [])]
        if "StructuredOutput" in names:
            values = [chunk(tool="StructuredOutput", arguments='{"items":[],"context_summary":"Finished"}')]
        elif last == CHUNK_SUMMARY_PROMPT:
            values = [chunk(content="Captured chunk summary.")]
        elif last == COMPACTION_PROMPT:
            values = [chunk(content="PRESERVED-SUMMARY: three archives were read.")]
        elif "continue after compaction" in json.dumps(messages):
            values = [chunk(content="Continued using the preserved summary.")]
        else:
            index = int(re.findall(r"history turn (\d)", json.dumps(messages))[-1])
            ordinary_calls[index] = ordinary_calls.get(index, 0) + 1
            values = ([chunk(tool="trace_archive", call_id=f"archive_{index}",
                             arguments=json.dumps({"index": index}))]
                      if ordinary_calls[index] == 1 else [chunk(content=f"Archive {index} read.")])

        async def stream():
            for value in values:
                yield value
            yield chunk(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=6, total_tokens=18))
        return stream()

    monkeypatch.setattr(litellm, "acompletion", completion)
    async with factory.begin() as db:
        await db.execute(text("CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP NOT NULL)"))
        db.add(WorkspaceMember(workspace_id="ws_a", user_id="a", role="owner", status="active",
                               created_at=now(), updated_at=now()))
        session = await db.get(Session, "session_a_1")
        session.model, session.agent = "openai/trajectory-test", "trace_archive"
    headers = {"Authorization": "Bearer " + create_access_token("a", "user"), "X-Workspace-Id": "ws_a"}

    async def drain_auxiliary():
        if loop._background_tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(loop._background_tasks)), timeout=10)

    for index in range(1, 4):
        response = await client.post("/api/agent/session/session_a_1/message", headers=headers,
            json={"text": f"history turn {index}: read the archive", "agent": "trace_archive"})
        assert response.status_code == 200, response.text
        await drain_auxiliary()
    assert ordinary_calls == {1: 2, 2: 2, 3: 2}
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        before_seq = trajectory.committed_seq
        before_state = await state_at(db, trajectory)
        outputs = (await db.scalars(select(TrajectoryEvent).where(
            TrajectoryEvent.type == "tool.output").order_by(TrajectoryEvent.seq))).all()
        assert len(outputs) == 3
        raw_outputs = {event.call_id: event.data["output"] for event in outputs}
        assert list(raw_outputs.values()) == list(archives.values())
        tool_parts = (await db.scalars(select(Part).where(Part.type == "tool")
                                      .order_by(Part.created_at, Part.id))).all()
        assert len(tool_parts) == 3
        assert all("truncated" in part.data["output"] for part in tool_parts)
        assert all("999:" not in part.data["output"] for part in tool_parts)
        original_part_ids = {part.id for part in tool_parts}

    response = await client.post("/api/agent/session/session_a_1/summarize", headers=headers)
    assert response.status_code == 200, response.text
    await drain_auxiliary()
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        starts = [event for event in events if event.type == "compaction.started"]
        assert len(starts) == 1
        # This is the exact old context before prune, including all tool Parts.
        assert all(f"ORIGINAL-ARCHIVE-{index}-0:" in json.dumps(starts[0].data["input"])
                   for index in range(1, 4))
        requests = [event for event in events if event.type == "request.prepared"]
        chunk_requests = [event for event in requests if event.data["purpose"] == "compaction_chunk"]
        summary_requests = [event for event in requests if event.data["purpose"] == "compaction"]
        assert len(chunk_requests) >= 2 and len(summary_requests) == 1
        assert all(event.data["capture_level"] == "adapter_input" for event in chunk_requests + summary_requests)
        assert "[Old tool result content cleared]" in json.dumps([event.data for event in chunk_requests])
        assert "Captured chunk summary." in json.dumps(summary_requests[0].data["input"])
        assert len({event.request_id for event in requests}) == len(requests)
        replacements = [event for event in events if event.type == "context.replaced"]
        assert len(replacements) == 1
        replacement = replacements[0]
        summary_id = replacement.data["summary_message_id"]
        assert replacement.data["applied"] is True
        assert replacement.request_id == summary_requests[0].request_id
        summary = await db.get(Message, summary_id)
        assert summary.summary and summary.finish == "stop"
        assert summary.parent_id == replacement.data["boundary_message_id"]
        terminal = next(event for event in events if event.type == "compaction.finished")
        assert terminal.data["status"] == "completed" and terminal.data["applied"] is True
        assert terminal.data["summary_message_id"] == summary_id
        assert terminal.data["request_id"] == summary_requests[0].request_id
        pruned = [event for event in events if event.type == "part.committed"
            and event.data.get("part", {}).get("id") in original_part_ids
            and (event.data["part"].get("state") or {}).get("time", {}).get("compacted")]
        assert len({event.data["part"]["id"] for event in pruned}) == 2
        trajectory = await db.scalar(select(SessionTrajectory))
        after_state = await state_at(db, trajectory)
        replay_before = await state_at(db, trajectory, before_seq)
        assert replay_before == before_state
        for call_id, output in raw_outputs.items():
            assert after_state["records"]["tool:" + call_id]["data"]["output"] == output
            assert replay_before["records"]["tool:" + call_id]["data"]["output"] == output
        assert "PRESERVED-SUMMARY" not in json.dumps(replay_before)
        assert "PRESERVED-SUMMARY" in json.dumps(after_state)

    effective = await filter_compacted(await get_messages("session_a_1", user_id="a"))
    assert [message.id for message in effective] == [replacement.data["boundary_message_id"], summary_id]
    response = await client.post("/api/agent/session/session_a_1/message", headers=headers,
        json={"text": "continue after compaction", "agent": "trace_archive"})
    assert response.status_code == 200, response.text
    await drain_auxiliary()
    async with factory() as db:
        following = (await db.scalars(select(TrajectoryEvent).where(
            TrajectoryEvent.seq > replacement.seq, TrajectoryEvent.type == "request.prepared")
            .order_by(TrajectoryEvent.seq))).all()
        resumed = next(event for event in following if event.data["purpose"] == "chat")
        new_context = json.dumps(resumed.data["input"]["messages"])
        assert "PRESERVED-SUMMARY" in new_context and "continue after compaction" in new_context
        assert "ORIGINAL-ARCHIVE" not in new_context and "history turn" not in new_context
        assert resumed.context["turn_id"] != replacement.context["turn_id"]
        assert resumed.context["run_id"] != replacement.context["run_id"]
        assert resumed.session_id == replacement.session_id == "session_a_1"
