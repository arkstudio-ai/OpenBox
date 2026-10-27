"""A successful child exit must not hide missing command output."""
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from sandbox.client import CommandExit, OutputChunk, SandboxClient
from tool.bash import BashArgs, execute
from tool.tool import ToolContext


def client_for(monkeypatch, payload):
    client = SandboxClient('fixture', 80, 'test-key')

    @asynccontextmanager
    async def connection(**kwargs):
        transport = httpx.MockTransport(lambda req: httpx.Response(200,
            headers={'content-type': 'text/event-stream'}, content=payload))
        async with httpx.AsyncClient(base_url='http://fixture', transport=transport) as connection:
            yield connection

    monkeypatch.setattr(client, '_client', connection)
    return client


@pytest.mark.parametrize('complete', [True, False, None])
async def test_stream_carries_integrity_and_preserves_legacy_int_contract(monkeypatch, complete):
    result = {'exit_code': 0}
    if complete is not None:
        result.update(output_complete=complete,
                      output_incomplete_reason=None if complete else 'pipe_drain_deadline')
    payload = 'data: ' + json.dumps({'type': 'stdout', 'content': '一\n二\n'}) + '\n\n'
    payload += 'data: ' + json.dumps(result) + '\n\n'
    client = client_for(monkeypatch, payload)
    items = [item async for item in client.execute_stream('fixture')]
    assert items[0].content == '一\n二\n'
    assert isinstance(items[-1], int) and items[-1] == 0
    assert items[-1].output_complete is complete


async def test_malformed_output_cannot_be_silently_ignored_before_success(monkeypatch):
    client = client_for(monkeypatch, 'data: {damaged\n\ndata: {"exit_code": 0}\n\n')
    with pytest.raises(httpx.RemoteProtocolError, match='malformed output'):
        _ = [item async for item in client.execute_stream('fixture')]


@pytest.mark.parametrize('complete', [True, False, None])
async def test_bash_reports_partial_output_without_reexecuting_command(complete):
    class Sandbox:
        calls = 0

        async def execute_stream(self, **kwargs):
            self.calls += 1
            yield OutputChunk('stdout', '{\n')
            yield CommandExit(0, output_complete=complete,
                              output_incomplete_reason='pipe_drain_deadline' if complete is False else None)

    sandbox = Sandbox()
    ctx = ToolContext(sandbox=sandbox)
    ctx.update_output = AsyncMock()
    result = await execute(BashArgs(command='fixture'), ctx)
    assert sandbox.calls == 1
    assert result.metadata['exit_code'] == 0
    assert result.metadata['output_complete'] is complete
    assert ('output is incomplete' in result.output) is (complete is False)
    assert ctx._trajectory_full_tool_output == '{\n'


async def test_bash_preview_truncation_is_distinct_from_complete_transport(monkeypatch):
    from tool import bash
    monkeypatch.setattr(bash, 'MAX_STREAM_OUTPUT', 5)

    class Sandbox:
        async def execute_stream(self, **kwargs):
            yield OutputChunk('stdout', 'abcdef')
            yield CommandExit(0, output_complete=True)

    ctx = ToolContext(sandbox=Sandbox())
    ctx.update_output = AsyncMock()
    result = await execute(BashArgs(command='fixture'), ctx)
    assert result.metadata['stream_output_complete'] is True
    assert result.metadata['output_complete'] is False
    assert result.metadata['output_truncated'] is True
    assert ctx._trajectory_full_tool_output == 'abcdef'
