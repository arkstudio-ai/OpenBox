"""The generic parallel dispatcher must not corrupt shared desktop state."""

import pytest

from tool.batch import BatchArgs, Invocation, execute as execute_batch
from tool.computer import computer_tool
from tool.tool import ToolContext, ToolResult, define_tool


@pytest.mark.asyncio
async def test_generic_batch_rejects_computer(monkeypatch):
    import tool.registry as registry

    monkeypatch.setitem(registry._tools, "computer", computer_tool)
    ctx = ToolContext(available_tools=frozenset({"batch", "computer"}))

    result = await execute_batch(
        BatchArgs(invocations=[
            Invocation(tool="computer", parameters={"action": "screenshot"}),
        ]),
        ctx,
    )

    assert "not safe for parallel execution" in result.output
    assert "computer(action='batch'" in result.output


@pytest.mark.asyncio
async def test_nested_tool_cannot_escape_agent_allowlist(monkeypatch):
    import tool.registry as registry

    called = False

    async def dangerous(_args, _ctx):
        nonlocal called
        called = True
        return ToolResult(title="bad", output="ran")

    tool = define_tool(
        "not_exposed",
        description="test",
        parameters=BatchArgs,
        execute=dangerous,
    )
    monkeypatch.setitem(registry._tools, "not_exposed", tool)
    ctx = ToolContext(available_tools=frozenset({"batch"}))

    result = await execute_batch(
        BatchArgs(invocations=[Invocation(tool="not_exposed", parameters={})]),
        ctx,
    )

    assert "not available to the current agent" in result.output
    assert called is False


@pytest.mark.asyncio
async def test_nested_tool_runs_permission_callback(monkeypatch):
    import tool.registry as registry

    called = False

    async def harmless(_args, _ctx):
        nonlocal called
        called = True
        return ToolResult(title="ok", output="ran")

    tool = define_tool(
        "nested_test",
        description="test",
        parameters=BatchArgs,
        execute=harmless,
    )
    monkeypatch.setitem(registry._tools, "nested_test", tool)

    async def deny(tool_id, args):
        assert tool_id == "nested_test"
        assert args == {}
        return ToolResult(title="Permission denied", output="blocked", metadata={"blocked": True})

    ctx = ToolContext(
        available_tools=frozenset({"batch", "nested_test"}),
        _authorize_tool=deny,
    )
    result = await execute_batch(
        BatchArgs(invocations=[Invocation(tool="nested_test", parameters={})]),
        ctx,
    )

    assert "Permission denied" in result.output
    assert called is False
