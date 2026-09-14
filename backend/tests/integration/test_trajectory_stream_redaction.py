"""Fragmented credentials never reach events, retained payloads or exports."""
import copy
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent.trajectory import RequestCapture, litellm_chunk_blocks, responses_chunk_blocks
from db.models.trajectory import SessionTrajectory, TrajectoryEvent, TrajectoryExport, TrajectoryPayload, TrajectoryRecord
from tests.integration.test_trajectory_agent_loop import Chunk
from tests.integration.test_trajectory_responses_title import start_recorded_turn
from tests.integration.test_trajectory_storage import tracedb
from tool.tool import ToolResult
from trajectory.export import build_export, create_export
from trajectory.payload import expand, read_payload
from trajectory.repository import state_at


async def assert_retained_content_has_no_fragments(factory, blob, forbidden):
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        encoded = json.dumps([event.data for event in events])
        expanded = [await expand(db, trajectory.id, event.data, through_seq=trajectory.committed_seq)
                    for event in events]
        encoded += json.dumps(expanded)
        records = (await db.scalars(select(TrajectoryRecord))).all()
        encoded += json.dumps([(row.data, row.summary, row.search_text) for row in records])
        for event in events:
            encoded += json.dumps(await state_at(db, trajectory, event.seq))
        for payload in (await db.scalars(select(TrajectoryPayload))).all():
            _, content = await read_payload(db, trajectory.id, payload.payload_id,
                                            through_seq=trajectory.committed_seq)
            for fragment in forbidden:
                assert fragment.encode() not in content
        for fragment in forbidden:
            assert fragment not in encoded
    for content in blob.objects.values():
        for fragment in forbidden:
            assert fragment.encode() not in content
    return events, expanded


async def assert_export_has_no_fragments(factory, blob, forbidden):
    async with factory.begin() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        export = await create_export(db, trajectory, "admin", trajectory.committed_seq)
    await build_export(export.id)
    async with factory() as db:
        saved = await db.get(TrajectoryExport, export.id)
        assert saved.status == "completed", saved.error
        content = blob.objects[saved.storage_key]
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in archive.namelist():
            for fragment in forbidden:
                assert fragment.encode() not in archive.read(name)


def public_chunk(surface, fragment, index):
    if surface.startswith("responses"):
        kind = ("response.function_call_arguments.delta" if surface.endswith("arguments")
                else "response.output_text.delta")
        return {"type": kind, "item_id": "stream_item", "delta": fragment}
    tool_calls = []
    content = reasoning = None
    if surface == "litellm_arguments":
        tool_calls = [SimpleNamespace(index=0, id="private_call", function=SimpleNamespace(name="example", arguments=fragment)),
                      SimpleNamespace(index=1, id="public_call", function=SimpleNamespace(name="example", arguments=str(index)))]
    elif surface == "litellm_text":
        content = fragment
    else:
        reasoning = fragment
    return Chunk(choices=[SimpleNamespace(index=0, delta=SimpleNamespace(content=content,
        reasoning_content=reasoning, tool_calls=tool_calls))])


@pytest.mark.parametrize("external_payload", [False, True], ids=["inline", "payload"])
@pytest.mark.parametrize("surface", ["litellm_arguments", "litellm_text", "litellm_reasoning",
                                     "responses_arguments", "responses_text"])
