"""Actual Responses and title entry points with durable execution identities."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from agent import llm, loop
from core.config import OpenBoxConfig
from db.models.billing import UsageEvent
from db.models.session import Session
from db.models.trajectory import SessionTrajectory, TrajectoryEvent
from question import runtime
from session.session import create_assistant_message, create_user_message, get_messages
from tests.integration.test_trajectory_agent_loop import Chunk
from tests.integration.test_trajectory_storage import tracedb
from tests.unit.test_llm_tool_search_responses import (
    _FakeAsyncClient, _FakeResponse, _budget, _deferred_definition, _native_plan, _tool,
)
from tool.tool import ToolContext
from trajectory import bind
from trajectory.repository import state_at


async def start_recorded_turn():
    message = await create_user_message("session_a_1", "Read the requested document", user_id="a")
    ticket = await runtime.start_run("session_a_1", "a")
    trace = (await runtime.get_run_trace(ticket)).derive(step_id="responses_step")
    with bind(trace):
        assistant = await create_assistant_message("session_a_1", message.id,
                                                   model_id="openai/gpt-5.4", user_id="a")
    return ticket, message, ToolContext(user_id="a", session_id="session_a_1",
        workspace_id="ws_a", message_id=assistant.id, trace_context=trace)


@pytest.mark.parametrize("fallback", [False, True], ids=["native", "fallback"])
async def test_responses_native_and_fallback_persist_actual_dispatches(tracedb, monkeypatch, fallback):
    import litellm

    factory, _ = tracedb
    config = OpenBoxConfig.model_validate({
        "model": "openai/gpt-5.4",
        "provider": {"openai": {"api_key": "private-responses-key", "base_url": "https://api.openai.com/v1"}},
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setenv("TRAJECTORY_BATCH_MS", "1")
    monkeypatch.setenv("TRAJECTORY_INLINE_BYTES", "500000")
    monkeypatch.setenv("BILLING_MODE", "off")
    monkeypatch.setattr(litellm, "completion_cost", lambda **_kwargs: 0.0)
    ticket, message, ctx = await start_recorded_turn()
    original_trace = ctx.trace_context
    _, native_plan = _native_plan(deferred=("read",))
    native_output = [
        {"id": "search_1", "type": "tool_search_call", "execution": "server",
         "status": "completed", "arguments": {"query": "read"}},
        {"id": "search_output_1", "type": "tool_search_output", "execution": "server",
         "status": "completed", "tools": [_deferred_definition(native_plan, "read")]},
        {"id": "function_1", "type": "function_call", "call_id": "read_call",
         "name": "read", "arguments": '{"marker":"observed"}'},
    ]
    output = ([{"id": "answer_1", "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Portable response"}]}]
              if fallback else native_output)
    raw_chunks = [
        {"type": "response.created", "response": {"id": "response_1"}},
        {"type": "response.completed", "response": {"id": "response_1", "output": output,
         "usage": {"input_tokens": 21, "output_tokens": 4, "total_tokens": 25}}},
    ]
    responses = ([_FakeResponse(400, body=b"defer_loading is not supported for this model")]
                 if fallback else [])
    responses.append(_FakeResponse(200, lines=[
        *("data: " + json.dumps(chunk) for chunk in raw_chunks), "data: [DONE]",
    ]))
    wire = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _FakeAsyncClient(responses, wire))
    try:
        with bind(original_trace):
            events = [event async for event in llm._stream_responses_api(
                "openai/gpt-5.4", ["native instructions"],
                [{"role": "user", "content": "read"}], {"bash": _tool("bash")},
                native_plan=native_plan, native_discovery_state=_budget(),
                native_portable_tools={"bash": _tool("bash"), "capability_search": _tool("capability_search")},
                native_portable_system=["portable instructions"], trace_ctx=ctx,
            )]
        assert not [event for event in events if event["type"] == "error"]
    finally:
        await runtime.finish_run(ticket, completed=True)

    count = 2 if fallback else 1
    assert len(wire) == count and not responses
    assert events[-1]["type"] == "finish" and events[-1]["usage"]["total"] == 25
    if not fallback:
        assert [event["type"] for event in events if event["type"].startswith("native_")] == [
            "native_search_started", "native_search_result", "native_tool_revealed"]
        call = next(event for event in events if event["type"] == "tool_call")
        assert call["call_id"] == "read_call" and call["args"] == {"marker": "observed"}

    async with factory() as db:
        rows = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        prepared = [row for row in rows if row.type == "request.prepared"]
        started = [row for row in rows if row.type == "request.started"]
        finished = [row for row in rows if row.type == "request.finished"]
        assert len(prepared) == len(started) == len(finished) == count
        assert len({row.request_id for row in prepared}) == count
        assert [row.request_id for row in started] == [row.request_id for row in prepared]
        assert [row.request_id for row in finished] == [row.request_id for row in prepared]
        for index, row in enumerate(prepared):
            assert row.data["capture_level"] == "provider_wire"
            assert row.data["sdk_internal_attempts"] == "not_observed"
            assert row.data["input"] == wire[index][2]["json"]
            assert row.seq < started[index].seq < finished[index].seq
        request_rows = [row for row in rows if row.type.startswith("request.")]
        assert {row.session_id for row in request_rows} == {"session_a_1"}
        assert {row.source_session_id for row in request_rows} == {"session_a_1"}
        assert {row.user_id for row in request_rows} == {"a"}
        assert {row.context["turn_id"] for row in request_rows} == {message.id}
        assert {row.context["run_id"] for row in request_rows} == {ticket.run_id}
        assert {row.context["generation"] for row in request_rows} == {ticket.generation}
        assert {row.context["step_id"] for row in request_rows} == {original_trace.step_id}
        assert {row.context["message_id"] for row in request_rows} == {ctx.message_id}
        deltas = [row for row in rows if row.type == "request.delta"]
        assert len(deltas) == len(raw_chunks)
        def resolved_raw(value, blocks):
            if isinstance(value, dict):
                if "$stream_blocks" in value:
                    assert value["availability"] == "sanitized_reference"
                    assert value["$stream_blocks"], "A recorded body cannot reference no visible content"
                    return "".join(blocks[block_id] for block_id in value["$stream_blocks"])
                return {key: resolved_raw(item, blocks) for key, item in value.items()}
            if isinstance(value, list):
                return [resolved_raw(item, blocks) for item in value]
            return value
        observed_chunks = [resolved_raw(row.data["raw"], {
            block["block_id"]: block["delta"] for block in row.data["blocks"]}) for row in deltas]
        expected_chunks = copy.deepcopy(raw_chunks)
        if not fallback:
            # Redaction may reserialize a complete JSON-valued argument string;
            # compare its parsed contents, plus every other actual response field.
            observed_args = observed_chunks[1]["response"]["output"][2].pop("arguments")
            expected_args = expected_chunks[1]["response"]["output"][2].pop("arguments")
            assert json.loads(observed_args) == json.loads(expected_args)
        assert observed_chunks == expected_chunks
        assert [row.data["chunk_index"] for row in deltas] == [1, 2]
        assert {row.request_id for row in deltas} == {prepared[-1].request_id}
        assert finished[-1].data["status"] == "completed"
        assert finished[-1].data["usage"]["total"] == 25
        assert finished[-1].data["chunk_count"] == 2
        if fallback:
            assert finished[0].data["status"] == "failed"
            assert finished[0].data["finish_reason"] == "http_400"
            assert finished[0].data["chunk_count"] == 0
            assert prepared[1].data["previous_request_id"] == prepared[0].request_id
            route = next(row for row in rows if row.type == "request.route_changed")
            assert route.request_id == prepared[0].request_id
            assert finished[0].seq < route.seq < prepared[1].seq
            assert any(tool.get("type") == "tool_search" for tool in prepared[0].data["input"]["tools"])
            assert not any(tool.get("type") == "tool_search" for tool in prepared[1].data["input"]["tools"])
        else:
            assert not [row for row in rows if row.type == "request.route_changed"]
        trajectory = await db.scalar(select(SessionTrajectory))
        state = await state_at(db, trajectory)
        for row in finished:
            assert state["records"]["request:" + row.request_id]["status"] == row.data["status"]
        assert "private-responses-key" not in json.dumps([row.data for row in rows])


async def test_title_entry_after_run_records_original_identity_and_existing_bill_once(tracedb, monkeypatch):
    factory, _ = tracedb
    model = "openai/trajectory-title-test"
    config = OpenBoxConfig.model_validate({
        "model": "openai/unused-chat-model", "mcp_filter_model": model,
        "provider": {"openai": {"api_key": "private-title-key", "base_url": "https://provider.invalid"}},
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setenv("BILLING_MODE", "shadow")
    monkeypatch.setenv("TRAJECTORY_INLINE_BYTES", "500000")
    async with factory.begin() as db:
        (await db.get(Session, "session_a_1")).title = ""
    message = await create_user_message("session_a_1", "Design an observability page", user_id="a")
    ticket = await runtime.start_run("session_a_1", "a")
    trace = await runtime.get_run_trace(ticket)
    await runtime.finish_run(ticket, completed=True)
    user_message = next(item for item in await get_messages("session_a_1", user_id="a") if item.id == message.id)
    response = Chunk(choices=[SimpleNamespace(index=0, finish_reason="stop",
        message=SimpleNamespace(content='"Session trajectory monitoring"'))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3, total_tokens=14))
    completion = AsyncMock(return_value=response)
    monkeypatch.setattr("litellm.acompletion", completion)
    with bind(trace):
        await loop._ensure_title("session_a_1", user_message, user_id="a")
    completion.assert_awaited_once()
    assert completion.await_args.kwargs["model"] == model
    assert "Design an observability page" in completion.await_args.kwargs["messages"][0]["content"]
    async with factory() as db:
        assert (await db.get(Session, "session_a_1")).title == "Session trajectory monitoring"
        rows = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        prepared = [row for row in rows if row.type == "request.prepared"]
        assert len(prepared) == 1
        request = prepared[0]
        assert request.data["purpose"] == "title" and request.data["capture_level"] == "adapter_input"
        assert request.data["input"]["model"] == model
        assert request.data["input"]["messages"] == completion.await_args.kwargs["messages"]
        requests = [row for row in rows if row.type.startswith("request.")]
        assert {row.request_id for row in requests} == {request.request_id}
        assert {row.user_id for row in requests} == {"a"}
        assert {row.session_id for row in requests} == {"session_a_1"}
        assert {row.source_session_id for row in requests} == {"session_a_1"}
        assert {row.context["turn_id"] for row in requests} == {message.id}
        assert {row.context["run_id"] for row in requests} == {ticket.run_id}
        assert {row.context["generation"] for row in requests} == {ticket.generation}
        terminal = [row for row in requests if row.type == "request.finished"]
        assert len(terminal) == 1 and terminal[0].data["status"] == "completed"
        assert len([row for row in requests if row.type == "request.delta"]) == 1
        assert next(row.seq for row in rows if row.type == "run.finished") < request.seq
        bills = (await db.scalars(select(UsageEvent))).all()
        settlements = [row for row in requests if row.type == "request.usage"
                       and row.data.get("source") == "existing_billing_ledger"]
        assert len(bills) == len(settlements) == 1
        bill = bills[0]
        assert (bill.user_id, bill.workspace_id, bill.session_id, bill.kind, bill.total_tokens) == (
            "a", "ws_a", "session_a_1", "title", 14)
        assert bill.status != "pending"
        assert request.data["billing_usage_event_id"] == bill.id
        assert settlements[0].data["billing_usage_event_id"] == bill.id
        assert settlements[0].data["usage"]["total"] == 14
        assert "private-title-key" not in json.dumps([row.data for row in rows])
