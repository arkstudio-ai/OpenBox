"""The generic parallel dispatcher must not corrupt shared desktop state."""

import pytest

from tool.batch import BatchArgs, Invocation, execute as execute_batch
from tool.computer import computer_tool
from tool.tool import ToolContext, ToolResult, define_tool


def test_unreviewed_tool_is_exclusive_by_default():
    async def harmless(_args, _ctx):
        return ToolResult(title="ok", output="ran")

    tool = define_tool(
        "unreviewed",
        description="test",
        parameters=BatchArgs,
        execute=harmless,
    )

    assert tool.parallel_safe is False


def test_reviewed_read_only_tools_are_explicitly_parallel_safe():
    from tool.glob_tool import glob_tool
    from tool.grep import grep_tool
    from tool.invalid import invalid_tool
    from tool.todo_tool import todo_read_tool
    from tool.web_fetch import web_fetch_tool
    from tool.web_search import web_search_tool

    assert all(tool.parallel_safe is True for tool in (
        glob_tool,
        grep_tool,
        invalid_tool,
        todo_read_tool,
        web_fetch_tool,
        web_search_tool,
    ))


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
        parallel_safe=True,
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


@pytest.mark.asyncio
async def test_batch_rejects_unreviewed_tool_before_dispatch(monkeypatch):
    import tool.registry as registry

    called = False

    async def unreviewed(_args, _ctx):
        nonlocal called
        called = True
        return ToolResult(title="bad", output="ran")

    tool = define_tool(
        "unreviewed_batch_tool",
        description="test",
        parameters=BatchArgs,
        execute=unreviewed,
    )
    monkeypatch.setitem(registry._tools, tool.id, tool)
    ctx = ToolContext(available_tools=frozenset({"batch", tool.id}))

    result = await execute_batch(
        BatchArgs(invocations=[Invocation(tool=tool.id, parameters={})]),
        ctx,
    )

    assert "not safe for parallel execution" in result.output
    assert called is False
