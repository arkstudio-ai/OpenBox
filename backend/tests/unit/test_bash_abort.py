"""Bash reacts to the durable Agent stop signal without waiting for timeout."""

from __future__ import annotations

import asyncio

import pytest

from sandbox.client import OutputChunk
from tool.bash import BashArgs, execute
from tool.tool import ToolContext


@pytest.mark.asyncio
async def test_running_bash_closes_stream_and_returns_terminal_abort():
    started = asyncio.Event()
    closed = asyncio.Event()

    class Sandbox:
        async def execute_stream(self, **_kwargs):
            try:
                started.set()
                yield OutputChunk(type="stdout", content="START\n")
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def execute(self, **_kwargs):  # pragma: no cover - safety assertion
            raise AssertionError("an intentional stop must not retry non-streaming")

    abort = asyncio.Event()
    updates: list[str] = []

    async def update_output(output: str):
        updates.append(output)

    ctx = ToolContext(
        sandbox=Sandbox(),
        abort=abort,
        workdir="/workspace/project",
        _on_output=update_output,
    )
    running = asyncio.create_task(execute(
        BashArgs(command="sleep 120", timeout=180),
        ctx,
    ))

    await started.wait()
    await asyncio.sleep(0)
    abort.set()
    result = await asyncio.wait_for(running, timeout=1)

    assert closed.is_set()
    assert result.title == "command stopped"
    assert result.metadata == {
        "exit_code": -9,
        "error": True,
        "failure_code": "tool_aborted",
    }
    assert "START" in result.output
    assert "stopped before completion" in result.output
    assert updates[-1] == result.output
