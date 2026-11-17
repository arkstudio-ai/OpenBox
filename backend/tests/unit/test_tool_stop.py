"""Stop reaches the body of a tool that declares ``cancel_on_stop``; everything else drains."""
from __future__ import annotations

import asyncio

import pytest

from agent.tool_scheduler import ScheduledToolCall, ToolCallPreparation, run_ordered_tool_calls
from agent.tool_stop import STOPPED, run_until_stopped


def call(name, events, *, gate=None, cancel_on_stop=False, parallel_safe=True, with_stopped=True):
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def prepare():
        async def body():
            started.set()
            try:
                if gate is not None:
                    await gate.wait()
                return f"result:{name}"
            finally:
                cleaned.set()
        return ToolCallPreparation.dispatch(body)

    async def commit(result):
        events.append(f"commit:{name}:{result}")

    async def aborted():
        return f"aborted:{name}"

    async def timed_out(_seconds):
        return f"timeout:{name}"

    async def stopped():
        assert cleaned.is_set(), "the body's cleanup must finish before the stopped slot is built"
        return f"stopped:{name}"

    scheduled = ScheduledToolCall(prepare, commit, aborted, timed_out, parallel_safe=parallel_safe,
                                  cancel_on_stop=cancel_on_stop, stopped=stopped if with_stopped else None)
    return scheduled, started


async def test_helper_returns_the_result_or_stopped_and_lets_cleanup_finish():
    abort = asyncio.Event()

    async def quick():
        return 7
    assert await run_until_stopped(quick, abort) == 7
    assert await run_until_stopped(quick, None) == 7

    cleaned = asyncio.Event()

    async def hung():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    waiter = asyncio.create_task(run_until_stopped(hung, abort))
    await asyncio.sleep(0.01)
    abort.set()
    assert await asyncio.wait_for(waiter, 1) is STOPPED and cleaned.is_set()
    # Already stopped: the body is never started.
    assert await run_until_stopped(hung, abort) is STOPPED


async def test_helper_propagates_the_callers_own_cancellation():
    cancelled = asyncio.Event()

    async def hung():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    waiter = asyncio.create_task(run_until_stopped(hung, asyncio.Event()))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert cancelled.is_set()


async def test_stop_cancels_an_opted_in_body_and_drains_the_rest_in_model_order():
    """The Gaode shape: several bodies in flight, one hung read, one paid call."""
    events: list[str] = []
    abort, paid_gate = asyncio.Event(), asyncio.Event()
    read, read_started = call("read", events, gate=asyncio.Event(), cancel_on_stop=True)
    paid, paid_started = call("paid", events, gate=paid_gate)
    later, _ = call("later", events, parallel_safe=False)

    running = asyncio.create_task(run_ordered_tool_calls([read, paid, later], abort))
    await asyncio.wait_for(asyncio.gather(read_started.wait(), paid_started.wait()), 5)
    abort.set()
    # The read is closed at once; the paid body is still owed its result.
    for _ in range(500):
        if events:
            break
        await asyncio.sleep(0.01)
    assert events == ["commit:read:stopped:read"] and not running.done()
    paid_gate.set()
    result = await asyncio.wait_for(running, 5)
    assert events == ["commit:read:stopped:read", "commit:paid:result:paid", "commit:later:aborted:later"]
    assert result.aborted and (result.started, result.committed, result.skipped) == (2, 3, 1)


@pytest.mark.parametrize("flag,with_stopped", [(False, True), (None, True), ("yes", True), (True, False)])
async def test_only_an_exact_true_with_a_stopped_builder_opts_in(flag, with_stopped):
    events: list[str] = []
    abort, gate = asyncio.Event(), asyncio.Event()
    scheduled, started = call("x", events, gate=gate, cancel_on_stop=flag, with_stopped=with_stopped)
    running = asyncio.create_task(run_ordered_tool_calls([scheduled], abort))
    await asyncio.wait_for(started.wait(), 1)
    abort.set()
    await asyncio.sleep(0.02)
    assert events == [] and not running.done()  # drains
    gate.set()
    await asyncio.wait_for(running, 1)
    assert events == ["commit:x:result:x"]


async def test_an_opted_in_body_that_finishes_is_untouched_and_the_deadline_still_applies():
    events: list[str] = []
    done, _ = call("done", events, cancel_on_stop=True)
    await run_ordered_tool_calls([done], asyncio.Event())
    assert events == ["commit:done:result:done"]

    hung, _ = call("hung", events, gate=asyncio.Event(), cancel_on_stop=True)
    await run_ordered_tool_calls([hung], asyncio.Event(), body_timeout_seconds=0.02)
    assert events[-1] == "commit:hung:timeout:hung"


def test_the_tools_that_opt_in_are_the_read_only_ones():
    from tool.registry import list_tools, register_builtin_tools

    register_builtin_tools(load_custom=False)
    opted = {tool.id for tool in list_tools() if getattr(tool, "cancel_on_stop", False)}
    assert opted == {"read", "glob", "grep", "view_image", "web_fetch", "web_search", "hot_trends", "footage_analyze"}
