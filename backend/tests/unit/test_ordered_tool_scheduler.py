"""The tool scheduler overlaps bodies without reordering observable commits."""
from __future__ import annotations

import asyncio

import pytest

from agent.tool_scheduler import (
    ScheduledToolCall,
    ToolCallPreparation,
    run_ordered_tool_calls,
)


def scheduled(
    name: str,
    events: list[str],
    *,
    parallel_safe=False,
    gate: asyncio.Event | None = None,
) -> ScheduledToolCall:
    async def prepare():
        events.append(f"pre:{name}")

        async def body():
            events.append(f"body-start:{name}")
            if gate is not None:
                await gate.wait()
            events.append(f"body-end:{name}")
            return f"result:{name}"

        return ToolCallPreparation.dispatch(body)

    async def commit(result):
        events.append(f"commit:{name}:{result}")

    async def aborted():
        return f"aborted:{name}"

    async def timed_out(seconds: float):
        events.append(f"timeout:{name}:{seconds:g}")
        return f"timeout:{name}"

    return ScheduledToolCall(
        prepare=prepare,
        commit=commit,
        aborted_before_dispatch=aborted,
        timed_out=timed_out,
        parallel_safe=parallel_safe,
    )


async def until(predicate) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


@pytest.mark.asyncio
async def test_parallel_bodies_settle_out_of_order_but_commit_by_model_order():
    events: list[str] = []
    first = asyncio.Event()
    second = asyncio.Event()
    running = asyncio.create_task(run_ordered_tool_calls(
        [
            scheduled("1", events, parallel_safe=True, gate=first),
            scheduled("2", events, parallel_safe=True, gate=second),
        ],
        asyncio.Event(),
    ))

    await until(lambda: events.count("body-start:1") and events.count("body-start:2"))
    second.set()
    await until(lambda: "body-end:2" in events)
    assert not any(item.startswith("commit:") for item in events)

    first.set()
    result = await running

    assert [item for item in events if item.startswith("pre:")] == ["pre:1", "pre:2"]
    assert [item for item in events if item.startswith("commit:")] == [
        "commit:1:result:1",
        "commit:2:result:2",
    ]
    assert (result.started, result.committed, result.skipped) == (2, 2, 0)


@pytest.mark.asyncio
async def test_exclusive_call_is_a_full_commit_barrier():
    events: list[str] = []
    first = asyncio.Event()
    exclusive = asyncio.Event()
    last = asyncio.Event()
    running = asyncio.create_task(run_ordered_tool_calls(
        [
            scheduled("parallel-before", events, parallel_safe=True, gate=first),
            scheduled("exclusive", events, gate=exclusive),
            scheduled("parallel-after", events, parallel_safe=True, gate=last),
        ],
        asyncio.Event(),
    ))

    await until(lambda: "body-start:parallel-before" in events)
    assert "pre:exclusive" not in events
    first.set()
    await until(lambda: "body-start:exclusive" in events)
    assert events.index("commit:parallel-before:result:parallel-before") < events.index("pre:exclusive")
    assert "pre:parallel-after" not in events
    exclusive.set()
    await until(lambda: "body-start:parallel-after" in events)
    assert events.index("commit:exclusive:result:exclusive") < events.index("pre:parallel-after")
    last.set()
    await running


@pytest.mark.asyncio
async def test_abort_stops_refill_drains_started_and_pairs_every_unstarted_call():
    events: list[str] = []
    gates = [asyncio.Event() for _ in range(4)]
    abort = asyncio.Event()
    running = asyncio.create_task(run_ordered_tool_calls(
        [
            scheduled(str(index), events, parallel_safe=True, gate=gate)
            for index, gate in enumerate(gates, start=1)
        ],
        abort,
        max_parallel=2,
    ))

    await until(lambda: events.count("body-start:1") and events.count("body-start:2"))
    abort.set()
    gates[1].set()
    await until(lambda: "body-end:2" in events)
    assert not running.done(), "the earlier started body must be drained"
    gates[0].set()
    result = await running

    assert not any(item in events for item in ("pre:3", "pre:4", "body-start:3", "body-start:4"))
    assert [item for item in events if item.startswith("commit:")] == [
        "commit:1:result:1",
        "commit:2:result:2",
        "commit:3:aborted:3",
        "commit:4:aborted:4",
    ]
    assert result.aborted
    assert (result.started, result.committed, result.skipped) == (2, 4, 2)


