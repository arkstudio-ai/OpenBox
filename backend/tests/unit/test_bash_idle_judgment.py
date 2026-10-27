"""Idle classification must never hold up a live shell's output or Stop."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from sandbox.client import CommandExit, IdleNotification, OutputChunk
from tool import bash
from tool.tool import ToolContext


class CommandStream:
    def __init__(self):
        self.items = asyncio.Queue()
        self.closed = asyncio.Event()
        self.calls = 0
        self.kill_command = AsyncMock()

    async def execute_stream(self, **kwargs):
        self.calls += 1
        try:
            while True:
                item = await self.items.get()
                if isinstance(item, BaseException):
                    raise item
                yield item
                if isinstance(item, int):
                    return
        finally:
            self.closed.set()

    def send(self, item):
        self.items.put_nowait(item)


def start_command(monkeypatch, judge):
    monkeypatch.setattr(bash, "_judge_idle_command", judge)
    sandbox = CommandStream()
    ctx = ToolContext(sandbox=sandbox)
    ctx.update_output = AsyncMock()
    task = asyncio.create_task(bash.execute(bash.BashArgs(command="fixture"), ctx))
    return sandbox, ctx, task


async def until(awaitable):
    # The tested interleavings use events, never real sleeps or an LLM. A short
    # deadline makes a regression fail instead of waiting for the blocked judge.
    return await asyncio.wait_for(awaitable, 2)


def idle(seconds=60):
    return IdleNotification(idle_seconds=seconds, total_seconds=seconds, pid=54321)


@pytest.mark.parametrize("late_decision", [None, "kill", "success"])
async def test_new_output_streams_and_exit_completes_while_judge_is_pending(monkeypatch, late_decision):
    judging, cancelled, output_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def judge(**kwargs):
        judging.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            if late_decision is None:
                raise
            return late_decision

    sandbox, ctx, task = start_command(monkeypatch, judge)

    async def show_output(output):
        if "new stderr" in output:
            output_seen.set()

    ctx.update_output.side_effect = show_output
    sandbox.send(idle())
    await until(judging.wait())
    sandbox.send(OutputChunk("stderr", "new stderr\n"))
    await until(output_seen.wait())
    await until(cancelled.wait())
    sandbox.send(CommandExit(0, output_complete=False, output_incomplete_reason="pipe_drain_deadline"))
    result = await until(task)

    assert sandbox.calls == 1 and sandbox.closed.is_set()
    sandbox.kill_command.assert_not_called()
    assert ctx._trajectory_full_tool_output == "new stderr\n"
    assert result.metadata["exit_code"] == 0
    assert result.metadata["output_complete"] is False
    assert result.metadata["output_incomplete_reason"] == "pipe_drain_deadline"
    assert "output is incomplete" in result.output


async def test_exit_without_output_cancels_judge_and_preserves_exit_status(monkeypatch):
    judging, cancelled = asyncio.Event(), asyncio.Event()

    async def judge(**kwargs):
        judging.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    sandbox, _, task = start_command(monkeypatch, judge)
    sandbox.send(idle())
    await until(judging.wait())
    sandbox.send(CommandExit(23, output_complete=True))
    result = await until(task)

    assert cancelled.is_set() and sandbox.closed.is_set()
    sandbox.kill_command.assert_not_called()
    assert result.metadata["exit_code"] == 23
    assert result.metadata["output_complete"] is True


@pytest.mark.parametrize("decision", ["kill", "success"])
@pytest.mark.parametrize("event", ["output", "exit"])
async def test_stream_event_wins_when_judge_completes_in_same_turn(monkeypatch, decision, event):
    judging, release, stream_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def judge(**kwargs):
        judging.set()
        await release.wait()
        return decision

    sandbox, ctx, task = start_command(monkeypatch, judge)

    async def stream(**kwargs):
        try:
            yield idle()
            await judging.wait()
            stream_waiting.set()
            await release.wait()
            if event == "output":
                yield OutputChunk("stdout", "resumed\n")
            yield CommandExit(0, output_complete=True)
        finally:
            sandbox.closed.set()

    sandbox.execute_stream = stream
    await until(stream_waiting.wait())
    # Both the model decision and the next SSE message become runnable together.
    release.set()
    result = await until(task)

    sandbox.kill_command.assert_not_called()
    assert result.output == ("resumed\n" if event == "output" else "")
    assert result.metadata["exit_code"] == 0 and sandbox.closed.is_set()


@pytest.mark.parametrize("stop", ["abort", "cancel"])
async def test_stop_closes_stream_and_cancels_pending_judge(monkeypatch, stop):
    judging, cancelled = asyncio.Event(), asyncio.Event()

    async def judge(**kwargs):
        judging.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    sandbox, ctx, task = start_command(monkeypatch, judge)
    sandbox.send(idle())
    await until(judging.wait())
    if stop == "abort":
        ctx.abort.set()
        result = await until(task)
        assert result.metadata["failure_code"] == "tool_aborted"
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await until(task)

    assert sandbox.closed.is_set() and cancelled.is_set()
    assert sandbox.calls == 1
    sandbox.kill_command.assert_not_called()


async def test_repeated_idle_notices_do_not_queue_more_judges(monkeypatch):
    judging, output_seen = asyncio.Event(), asyncio.Event()
    judge = AsyncMock()

    async def pending(**kwargs):
        judging.set()
        await asyncio.Event().wait()

    judge.side_effect = pending
    sandbox, ctx, task = start_command(monkeypatch, judge)
    ctx.update_output.side_effect = lambda output: output_seen.set()
    sandbox.send(idle())
    await until(judging.wait())
    for seconds in range(120, 600, 60):
        sandbox.send(idle(seconds))
    sandbox.send(OutputChunk("stdout", "resumed\n"))
    await until(output_seen.wait())
    sandbox.send(CommandExit(0, output_complete=True))
    await until(task)

    assert judge.await_count == 1
    sandbox.kill_command.assert_not_called()


async def test_cancelled_judge_settles_once_while_output_and_exit_keep_flowing(monkeypatch):
    judging, settling, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finish_settlement, second_output = asyncio.Event(), asyncio.Event()
    judge = AsyncMock()

    async def pending(**kwargs):
        judging.set()
        try:
            await asyncio.Event().wait()
        finally:
            settling.set()
            await finish_settlement.wait()
            settled.set()

    judge.side_effect = pending
    sandbox, ctx, task = start_command(monkeypatch, judge)

    async def show_output(output):
        if "second" in output:
            second_output.set()

    ctx.update_output.side_effect = show_output
    sandbox.send(idle())
    await until(judging.wait())
    sandbox.send(OutputChunk("stdout", "first\n"))
    await until(settling.wait())
    sandbox.send(idle())
    sandbox.send(OutputChunk("stderr", "second\n"))
    await until(second_output.wait())
    sandbox.send(CommandExit(0, output_complete=True))
    await until(sandbox.closed.wait())
    assert not task.done() and not settled.is_set()
    finish_settlement.set()
    result = await until(task)

    assert settled.is_set() and judge.await_count == 1
    assert result.output == "first\nsecond\n"
    sandbox.kill_command.assert_not_called()


async def test_unrequested_judge_cancellation_cancels_command(monkeypatch):
    async def judge(**kwargs):
        raise asyncio.CancelledError

    sandbox, _, task = start_command(monkeypatch, judge)
    sandbox.send(idle())
    with pytest.raises(asyncio.CancelledError):
        await until(task)

    assert sandbox.closed.is_set()
    sandbox.kill_command.assert_not_called()


@pytest.mark.parametrize("decision", ["kill", "success"])
async def test_current_judgment_still_stops_the_exact_idle_pid(monkeypatch, decision):
    sandbox, _, task = start_command(monkeypatch, AsyncMock(return_value=decision))

    async def killed(pid):
        sandbox.send(CommandExit(-9, output_complete=True))

    sandbox.kill_command.side_effect = killed
    sandbox.send(idle())
    result = await until(task)

    sandbox.kill_command.assert_awaited_once_with(54321)
    assert ("completed successfully" if decision == "success" else "killed (idle)") in result.output


async def test_wait_budget_is_bounded_and_resets_only_after_new_output(monkeypatch):
    monkeypatch.setattr(bash, "MAX_IDLE_JUDGES", 2)
    judge = AsyncMock(return_value="wait")
    sandbox, ctx, task = start_command(monkeypatch, judge)
    updates = asyncio.Queue()
    ctx.update_output.side_effect = updates.put_nowait

    for seconds in (60, 120):
        sandbox.send(idle(seconds))
        assert "Waiting..." in await until(updates.get())
    sandbox.send(OutputChunk("stdout", "progress\n"))
    assert await until(updates.get()) == "progress\n"
    for seconds in (60, 120):
        sandbox.send(idle(seconds))
        assert "Waiting..." in await until(updates.get())

    async def killed(pid):
        sandbox.send(CommandExit(-9, output_complete=True))

    sandbox.kill_command.side_effect = killed
    sandbox.send(idle(180))
    result = await until(task)

    assert judge.await_count == 4
    sandbox.kill_command.assert_awaited_once_with(54321)
    assert "idle too long" in result.output


async def test_broken_stream_cancels_judge_and_reports_unknown_without_replay(monkeypatch):
    judging, cancelled = asyncio.Event(), asyncio.Event()

    async def judge(**kwargs):
        judging.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    sandbox, _, task = start_command(monkeypatch, judge)
    sandbox.send(idle())
    await until(judging.wait())
    sandbox.send(httpx.RemoteProtocolError("stream lost"))
    result = await until(task)

    assert result.metadata["failure_code"] == "command_stream_interrupted"
    assert result.metadata["outcome_unknown"] is True
    assert cancelled.is_set() and sandbox.closed.is_set()
    assert sandbox.calls == 1
    sandbox.kill_command.assert_not_called()


@pytest.mark.parametrize("failure", ["revoked", "lease_lost"])
@pytest.mark.parametrize("when", ["judging", "cancel_on_output", "cancel_on_exit", "cancel_on_stop"])
async def test_run_fence_failure_is_never_hidden_by_obsolete_judgment(monkeypatch, failure, when):
    from agent.driver import LeaseLostError
    from question.runtime import RunRevoked, RunTicket

    error = (RunRevoked(RunTicket("fixture", "fixture", 1, f"fixture-{when}"), "superseded", "request")
             if failure == "revoked" else LeaseLostError("lost"))
    judging = asyncio.Event()

    async def judge(**kwargs):
        judging.set()
        if when == "judging":
            raise error
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise error

    sandbox, ctx, task = start_command(monkeypatch, judge)
    sandbox.send(idle())
    await until(judging.wait())
    if when == "cancel_on_output":
        sandbox.send(OutputChunk("stdout", "progress\n"))
    elif when == "cancel_on_exit":
        sandbox.send(CommandExit(0, output_complete=True))
    elif when == "cancel_on_stop":
        ctx.abort.set()
    with pytest.raises(type(error)) as raised:
        await until(task)

    assert raised.value is error
    assert sandbox.closed.is_set()
    sandbox.kill_command.assert_not_called()


@pytest.mark.parametrize("failure", ["revoked", "lease_lost", "provider_error"])
async def test_judge_preserves_run_fences_instead_of_turning_them_into_kill(monkeypatch, failure):
    from agent import llm
    from agent.driver import LeaseLostError
    from core import config
    from question.runtime import RunRevoked, RunTicket

    error = {
        "revoked": lambda: RunRevoked(RunTicket("fixture", "fixture", 1, "fixture-judge-fence"), "superseded", "request"),
        "lease_lost": lambda: LeaseLostError("lost"),
        "provider_error": lambda: RuntimeError("provider unavailable"),
    }[failure]()
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(model="fixture"))
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda model: {})
    monkeypatch.setattr(llm, "metered_completion", AsyncMock(side_effect=error))

    if failure == "provider_error":
        assert await bash._judge_idle_command("fixture", "", 60, 60) == "kill"
    else:
        with pytest.raises(type(error)) as raised:
            await bash._judge_idle_command("fixture", "", 60, 60)
        assert raised.value is error
