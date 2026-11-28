"""The kernel's thin timing interface.

Wall-clock run timing is measured by the registered timing port
(``agent.extensions.TimingPort``, implemented by ``trajectory.latency``). The
kernel marks phases, spans and external waits here; with no port registered
nothing is measured and every call is a no-op.
"""
from __future__ import annotations

import asyncio
from contextvars import copy_context
from functools import wraps
from weakref import WeakKeyDictionary

from core.timing import current, network_wait, provider_wait, span

__all__ = ["current", "provider_wait", "network_wait", "span", "phase", "bind_run", "measured_span",
           "measured_run", "create_dependency_task", "await_dependency", "provider_chunks", "ProviderStream"]

_dependencies = WeakKeyDictionary()


def phase(name):
    """Switch the run's exclusive phase; only the run-loop owner calls this."""
    timing = current.get()
    if timing is not None:
        timing.switch_phase(name)


def bind_run(run_id):
    timing = current.get()
    if timing is not None:
        timing.run_id = run_id


def measured_span(name):
    """Nested diagnostic span, separate from the additive run phases."""
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            timing = current.get()
            if timing is None:
                return await function(*args, **kwargs)
            with timing.span(name):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


def measured_run(function):
    """Time one run with a timer from the registered port; none registered, the run is not timed."""
    @wraps(function)
    async def run(session_id, *args, **kwargs):
        from agent.extensions import extensions
        port = extensions().timing
        if port is None:
            return await function(session_id, *args, **kwargs)
        timing = port.start_run()
        token = current.set(timing)
        try:
            return await function(session_id, *args, **kwargs)
        finally:
            ended = timing.clock()
            current.reset(token)
            port.finish_run(timing, session_id=session_id,
                            run_id=timing.run_id or getattr(kwargs.get("lease"), "run_id", None), ended=ended)
    return run


def create_dependency_task(coroutine, *, name=None):
    """Isolate speculative work's timing until a run actually waits for it."""
    from agent.extensions import extensions
    port = extensions().timing
    if port is None:
        return asyncio.create_task(coroutine, name=name)
    timing = port.start_dependency(current.get())
    context = copy_context()
    context.run(current.set, timing)
    task = asyncio.create_task(coroutine, name=name, context=context)
    _dependencies[task] = timing
    return task


async def await_dependency(task):
    parent, dependency = current.get(), _dependencies.get(task)
    if parent is None or dependency is None or dependency is parent:
        return await task
    with span("memory.prefetch_wait"), dependency.forward_waits_to(parent):
        return await task


async def provider_chunks(stream):
    iterator = aiter(stream)
    while True:
        try:
            with provider_wait():
                chunk = await anext(iterator)
        except StopAsyncIteration:
            return
        # The consumer can do slow SQL writes here. They are application time.
        yield chunk


class ProviderStream:
    """Time HTTP stream enter/exit, without timing the consumer inside it."""
    def __init__(self, manager):
        self.manager = manager

    async def __aenter__(self):
        with provider_wait():
            return await self.manager.__aenter__()

    async def __aexit__(self, *args):
        with provider_wait():
            return await self.manager.__aexit__(*args)