@pytest.mark.asyncio
async def test_default_unknown_and_non_boolean_classifiers_fail_closed():
    events: list[str] = []
    gate1 = asyncio.Event()
    gate2 = asyncio.Event()
    calls = [
        scheduled("missing", events, gate=gate1),
        scheduled("truthy", events, parallel_safe=1, gate=gate2),
    ]
    assert not calls[0].is_parallel_safe()
    assert not calls[1].is_parallel_safe()

    running = asyncio.create_task(run_ordered_tool_calls(calls, asyncio.Event()))
    await until(lambda: "body-start:missing" in events)
    assert "pre:truthy" not in events
    gate1.set()
    await until(lambda: "body-start:truthy" in events)
    gate2.set()
    await running


@pytest.mark.asyncio
async def test_scheduler_failure_drains_started_bodies_before_raising():
    events: list[str] = []
    drain = asyncio.Event()
    failure = RuntimeError("scheduler body failed")

    async def failing_prepare():
        async def body():
            raise failure

        return ToolCallPreparation.dispatch(body)

    async def draining_prepare():
        async def body():
            events.append("drain-start")
            await drain.wait()
            events.append("drain-end")
            return "ok"

        return ToolCallPreparation.dispatch(body)

    async def commit(_result):
        events.append("unexpected-commit")

    async def aborted():
        return "aborted"

    async def timed_out(_seconds):
        return "timed-out"

    calls = [
        ScheduledToolCall(failing_prepare, commit, aborted, timed_out, True),
        ScheduledToolCall(draining_prepare, commit, aborted, timed_out, True),
    ]
    running = asyncio.create_task(run_ordered_tool_calls(calls, asyncio.Event()))
    await until(lambda: "drain-start" in events)
    await asyncio.sleep(0)
    assert not running.done()
    drain.set()
    with pytest.raises(RuntimeError, match="scheduler body failed"):
        await running
    assert "drain-end" in events
    assert "unexpected-commit" not in events


@pytest.mark.asyncio
async def test_abort_during_prepare_keeps_specific_policy_result():
    abort = asyncio.Event()
    committed: list[str] = []

    async def prepare():
        abort.set()
        return ToolCallPreparation.ready("permission-denied")

    async def commit(result):
        committed.append(result)

    async def aborted():
        return "synthetic-abort"

    async def timed_out(_seconds):
        return "timed-out"

    result = await run_ordered_tool_calls(
        [ScheduledToolCall(prepare, commit, aborted, timed_out, False)],
        abort,
    )

    assert committed == ["permission-denied"]
    assert (result.started, result.committed, result.skipped) == (1, 1, 0)
    assert result.aborted is True


@pytest.mark.asyncio
async def test_hung_body_is_cancelled_and_commits_timeout_result():
    events: list[str] = []
    body_started = asyncio.Event()
    body_cancelled = asyncio.Event()

    async def prepare():
        async def body():
            body_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                body_cancelled.set()

        return ToolCallPreparation.dispatch(body)

    async def commit(result):
        events.append(f"commit:{result}")

    async def aborted():
        return "aborted"

    async def timed_out(seconds):
        assert body_cancelled.is_set(), "body cleanup must finish before timeout synthesis"
        events.append(f"timeout:{seconds:g}")
        return "tool-timeout"

    result = await run_ordered_tool_calls(
        [ScheduledToolCall(prepare, commit, aborted, timed_out)],
        asyncio.Event(),
        body_timeout_seconds=0.02,
    )

    assert body_started.is_set()
    assert body_cancelled.is_set()
    assert events == ["timeout:0.02", "commit:tool-timeout"]
    assert (result.started, result.committed, result.skipped) == (1, 1, 0)


