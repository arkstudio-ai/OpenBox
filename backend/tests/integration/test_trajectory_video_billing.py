"""The existing video analysis meter is linked to the observed model request."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from db.models.billing import UsageEvent
from db.models.message import Message
from db.models.trajectory import TrajectoryEvent
from tests.integration.test_trajectory_agent_loop import Chunk
from tests.integration.test_trajectory_storage import tracedb
from tool.tool import ToolContext
from trajectory import TraceContext, bind
from trajectory.types import now


async def test_video_analysis_settlement_uses_existing_bill_once(tracedb, monkeypatch):
    from tool.video_analyze import _complete

    factory, _ = tracedb
    monkeypatch.setenv("BILLING_MODE", "shadow")
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _model: {"api_key": "private-video-key"})
    trace = TraceContext("a", "session_a_1", workspace_id="ws_a", turn_id="video_turn",
        run_id="video_run", agent_id="video_agent", request_id="parent_chat",
        call_id="video_call", message_id="video_message")
    ctx = ToolContext(user_id="a", session_id="session_a_1", workspace_id="ws_a",
        message_id="video_message", part_id="video_part", trace_context=trace)
    async with factory.begin() as db:
        db.add(Message(id=ctx.message_id, session_id=ctx.session_id, user_id="a", role="assistant",
                       summary=False, created_at=now()))
    response = Chunk(choices=[SimpleNamespace(index=0, finish_reason="stop",
        message=SimpleNamespace(content='{"analysis":"captured"}'))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=6, total_tokens=18))
    completion = AsyncMock(return_value=response)
    monkeypatch.setattr("litellm.acompletion", completion)
    with bind(trace):
        result, usage, credits = await _complete(ctx, model="openai/trajectory-test",
            frames=["https://fixture.invalid/frame.jpg"], duration=8, transcript="fixture words", timeout=10)
    completion.assert_awaited_once()
    assert result == '{"analysis":"captured"}' and usage["total"] == 18
    # Auxiliary capture must not replace the parent tool's immutable identity.
    assert ctx.trace_context == trace
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        prepared = next(event for event in events if event.type == "request.prepared")
        assert prepared.request_id != trace.request_id
        assert prepared.context["parent_call_id"] == "video_call"
        assert prepared.context["run_id"] == "video_run"
        assert prepared.data["purpose"] == "video_analyze"
        assert prepared.data["capture_level"] == "adapter_input"
        assert prepared.data["sdk_internal_attempts"] == "not_observed"
        assert prepared.data["input"]["messages"] == completion.await_args.kwargs["messages"]
        bills = (await db.scalars(select(UsageEvent))).all()
        settlements = [event for event in events if event.type == "request.usage"
                       and event.data.get("source") == "existing_billing_ledger"]
        assert len(bills) == len(settlements) == 1
        assert bills[0].kind == "video_analyze" and bills[0].total_tokens == 18
        assert prepared.data["billing_usage_event_id"] == bills[0].id
        assert settlements[0].data["billing_usage_event_id"] == bills[0].id
        assert settlements[0].request_id == prepared.request_id
        assert settlements[0].data["usage"]["total"] == 18
        assert settlements[0].data["usage"]["credits"] == (str(credits) if credits is not None else None)
        assert len([event for event in events if event.type == "request.delta"]) == 1
        assert "private-video-key" not in json.dumps([event.data for event in events])