async def test_fragmented_request_credentials_never_enter_retained_views(
        tracedb, monkeypatch, surface, external_payload):
    from question import runtime

    factory, blob = tracedb
    monkeypatch.setenv("BILLING_MODE", "off")
    monkeypatch.setenv("TRAJECTORY_BATCH_MS", "1")
    monkeypatch.setenv("TRAJECTORY_INLINE_BYTES", "100" if external_payload else "1000000")
    ticket, _, ctx = await start_recorded_turn()
    if surface.endswith("arguments"):
        forbidden = ["fixture-sensitive-fragment"]
        fragments = ['{"api_', 'key":"', forbidden[0], '"}']
    elif surface == "litellm_text":
        forbidden = ["Q7vXfragment", "W9zYfragment"]
        fragments = ["Authorization: Bea", "rer ", forbidden[0], forbidden[1], "\nVisible suffix"]
    else:
        forbidden = ["Q7vXfragment", "W9zYfragment"]
        fragments = ["Visible prefix ", "s", "k", "-", forbidden[0], forbidden[1], "\nVisible suffix"]
    capture = await RequestCapture.start(copy.copy(ctx), purpose="chat", model_id="fixture/model",
        payload={"model": "fixture/model", "messages": [{"role": "user", "content": "fixture"}]},
        capture_level="provider_wire" if surface.startswith("responses") else "adapter_input")
    chunks = [public_chunk(surface, fragment, index) for index, fragment in enumerate(fragments)]
    before = [chunk.model_dump() if isinstance(chunk, Chunk) else copy.deepcopy(chunk) for chunk in chunks]

    async def stream():
        for chunk in chunks:
            yield chunk
    builder = responses_chunk_blocks if surface.startswith("responses") else litellm_chunk_blocks
    try:
        delivered = []
        async for chunk in capture.stream_chunks(stream(), builder):
            delivered.append(chunk)
        await capture.finish("completed")
    finally:
        await runtime.finish_run(ticket, completed=True)
    assert delivered == chunks
    assert before == [chunk.model_dump() if isinstance(chunk, Chunk) else chunk for chunk in chunks]
    events, expanded = await assert_retained_content_has_no_fragments(factory, blob, forbidden)
    deltas = [(row, data) for row, data in zip(events, expanded) if row.type == "request.delta"
              and data.get("redaction_control") != "finalize"]
    assert len(deltas) == len(fragments)
    assert [data["chunk_index"] for _, data in deltas] == list(range(1, len(fragments) + 1))
    assert any("[REDACTED]" in json.dumps(data) for _, data in deltas)
    assert any("$payload" in row.data for row, _ in deltas) is external_payload
    terminal = next(data for row, data in zip(events, expanded) if row.type == "request.finished")
    assert terminal["chunk_count"] == len(fragments)
    for row, data in zip(events, expanded):
        if data.get("redaction_control") == "finalize":
            assert data["source"] == "recorder_redaction"
            assert data["observed_chunk_count"] == len(fragments)
    if surface == "litellm_arguments":
        # Sibling tool argument streams must not inherit the private call's state.
        assert [next(block["delta"] for block in data["blocks"] if block["block_id"] == "tool:1")
                for _, data in deltas] == [str(index) for index in range(len(fragments))]
    await assert_export_has_no_fragments(factory, blob, forbidden)


async def test_tool_cumulative_output_redaction_resets_for_each_call(tracedb, monkeypatch):
    from agent.hooks import ToolHooks
    from question import runtime

    factory, blob = tracedb
    monkeypatch.setenv("BILLING_MODE", "off")
    monkeypatch.setenv("TRAJECTORY_INLINE_BYTES", "100")
    ticket, _, ctx = await start_recorded_turn()
    hooks = ToolHooks("session_a_1", "a")

    async def allow(*_args):
        return None
    hooks.authorize_tool = allow
    forbidden = ["Q7vXfragment", "W9zYfragment"]
    values = ["sk-", "sk-" + forbidden[0], "sk-" + "".join(forbidden)]
    original_outputs = []

    async def first(_args, context):
        for value in values:
            await context.update_output(value)
            original_outputs.append(value)
        return ToolResult(output=values[-1])

    async def second(_args, context):
        await context.update_output("Visible second call")
        return ToolResult(output="Visible second call")
    try:
        await hooks.wrap_execute("fixture", first, {}, ctx, part_id="private_output_call")
        await hooks.wrap_execute("fixture", second, {}, ctx, part_id="public_output_call")
    finally:
        await runtime.finish_run(ticket, completed=True)
    assert original_outputs == values
    events, expanded = await assert_retained_content_has_no_fragments(factory, blob, forbidden)
    second_outputs = [data["output"] for row, data in zip(events, expanded)
                      if row.call_id == "public_output_call" and row.type == "tool.output"]
    assert second_outputs == ["Visible second call", "Visible second call"]
    await assert_export_has_no_fragments(factory, blob, forbidden)


