"""Wall-clock run timing, excluding observed model/provider transport waits.

Only provider awaits belong in ``provider_wait``. ``application_ms`` retains
the historical total-minus-provider meaning. ``internal_ms`` also excludes
observed runtime HTTP IO; authorization, parsing, SQL and retry backoff remain
internal. Overlapping external waits count once. Other tool/service IO is not
instrumented here; these are wall-clock measurements, not CPU time. Queue time
before run_loop and browser/playback latency are outside this measurement.
"""
import time
import json
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from core.log import create_logger

log = create_logger("agent.latency")
current: ContextVar["RunTiming | None"] = ContextVar("run_latency", default=None)


class RunTiming:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.provider_seconds = 0.0
        self.active = 0
        self.provider_started = None
        self.run_id = None
        self.phase = "setup"
        self.phase_start = (0.0, 0.0, 0.0, 0.0)
        self.phases = {}
        self.spans = {}
        self.network_seconds = self.external_seconds = 0.0
        self.network_active = self.external_active = 0
        self.network_started = self.external_started = None

    def _elapsed(self, now=None):
        now = self.clock() if now is None else now
        values = self.snapshot(now)
        network = self.network_snapshot(now)
        return (values["total_ms"], values["provider_wait_ms"],
                network["runtime_network_wait_ms"], network["external_wait_ms"])

    @staticmethod
    def _add(target, name, start, end):
        values = target.setdefault(name, {key: 0.0 for key in (
            "total_ms", "provider_wait_ms", "application_ms", "runtime_network_wait_ms", "internal_ms")})
        total, provider, network, external = (end[i] - start[i] for i in range(4))
        values["total_ms"] += total
        values["provider_wait_ms"] += provider
        values["application_ms"] += max(total - provider, 0.0)
        values["runtime_network_wait_ms"] += network
        values["internal_ms"] += max(total - external, 0.0)

    def switch_phase(self, name):
        now = self._elapsed()
        self._add(self.phases, self.phase, self.phase_start, now)
        self.phase, self.phase_start = name, now

    def breakdown(self, now=None):
        # Snapshot without closing the phase, so diagnostics never change timing.
        phases = {name: dict(values) for name, values in self.phases.items()}
        self._add(phases, self.phase, self.phase_start, self._elapsed(now))
        return {name: {key: round(value, 1) for key, value in values.items()}
                for name, values in phases.items()}

    @contextmanager
    def provider_wait(self):
        if not self.active:
            self.provider_started = self.clock()
        self.active += 1
        try:
            with self._external_wait():
                yield
        finally:
            self.active -= 1
            if not self.active:
                self.provider_seconds += self.clock() - self.provider_started
                self.provider_started = None

    @contextmanager
    def _external_wait(self):
        if not self.external_active:
            self.external_started = self.clock()
        self.external_active += 1
        try:
            yield
        finally:
            self.external_active -= 1
            if not self.external_active:
                self.external_seconds += self.clock() - self.external_started
                self.external_started = None

    @contextmanager
    def network_wait(self):
        if not self.network_active:
            self.network_started = self.clock()
        self.network_active += 1
        try:
            with self._external_wait():
                yield
        finally:
            self.network_active -= 1
            if not self.network_active:
                self.network_seconds += self.clock() - self.network_started
                self.network_started = None

    def network_snapshot(self, now=None):
        now = self.clock() if now is None else now
        total = max(now - self.started, 0.0)
        network = self.network_seconds + (now - self.network_started if self.network_active else 0)
        external = self.external_seconds + (now - self.external_started if self.external_active else 0)
        return {"runtime_network_wait_ms": network * 1000, "external_wait_ms": external * 1000,
                "internal_ms": max(total - external, 0) * 1000}

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        total = max(now - self.started, 0.0)
        provider = self.provider_seconds
        if self.active:
            provider += now - self.provider_started
        provider = min(max(provider, 0.0), total)
        return {"total_ms": total * 1000, "provider_wait_ms": provider * 1000,
                "application_ms": (total - provider) * 1000}


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
            start = timing._elapsed()
            try:
                return await function(*args, **kwargs)
            finally:
                timing._add(timing.spans, name, start, timing._elapsed())
        return wrapped
    return decorate


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


def measured_run(function):
    @wraps(function)
    async def run(session_id, *args, **kwargs):
        timing = RunTiming()
        token = current.set(timing)
        try:
            return await function(session_id, *args, **kwargs)
        finally:
            ended = timing.clock()
            values = timing.snapshot(ended)
            breakdown = timing.breakdown(ended)
            network = timing.network_snapshot(ended)
            current.reset(token)
            lease = kwargs.get("lease")
            log.info("agent latency session=%s run=%s total_ms=%.1f provider_wait_ms=%.1f application_ms=%.1f",
                     session_id, timing.run_id or getattr(lease, "run_id", None), values["total_ms"],
                     values["provider_wait_ms"], values["application_ms"])
            log.info("agent latency breakdown session=%s run=%s phases=%s spans=%s",
                     session_id, timing.run_id or getattr(lease, "run_id", None),
                     json.dumps(breakdown), json.dumps(timing.spans))
            log.info("agent internal latency session=%s run=%s measurements=%s",
                     session_id, timing.run_id or getattr(lease, "run_id", None),
                     json.dumps({key: round(value, 1) for key, value in network.items()}))
    return run
