"""Wall-clock run timing, excluding observed model/provider transport waits.

Only provider awaits belong in ``provider_wait``. ``application_ms`` retains
the historical total-minus-provider meaning. ``internal_ms`` also excludes
observed runtime HTTP IO; authorization, parsing, SQL and retry backoff remain
internal. Overlapping external waits count once. Other tool/service IO is not
instrumented here; these are wall-clock measurements, not CPU time. Queue time
before run_loop and browser/playback latency are outside this measurement.

``RunTiming`` is the timer behind the kernel's timing interface
(``agent.timing``); ``TrajectoryTiming`` is the port the trajectory plugin
registers. The free functions are the kernel's, re-exported for callers of the
former ``agent.latency``.
"""
import time
import json
from contextlib import contextmanager

from core.log import create_logger
from core.timing import attach_sql_timing, current, network_wait, provider_wait, span  # noqa: F401
from agent.timing import (  # noqa: F401
    ProviderStream, _dependencies, await_dependency, bind_run, create_dependency_task, measured_run,
    measured_span, phase, provider_chunks,
)

# The historical logger name: operators grep for "agent latency" lines under it.
log = create_logger("agent.latency")


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
        self.wait_observers = []
        self.sql = {}

    def _notify_wait(self, kind, active):
        for observer in tuple(self.wait_observers):
            observer(kind, active)

    @contextmanager
    def forward_waits_to(self, parent):
        """Attribute only external IO overlapping the caller's dependency wait.

        Prefetch can start before a run or while the run does other work. Its
        earlier IO must not be subtracted from the run's internal work. Relay
        active and subsequent transport waits only while the run awaits it.
        """
        active = {}

        def changed(kind, waiting):
            if waiting and kind not in active:
                scope = parent.provider_wait() if kind == "provider" else parent.network_wait()
                active[kind] = scope
                scope.__enter__()
            elif not waiting and kind in active:
                active.pop(kind).__exit__(None, None, None)

        self.wait_observers.append(changed)
        try:
            changed("provider", bool(self.active))
            changed("network", bool(self.network_active))
            yield
        finally:
            self.wait_observers.remove(changed)
            for kind in tuple(active):
                changed(kind, False)

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
        values["count"] = values.get("count", 0) + 1
        values["max_ms"] = max(values.get("max_ms", 0.0), total)
        values["total_ms"] += total
        values["provider_wait_ms"] += provider
        values["application_ms"] += max(total - provider, 0.0)
        values["runtime_network_wait_ms"] += network
        values["internal_ms"] += max(total - external, 0.0)

    def switch_phase(self, name):
        now = self._elapsed()
        self._add(self.phases, self.phase, self.phase_start, now)
        self.phase, self.phase_start = name, now

    def record_sql(self, phase, started):
        # Aggregate timings only: never log SQL text, parameters or credentials.
        elapsed = max(0, (self.clock() - started) * 1000)
        bucket = self.sql.setdefault(phase, {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
        bucket["count"] += 1
        bucket["total_ms"] += elapsed
        bucket["max_ms"] = max(bucket["max_ms"], elapsed)

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
        if self.active == 1:
            self._notify_wait("provider", True)
        try:
            with self._external_wait():
                yield
        finally:
            self.active -= 1
            if not self.active:
                self.provider_seconds += self.clock() - self.provider_started
                self.provider_started = None
                self._notify_wait("provider", False)

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
        if self.network_active == 1:
            self._notify_wait("network", True)
        try:
            with self._external_wait():
                yield
        finally:
            self.network_active -= 1
            if not self.network_active:
                self.network_seconds += self.clock() - self.network_started
                self.network_started = None
                self._notify_wait("network", False)

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
    def span(self, name):
        """Measure a bounded block using the same union-of-waits clock as decorators."""
        start = self._elapsed()
        try:
            yield
        finally:
            self._add(self.spans, name, start, self._elapsed())


class TrajectoryTiming:
    """The timing port: one RunTiming per run and per speculative dependency, logged when the run ends."""

    def start_run(self):
        return RunTiming()

    def start_dependency(self, parent):
        return RunTiming(parent.clock if parent else time.monotonic)

    def finish_run(self, timing, *, session_id, run_id, ended):
        values = timing.snapshot(ended)
        breakdown = timing.breakdown(ended)
        network = timing.network_snapshot(ended)
        log.info("agent latency session=%s run=%s total_ms=%.1f provider_wait_ms=%.1f application_ms=%.1f",
                 session_id, run_id, values["total_ms"],
                 values["provider_wait_ms"], values["application_ms"])
        log.info("agent latency breakdown session=%s run=%s phases=%s spans=%s",
                 session_id, run_id,
                 json.dumps(breakdown), json.dumps(timing.spans))
        log.info("agent internal latency session=%s run=%s measurements=%s",
                 session_id, run_id,
                 json.dumps({key: round(value, 1) for key, value in network.items()}))
        log.info("agent sql latency session=%s run=%s phases=%s",
                 session_id, run_id,
                 json.dumps({phase: {key: round(value, 1) for key, value in stats.items()}
                             for phase, stats in timing.sql.items()}))