async def test_redaction_state_is_not_shared_between_requests(tracedb, monkeypatch):
    from question import runtime

    factory, blob = tracedb
    monkeypatch.setenv("BILLING_MODE", "off")
    ticket, _, ctx = await start_recorded_turn()
    captures = [await RequestCapture.start(copy.copy(ctx), purpose="chat", model_id="fixture/model",
        payload={"model": "fixture/model"}, capture_level="provider_wire") for _ in range(2)]
    left, right = captures
    secret = "Q7vXfragmentW9zYfragment"
    try:
        for capture, value in ((left, "Authorization: Bearer "),
                               (right, "Visible separate request"), (left, secret)):
            raw = {"type": "response.output_text.delta", "item_id": "same_item_id", "delta": value}
            await capture.chunk(raw, blocks=responses_chunk_blocks(raw))
        for capture in captures:
            await capture.finish("completed")
    finally:
        await runtime.finish_run(ticket, completed=True)
    await assert_retained_content_has_no_fragments(factory, blob, [secret])
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        state = await state_at(db, trajectory)
        assert left.context.request_id != right.context.request_id
        record = state["records"]["assistant:" + right.context.request_id]
        assert "".join(block["text"] for block in record["blocks"]) == "Visible separate request"


async def test_sensitive_schema_properties_remain_the_same_request_and_tool_contract(tracedb, monkeypatch):
    from agent.hooks import ToolHooks
    from pydantic import BaseModel, Field
    from question import runtime
    from tool.tool import define_tool

    factory, _ = tracedb
    monkeypatch.setenv("BILLING_MODE", "off")
    monkeypatch.setenv("TRAJECTORY_INLINE_BYTES", "1000000")
    ticket, _, ctx = await start_recorded_turn()

    class Arguments(BaseModel):
        password: str = Field(min_length=3, description="The requested password",
                              examples=["private-example-value"])
        api_key: str = Field(default="private-default-value", min_length=2)

    executed = []
    async def execute(args, _ctx):
        executed.append(args.password)
        return ToolResult(output="Accepted")
    tool = define_tool("credential_fixture", description="Credential field contract", parameters=Arguments,
                       execute=execute, sandbox_required=False)
    schema = {"type": "function", "function": {"name": tool.id, "description": tool.description,
                                                "parameters": Arguments.model_json_schema()}}
    body = {"model": "fixture/model", "tools": [schema],
            "_hidden_params": {"opaque": "private-sdk-state"}, "api_key": "private-provider-value"}
    original = copy.deepcopy(body)
    capture = await RequestCapture.start(ctx, purpose="chat", model_id="fixture/model",
                                         payload=body, capture_level="adapter_input")
    await capture.finish("completed")
    hooks = ToolHooks("session_a_1", "a")

    async def allow(*_args):
        return None
    hooks.authorize_tool = allow
    try:
        await hooks.wrap_execute(tool.id, tool.execute, {"password": "private-input-value"}, ctx,
                                 part_id="credential_call", tool_info=tool)
    finally:
        await runtime.finish_run(ticket, completed=True)
    assert body == original and executed == ["private-input-value"]
    async with factory() as db:
        rows = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        prepared = next(row for row in rows if row.type == "request.prepared")
        requested = next(row for row in rows if row.type == "tool.requested")
        retained = prepared.data["input"]["tools"][0]
        assert requested.data["schema_source"] == "provider_request"
        assert requested.data["schema"] == retained
        parameters = retained["function"]["parameters"]
        assert parameters["required"] == ["password"]
        assert parameters["properties"]["password"]["type"] == "string"
        assert parameters["properties"]["password"]["minLength"] == 3
        assert parameters["properties"]["password"]["description"] == "The requested password"
        assert parameters["properties"]["password"]["examples"] == "[REDACTED]"
        assert parameters["properties"]["api_key"]["minLength"] == 2
        assert parameters["properties"]["api_key"]["default"] == "[REDACTED]"
        encoded = json.dumps([row.data for row in rows])
        for value in ("private-example-value", "private-default-value", "private-sdk-state",
                      "private-provider-value", "private-input-value"):
            assert value not in encoded
