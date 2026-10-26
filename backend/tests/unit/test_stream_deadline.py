"""Provider silence, cancellation and progress have distinct budgets."""
import asyncio

import pytest

from agent.retry import is_retryable
from agent.stream_deadline import ModelResponseDeadline, StreamLimits, bounded_stream, limits_for
from core.config import OpenBoxConfig


async def test_empty_chunks_and_usage_cannot_keep_a_silent_request_alive():
    closed = asyncio.Event()
    async def source():
        try:
            while True:
                await asyncio.sleep(.005)
                yield {"type": "text_delta", "text": ""}
                yield {"type": "usage", "usage": {"input_tokens": 3}}
        finally:
            closed.set()
    with pytest.raises(ModelResponseDeadline) as error:
        async for _ in bounded_stream(source(), StreamLimits(.03, .03, .2)):
            pass
    assert error.value.phase == "first_progress" and closed.is_set()
    assert is_retryable(error.value) is None


async def test_progress_is_not_an_unlimited_total_budget():
    async def source():
        while True:
            await asyncio.sleep(.005)
            yield {"type": "reasoning_delta", "text": "thinking"}
    with pytest.raises(ModelResponseDeadline) as error:
        async for _ in bounded_stream(source(), StreamLimits(.05, .05, .03)):
            pass
    assert error.value.phase == "total"


async def test_consumer_database_and_tool_time_does_not_spend_provider_budget():
    async def source():
        yield {"type": "text_delta", "text": "ok"}
        yield {"type": "finish", "reason": "stop"}
    received = []
    async for event in bounded_stream(source(), StreamLimits(.02, .02, .02)):
        received.append(event)
        await asyncio.sleep(.04)
    assert received[-1]["type"] == "finish"


async def test_abort_closes_a_silent_origin_stream():
    entered, closed = asyncio.Event(), asyncio.Event()
    async def source():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield {}
        finally:
            closed.set()
    stream = bounded_stream(source(), StreamLimits())
    task = asyncio.create_task(anext(stream))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_provider_own_timeout_preserves_transport_classification():
    async def source():
        raise TimeoutError("provider")
        yield {}
    with pytest.raises(TimeoutError):
        await anext(bounded_stream(source(), StreamLimits()))


def test_per_model_limits_and_reasoning_defaults(monkeypatch):
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(models=[{
        "id": "openai/custom", "first_progress_timeout_seconds": 120, "response_timeout_seconds": 900}]))
    assert limits_for("openai/custom", None, "chat") == StreamLimits(120, 60, 900)
    assert limits_for("openai/other", None, "chat") == StreamLimits()
    assert limits_for("openai/other", "high", "chat").first_progress == 90
    assert limits_for("openai/other", None, "compaction").total == 600
