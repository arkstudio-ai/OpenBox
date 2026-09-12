"""The executor's retained result must survive its model-facing output budget."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from agent.hooks import ToolHooks
from db.models.trajectory import SessionTrajectory, TrajectoryEvent
from permission.permission import Rule
from tests.integration.test_trajectory_storage import tracedb
from tool.tool import ToolContext, ToolInfo
from trajectory import TraceContext
from trajectory.payload import expand
from trajectory.repository import state_at


class EmptyArguments(BaseModel):
    pass


@pytest.fixture(autouse=True)
def isolate_model_output_files(tmp_path, monkeypatch):
    monkeypatch.setattr("tool.truncation._data_dir", str(tmp_path))


def context(sandbox):
    return ToolContext(
        user_id="a", session_id="session_a_1", workspace_id="ws_a", sandbox=sandbox,
        trace_context=TraceContext("a", "session_a_1", workspace_id="ws_a",
                                   run_id="raw_run", request_id="raw_request"),
    )


async def execute_tool(tool, arguments, sandbox):
    ctx = context(sandbox)
    hooks = ToolHooks(ctx.session_id, ctx.user_id,
                      config_rules=[Rule(permission="*", pattern="*", action="allow")])
    result = await hooks.wrap_execute(tool.id, tool.execute, arguments, ctx,
                                      part_id="raw_call", tool_info=tool)
    return result


async def retained(factory):
    async with factory() as db:
        root = await db.scalar(select(SessionTrajectory))
        state = await state_at(db, root)
        return state["records"]["tool:raw_call"]["data"]


async def test_bash_stream_over_preview_limit_is_replayable_before_and_after_finish(tracedb):
    from tool.bash import bash_tool

    factory, _ = tracedb
    chunks = ["A" * 60_000, "B" * 60_000, "\nunique-executor-tail"]
    async def stream(**_kwargs):
        for value in chunks:
            yield SimpleNamespace(content=value)
        yield 0
    sandbox = SimpleNamespace(execute_stream=stream, execute=AsyncMock(),
                              write_file=AsyncMock(), kill_command=AsyncMock())
    result = await execute_tool(bash_tool, {"command": "fixture-output"}, sandbox)
    assert "unique-executor-tail" not in result.output
    sandbox.execute.assert_not_awaited()
    data = await retained(factory)
    assert data["output"] == "".join(chunks)
    assert data["model_output"] == result.output
    async with factory() as db:
        root = await db.scalar(select(SessionTrajectory))
        rows = (await db.scalars(select(TrajectoryEvent).where(
            TrajectoryEvent.type == "tool.output").order_by(TrajectoryEvent.seq))).all()
        stream_rows = []
        for row in rows:
            payload = await expand(db, root.id, row.data, through_seq=root.committed_seq)
            if payload.get("stage") == "executor_stream":
                stream_rows.append(row)
        assert len(stream_rows) == len(chunks)
        # The chunk crossing 100k is retained once, including before the final
        # snapshot exists; final-state-only assertions would hide a bad delta.
        for index, row in enumerate(stream_rows):
            state = await state_at(db, root, row.seq)
            item = state["records"]["tool:raw_call"]
            assert item["data"]["output"] == "".join(chunks[:index + 1])
            assert item["status"] == "running"
            assert "model_output" not in item["data"]


@pytest.mark.parametrize("resource", [False, True])
async def test_mcp_internal_truncation_preserves_full_observed_public_body(tracedb, resource):
    from tool.mcp_tool import _make_mcp_executor, create_mcp_resource_tool

    factory, _ = tracedb
    content = "M" * 120_000 + "\nunique-mcp-tail"
    sandbox = SimpleNamespace(write_file=AsyncMock())
    if resource:
        body = {"contents": [{"text": content, "mimeType": "text/plain", "uri": "fixture://resource",
                               "private_extension": "must-not-retain-unknown-extension"}]}
        sandbox.read_mcp_resource = AsyncMock(return_value=body)
        tool = create_mcp_resource_tool()
        arguments = {"server": "fixture", "uri": "fixture://resource"}
        expected = [{key: value for key, value in body["contents"][0].items()
                     if key != "private_extension"}]
    else:
        body = {"content": [{"type": "text", "text": content}], "isError": False}
        sandbox.call_mcp_tool = AsyncMock(return_value=body)
        tool = ToolInfo(id="mcp__fixture__echo", description="Fixture MCP", parameters=EmptyArguments,
                        execute=_make_mcp_executor("fixture", "echo", "mcp__fixture__echo"))
        arguments, expected = {}, body
    result = await execute_tool(tool, arguments, sandbox)
    assert result.metadata["truncated"] is True
    assert "unique-mcp-tail" not in result.output
    data = await retained(factory)
    assert json.loads(data["output"]) == expected
    assert data["model_output"] == result.output
    assert "must-not-retain-unknown-extension" not in json.dumps(data)


async def test_web_fetch_retains_processed_body_before_tool_truncation(tracedb, monkeypatch):
    import httpx
    from tool.web_fetch import web_fetch_tool

    factory, _ = tracedb
    content = "W" * 120_000 + "unique-page-tail"
    real_client = httpx.AsyncClient
    def response(_request):
        return httpx.Response(200, headers={"Content-Type": "text/html"},
                              text="<script>private-script-code</script><p>" + content + "</p>")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(response)))
    result = await execute_tool(web_fetch_tool, {"url": "https://fixture.invalid/page"},
                                SimpleNamespace(write_file=AsyncMock()))
    assert "unique-page-tail" not in result.output
    data = await retained(factory)
    assert data["output"] == content
    assert data["model_output"] == result.output
    assert "private-script-code" not in data["output"]


async def test_bash_redaction_spans_chunks_after_chat_preview_budget(tracedb):
    from tool.bash import bash_tool

    factory, _ = tracedb
    secret = "fixture-sensitive-tail-" * 3
    chunks = ["A" * 99_984 + "\napi_", 'key="', secret, '"\ncompleted']
    async def stream(**_kwargs):
        for value in chunks:
            yield SimpleNamespace(content=value)
        yield 0
    await execute_tool(bash_tool, {"command": "fixture-output"},
                       SimpleNamespace(execute_stream=stream, write_file=AsyncMock()))
    async with factory() as db:
        root = await db.scalar(select(SessionTrajectory))
        for event in (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all():
            data = await expand(db, root.id, event.data, through_seq=root.committed_seq)
            assert "fixture-sensitive-tail" not in json.dumps(data)
        state = await state_at(db, root)
        output = state["records"]["tool:raw_call"]["data"]["output"]
        assert "[REDACTED]" in output and output.endswith('"\ncompleted')
        assert secret not in output
