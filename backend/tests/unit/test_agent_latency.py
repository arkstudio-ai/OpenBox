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


async def test_auxiliary_title_wait_does_not_subtract_foreground_work():
    from question.runtime import RunTicket, run_auxiliary, auxiliary_run
    clock = Clock()
    timing = latency.RunTiming(clock)
    token = latency.current.set(timing)
    started, finished = asyncio.Event(), asyncio.Event()
    async def title():
        assert auxiliary_run.get().purpose == "title"
        with latency.provider_wait():
            started.set()
            await finished.wait()
    task = asyncio.create_task(run_auxiliary(RunTicket("s", "u", 1, "run"), "title", title()))
    try:
        await started.wait()
        clock.advance(3)  # Foreground SQL while the title waits elsewhere.
        assert timing.network_snapshot()["internal_ms"] == 3000
        assert timing.snapshot()["provider_wait_ms"] == 0
        finished.set()
        await task
        assert latency.current.get() is timing
    finally:
        finished.set()
        await task
        latency.current.reset(token)


async def test_sql_observer_counts_success_and_failure_without_query_content():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    latency.attach_sql_timing(engine)
    timing = latency.RunTiming()
    timing.switch_phase("request_prepare")
    token = latency.current.set(timing)
    try:
        async with engine.connect() as db:
            await db.execute(text("select :private_value"), {"private_value": "private-sentinel"})
            with pytest.raises(Exception):
                await db.execute(text("select * from missing_private_table"))
        assert timing.sql["request_prepare"]["count"] == 2
        assert timing.sql["request_prepare"]["total_ms"] >= timing.sql["request_prepare"]["max_ms"] > 0
        assert "private" not in str(timing.sql)
    finally:
        latency.current.reset(token)
        await engine.dispose()


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


async def test_early_recall_counts_only_transport_while_run_awaits_it():
    clock = Clock()
    seed = latency.current.set(latency.RunTiming(clock))
    ready, release = asyncio.Event(), asyncio.Event()

    async def recall():
        with latency.provider_wait():
            ready.set()
            await release.wait()
            clock.advance(3)
        with latency.network_wait():
            clock.advance(2)
        clock.advance(4)  # SQL / formatting must remain internal.
        return "packet"

    try:
        task = latency.create_dependency_task(recall())
        await ready.wait()
        clock.advance(2)  # IO before the run even exists.
        run = latency.RunTiming(clock)
        token = latency.current.set(run)
        try:
            clock.advance(7)  # Run setup overlaps the speculative request.
            run.switch_phase("context")
            release.set()
            assert await latency.await_dependency(task) == "packet"
            assert run.snapshot() == {"total_ms": 16000, "provider_wait_ms": 3000, "application_ms": 13000}
            assert run.network_snapshot() == {
                "runtime_network_wait_ms": 2000, "external_wait_ms": 5000, "internal_ms": 11000}
            assert run.breakdown()["setup"]["internal_ms"] == 7000
            assert run.breakdown()["context"]["internal_ms"] == 4000
            assert run.spans["memory.prefetch_wait"]["internal_ms"] == 4000
        finally:
            latency.current.reset(token)
    finally:
        latency.current.reset(seed)


async def test_completed_or_unused_recall_does_not_subtract_its_io_from_run():
    clock = Clock()
    run = latency.RunTiming(clock)
    token = latency.current.set(run)

    async def recall():
        with latency.provider_wait():
            clock.advance(3)
        return "already ready"

    try:
        task = latency.create_dependency_task(recall())
        await asyncio.sleep(0)
        assert task.done() and run.active == 0 and run.provider_seconds == 0
        before = run.snapshot()
        assert await latency.await_dependency(task) == "already ready"
        assert run.snapshot() == before
    finally:
        latency.current.reset(token)


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_dependency_error_clears_overlapping_transport_and_observers(failure):
    clock = Clock()
    run = latency.RunTiming(clock)
    token = latency.current.set(run)

    async def recall():
        clock.advance(1)
        with latency.network_wait():
            clock.advance(2)
            with latency.provider_wait():
                clock.advance(3)
                raise failure()

    try:
        task = latency.create_dependency_task(recall())
        with pytest.raises(failure):
            await latency.await_dependency(task)
        child = latency._dependencies[task]
        assert child.wait_observers == []
        assert run.active == run.network_active == run.external_active == 0
        assert run.network_snapshot() == {
            "runtime_network_wait_ms": 5000, "external_wait_ms": 5000, "internal_ms": 1000}
        assert run.snapshot()["provider_wait_ms"] == 3000
    finally:
        latency.current.reset(token)
