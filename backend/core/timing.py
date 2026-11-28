"""The current run's timer: one slot that the database layer and the kernel read.

A run timer is opened by the kernel's timing port (``agent.timing``, implemented
by ``trajectory.latency``) for the duration of a run. Without one the slot stays
empty and every helper here is a no-op. Only provider awaits belong in
``provider_wait``; ``network_wait`` is remote runtime HTTP IO only.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ContextManager, Protocol


class RunTimer(Protocol):
    """Wall-clock attribution of one run (``trajectory.latency.RunTiming``)."""

    phase: str
    run_id: str | None

    def clock(self) -> float: ...
    def provider_wait(self) -> ContextManager: ...
    def network_wait(self) -> ContextManager: ...
    def span(self, name: str) -> ContextManager: ...
    def switch_phase(self, name: str) -> None: ...
    def record_sql(self, phase: str, started: float) -> None: ...
    def forward_waits_to(self, parent: Any) -> ContextManager: ...


current: ContextVar["RunTimer | None"] = ContextVar("run_latency", default=None)


@contextmanager
def provider_wait():
    timing = current.get()
    if timing is None:
        yield
    else:
        with timing.provider_wait():
            yield


@contextmanager
def network_wait():
    """Remote runtime HTTP IO only, excluding authorization and consumers."""
    timing = current.get()
    if timing is None:
        yield
    else:
        with timing.network_wait():
            yield


@contextmanager
def span(name):
    """Measure a bounded block using the same union-of-waits clock as decorators."""
    timing = current.get()
    if timing is None:
        yield
    else:
        with timing.span(name):
            yield


def attach_sql_timing(engine):
    """Observe database waits within a run without changing transaction behavior."""
    from sqlalchemy import event

    def before(conn, cursor, statement, parameters, context, executemany):
        timing = current.get()
        if timing is not None:
            context._run_sql_timing = (timing, timing.phase, timing.clock())

    def finish(context):
        sample = getattr(context, "_run_sql_timing", None)
        if sample is not None:
            context._run_sql_timing = None
            timing, phase, started = sample
            timing.record_sql(phase, started)

    def after(conn, cursor, statement, parameters, context, executemany):
        finish(context)

    def error(context):
        if context.execution_context is not None:
            finish(context.execution_context)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    event.listen(engine.sync_engine, "after_cursor_execute", after)
    event.listen(engine.sync_engine, "handle_error", error)
