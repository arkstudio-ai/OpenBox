"""Bounded tool-body concurrency with model-ordered commit.

The scheduler deliberately knows nothing about ToolPart persistence or hooks.
Callers provide five staged operations per model call: ordered preparation, an
optional body, ordered commit, the result to use when cancellation prevents
dispatch, and the result to use when a dispatched body exceeds its deadline.
Only bodies overlap; policy and observable state stay serial.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


DEFAULT_MAX_PARALLEL_TOOL_CALLS = 10
# Individual tools normally carry tighter protocol-specific timeouts.  This is
# the kernel's final liveness fence for a broken/custom tool that never returns.
# Ten minutes is deliberately conservative for production while tests and
# specialized callers can inject a shorter positive deadline.
DEFAULT_TOOL_BODY_TIMEOUT_SECONDS = 600.0
_EMPTY = object()


@dataclass(frozen=True)
class ToolCallPreparation:
    """The result of ordered preparation for one call.

    ``body`` is present only when dispatch is still required.  A policy or
    validation result uses ``body=None`` and parks ``result`` directly in its
    model-order slot.
    """

    body: Callable[[], Awaitable[Any]] | None = None
    result: Any = None

    @classmethod
    def dispatch(cls, body: Callable[[], Awaitable[Any]]) -> "ToolCallPreparation":
        return cls(body=body)

    @classmethod
    def ready(cls, result: Any) -> "ToolCallPreparation":
        return cls(result=result)


@dataclass(frozen=True)
class ScheduledToolCall:
    """One assistant tool call represented as scheduler-owned phases."""

    prepare: Callable[[], Awaitable[ToolCallPreparation]]
    commit: Callable[[Any], Awaitable[None]]
    aborted_before_dispatch: Callable[[], Awaitable[Any]]
    timed_out: Callable[[float], Awaitable[Any]]
    # Only the exact boolean True is parallel. Missing/None/truthy classifiers
    # remain exclusive; a callable may reclassify an unstarted call live.
    parallel_safe: object = False

    def is_parallel_safe(self) -> bool:
        try:
            value = self.parallel_safe() if callable(self.parallel_safe) else self.parallel_safe
        except Exception:
            return False
        return value is True


@dataclass(frozen=True)
class ToolScheduleResult:
    started: int
    committed: int
    skipped: int
    aborted: bool


@dataclass(frozen=True)
class _GroupResult:
    consumed: int
    started: int
    committed: int
    skipped: int
    aborted: bool


async def run_ordered_tool_calls(
    calls: list[ScheduledToolCall],
    abort,
    *,
    max_parallel: int = DEFAULT_MAX_PARALLEL_TOOL_CALLS,
    body_timeout_seconds: float = DEFAULT_TOOL_BODY_TIMEOUT_SECONDS,
) -> ToolScheduleResult:
    """Run model-ordered calls with bounded parallel bodies and barriers.

    An exclusive call is a singleton group and starts only after the preceding
    parallel group has fully committed.  Cancellation stops replenishment,
    drains every started body, commits their contiguous slots, and then commits
    one synthetic aborted-before-dispatch result for every unstarted call.
    """
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
        raise ValueError("max_parallel must be a positive integer")
    if (
        isinstance(body_timeout_seconds, bool)
        or not isinstance(body_timeout_seconds, (int, float))
        or not math.isfinite(float(body_timeout_seconds))
        or body_timeout_seconds <= 0
    ):
        raise ValueError("body_timeout_seconds must be a finite positive number")
    body_timeout_seconds = float(body_timeout_seconds)

    next_index = 0
    started = committed = skipped = 0
    while next_index < len(calls):
        first = calls[next_index]
        parallel_group = first.is_parallel_safe()
        group = calls[next_index:] if parallel_group else [first]
        outcome = await _run_group(
            group,
            abort,
            parallel_group=parallel_group,
            max_parallel=max_parallel,
            body_timeout_seconds=body_timeout_seconds,
        )
        next_index += outcome.consumed
        started += outcome.started
        committed += outcome.committed
        skipped += outcome.skipped
        if not outcome.aborted:
            continue

        # An exclusive singleton cannot own the later suffix, so pair and
        # commit those undispatched calls here. A parallel group already owns
        # the complete suffix and consumes it in _run_group.
        for call in calls[next_index:]:
            result = await call.aborted_before_dispatch()
            await call.commit(result)
            committed += 1
            skipped += 1
        return ToolScheduleResult(started, committed, skipped, True)

    return ToolScheduleResult(started, committed, skipped, False)


async def _run_group(
    group: list[ScheduledToolCall],
    abort,
    *,
    parallel_group: bool,
    max_parallel: int,
    body_timeout_seconds: float,
) -> _GroupResult:
    """Run one exclusive barrier or one reclassifiable parallel suffix."""
    slots: list[Any] = [_EMPTY for _ in group]
    next_to_start = 0
    started = 0
    committed = 0
    skipped = 0
    aborted = bool(abort.is_set())
    first_failure: BaseException | None = None
    in_flight: dict[int, asyncio.Task[int]] = {}

    def remember_failure(error: BaseException) -> None:
        nonlocal first_failure
        if first_failure is None:
            first_failure = error

    def raise_failure() -> None:
        if first_failure is not None:
            raise first_failure

    async def commit_ready() -> None:
        nonlocal committed
        while committed < started:
            result = slots[committed]
            if result is _EMPTY:
                break
            await group[committed].commit(result)
            committed += 1

    async def settle(index: int, body: Callable[[], Awaitable[Any]]) -> int:
        timeout_scope = asyncio.timeout(body_timeout_seconds)
        try:
            async with timeout_scope:
                slots[index] = await body()
        except TimeoutError as error:
            # A tool may deliberately raise TimeoutError itself.  Only the
            # scheduler-owned deadline becomes a canonical tool_timeout slot.
            if not timeout_scope.expired():
                remember_failure(error)
            else:
                try:
                    slots[index] = await group[index].timed_out(
                        body_timeout_seconds
                    )
                except BaseException as timeout_error:
                    remember_failure(timeout_error)
        except BaseException as error:
            remember_failure(error)
        return index

    async def start_call(index: int) -> None:
        nonlocal started
        call = group[index]
        started += 1
        preparation = await call.prepare()
        # Cancellation may land while ordered policy/permission is awaiting.
        # No body has started, so the canonical result is aborted-before-dispatch.
        if preparation.body is None:
            slots[index] = preparation.result
        elif abort.is_set():
            slots[index] = await call.aborted_before_dispatch()
        else:
            in_flight[index] = asyncio.create_task(settle(index, preparation.body))

    async def fill_pool() -> None:
        nonlocal next_to_start, aborted
        while (
            not aborted
            and next_to_start < len(group)
            and len(in_flight) < max_parallel
        ):
            call = group[next_to_start]
            # Re-read before each start. A live downgrade becomes a barrier and
            # remains for the outer loop after this group drains.
            if next_to_start > 0 and parallel_group and not call.is_parallel_safe():
                break
            await start_call(next_to_start)
            next_to_start += 1
            raise_failure()
            await commit_ready()
            raise_failure()
            if abort.is_set():
                aborted = True

    try:
        await fill_pool()
        while in_flight:
            done, _ = await asyncio.wait(
                tuple(in_flight.values()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                index = await task
                in_flight.pop(index, None)
            raise_failure()
            await commit_ready()
            raise_failure()
            if abort.is_set():
                aborted = True
            await fill_pool()
    except BaseException as error:
        remember_failure(error)
        # External cancellation is not a tool timeout. Cancel every child now
        # so body/request-context cleanup runs, then propagate CancelledError.
        # Ordinary failures retain the historical drain-before-raise contract.
        if isinstance(error, asyncio.CancelledError):
            for task in in_flight.values():
                task.cancel()
        # Quiescence before failure: never leave a started body orphaned.
        if in_flight:
            await asyncio.gather(*in_flight.values(), return_exceptions=True)
        assert first_failure is not None
        raise first_failure

    if aborted:
        # Every started slot (including a call cancelled during prepare) must
        # commit before undispatched suffix results are synthesized.
        await commit_ready()
        for call in group[started:]:
            result = await call.aborted_before_dispatch()
            await call.commit(result)
            committed += 1
            skipped += 1
        return _GroupResult(len(group), started, committed, skipped, True)

    if committed != started:  # pragma: no cover - defensive invariant
        raise RuntimeError("tool scheduler left a settled call uncommitted")
    return _GroupResult(started, started, committed, skipped, False)