@pytest.mark.asyncio
async def test_timeout_and_fast_parallel_result_still_commit_in_model_order():
    events: list[str] = []
    fast_finished = asyncio.Event()

    async def prepare_hung():
        async def body():
            events.append("body-start:hung")
            await asyncio.Event().wait()

        return ToolCallPreparation.dispatch(body)

    async def prepare_fast():
        async def body():
            events.append("body-end:fast")
            fast_finished.set()
            return "fast-result"

        return ToolCallPreparation.dispatch(body)

    async def commit_hung(result):
        events.append(f"commit:hung:{result}")

    async def commit_fast(result):
        events.append(f"commit:fast:{result}")

    async def aborted():
        return "aborted"

    async def timeout_hung(_seconds):
        assert fast_finished.is_set()
        return "timeout-result"

    async def timeout_fast(_seconds):  # pragma: no cover - defensive
        raise AssertionError("fast call must not time out")

    await run_ordered_tool_calls(
        [
            ScheduledToolCall(
                prepare_hung, commit_hung, aborted, timeout_hung, True
            ),
            ScheduledToolCall(
                prepare_fast, commit_fast, aborted, timeout_fast, True
            ),
        ],
        asyncio.Event(),
        body_timeout_seconds=0.02,
    )

    assert [event for event in events if event.startswith("commit:")] == [
        "commit:hung:timeout-result",
        "commit:fast:fast-result",
    ]


@pytest.mark.asyncio
async def test_timed_out_exclusive_call_remains_a_full_commit_barrier():
    events: list[str] = []

    async def prepare_exclusive():
        events.append("pre:exclusive")

        async def body():
            events.append("body-start:exclusive")
            await asyncio.Event().wait()

        return ToolCallPreparation.dispatch(body)

    async def prepare_after():
        events.append("pre:after")

        async def body():
            events.append("body-start:after")
            return "after-result"

        return ToolCallPreparation.dispatch(body)

    async def commit_exclusive(result):
        events.append(f"commit:exclusive:{result}")

    async def commit_after(result):
        events.append(f"commit:after:{result}")

    async def aborted():
        return "aborted"

    async def timed_out(_seconds):
        return "timeout-result"

    await run_ordered_tool_calls(
        [
            ScheduledToolCall(
                prepare_exclusive,
                commit_exclusive,
                aborted,
                timed_out,
                False,
            ),
            ScheduledToolCall(
                prepare_after,
                commit_after,
                aborted,
                timed_out,
                True,
            ),
        ],
        asyncio.Event(),
        body_timeout_seconds=0.02,
    )

    assert events.index("commit:exclusive:timeout-result") < events.index("pre:after")
    assert events.index("pre:after") < events.index("commit:after:after-result")


@pytest.mark.asyncio
async def test_external_scheduler_cancellation_cancels_body_without_timeout_result():
    events: list[str] = []
    body_started = asyncio.Event()
    body_cancelled = asyncio.Event()

    async def prepare():
        async def body():
            body_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                body_cancelled.set()

        return ToolCallPreparation.dispatch(body)

    async def commit(_result):
        events.append("commit")

    async def aborted():
        return "aborted"

    async def timed_out(_seconds):
        events.append("timeout")
        return "timeout"

    running = asyncio.create_task(run_ordered_tool_calls(
        [ScheduledToolCall(prepare, commit, aborted, timed_out)],
        asyncio.Event(),
        body_timeout_seconds=60,
    ))
    await body_started.wait()
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert body_cancelled.is_set()
    assert events == []


@pytest.mark.parametrize("deadline", [0, -1, float("inf"), float("nan"), True])
@pytest.mark.asyncio
async def test_body_timeout_must_be_a_finite_positive_number(deadline):
    with pytest.raises(ValueError, match="body_timeout_seconds"):
        await run_ordered_tool_calls(
            [],
            asyncio.Event(),
            body_timeout_seconds=deadline,
        )
