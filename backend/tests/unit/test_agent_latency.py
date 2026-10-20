"""Model wait excludes slow consumers and counts overlapping calls only once."""
import asyncio

import pytest

from agent import latency
from agent.trajectory import RequestCapture


class Clock:
    now = 0.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("context", [None, "recording"])
async def test_stream_wait_excludes_sql_consumer_time(context, monkeypatch):
    clock = Clock()
    timing = latency.RunTiming(clock)
    token = latency.current.set(timing)
    if context:
        from trajectory import TraceContext
        context = TraceContext("u", "s", request_id="r")
        monkeypatch.setattr("trajectory.record_stream", lambda *a, **kw: None)
    capture = RequestCapture(context, None, "chat", "model", "adapter_input")
    async def stream():
        clock.advance(3)
        yield "one"
        clock.advance(5)
        yield "two"
    try:
        async for _ in capture.stream_chunks(stream(), lambda _: []):
            clock.advance(7)  # app persistence, not the model
        assert timing.snapshot() == {"total_ms": 22000, "provider_wait_ms": 8000, "application_ms": 14000}
    finally:
        latency.current.reset(token)


def test_parallel_waits_form_a_union_and_retry_backoff_stays_application_time():
    clock, timing = Clock(), None
    timing = latency.RunTiming(clock)
    with timing.provider_wait():
        clock.advance(2)
        with timing.provider_wait():
            clock.advance(4)
        clock.advance(2)
    clock.advance(3)  # retry backoff is controllable application waiting
    with timing.provider_wait():
        clock.advance(5)
    assert timing.snapshot() == {"total_ms": 16000, "provider_wait_ms": 13000, "application_ms": 3000}


async def test_failed_and_cancelled_provider_waits_still_close_the_measurement():
    clock = Clock()
    timing = latency.RunTiming(clock)
    for exception in (TimeoutError, asyncio.CancelledError):
        with pytest.raises(exception), timing.provider_wait():
            clock.advance(60)
            raise exception()
        assert timing.active == 0
    clock.advance(1)
    assert timing.snapshot()["application_ms"] == 1000


async def test_http_enter_exit_preserve_errors_and_exclude_consumer_work():
    clock, errors = Clock(), []
    timing = latency.RunTiming(clock)
    token = latency.current.set(timing)
    class Manager:
        async def __aenter__(self):
            clock.advance(2)
            return "response"
        async def __aexit__(self, *args):
            errors.append(args[0])
            clock.advance(1)
    try:
        with pytest.raises(ValueError):
            async with latency.ProviderStream(Manager()) as response:
                assert response == "response"
                clock.advance(4)
                raise ValueError("local parsing")
        assert errors == [ValueError]
        assert timing.snapshot() == {"total_ms": 7000, "provider_wait_ms": 3000, "application_ms": 4000}
    finally:
        latency.current.reset(token)


def test_phases_reconcile_and_network_model_overlap_is_subtracted_once():
    clock = Clock()
    timing = latency.RunTiming(clock)
    clock.advance(1)
    timing.switch_phase("runtime")
    with timing.network_wait():
        clock.advance(2)
        with timing.provider_wait():
            clock.advance(3)
            timing.switch_phase("model")
            clock.advance(4)
        clock.advance(2)
    clock.advance(5)
    assert timing.snapshot()["total_ms"] == 17000
    assert timing.network_snapshot() == {
        "runtime_network_wait_ms": 11000, "external_wait_ms": 11000, "internal_ms": 6000}
    phases = timing.breakdown()
    assert sum(p["total_ms"] for p in phases.values()) == 17000
    assert sum(p["internal_ms"] for p in phases.values()) == 6000
    assert phases["runtime"]["internal_ms"] == 0
    assert phases["model"]["internal_ms"] == 5000
