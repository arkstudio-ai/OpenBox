"""Short successful commands must not lose stdout when exit wins the race."""
import asyncio
import json
import shlex
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_action_server_desktop_lease import server, request


@pytest.mark.parametrize("lines", [8, 500])
async def test_exited_process_drains_all_stdout_and_stderr(monkeypatch, tmp_path, lines):
    monkeypatch.setattr(server, "EventSourceResponse", lambda generator: generator)
    monkeypatch.setattr(server, "_validate_desktop_lease", AsyncMock())
    monkeypatch.setattr(server, "resource_checkpoint", AsyncMock())
    monkeypatch.setattr(server, "prepare_child", lambda argv, env: (argv, env))
    monkeypatch.setattr(server, "_emit_execute_trace", lambda *a, **k: None)
    create = asyncio.create_subprocess_exec
    async def already_exited(*args, **kwargs):
        process = await create(*args, **kwargs)
        await process.wait()
        return process
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", already_exited)
    script = f"import sys\nfor i in range({lines}):\n print('中文行-'+str(i))\n print('stderr-'+str(i),file=sys.stderr)"
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(script)
    stream = await server.execute_stream(server.ExecuteRequest(command=command,
        workdir=str(tmp_path), timeout=5, idle_timeout=1), request())
    events = [json.loads(event["data"]) async for event in stream]
    stdout = ''.join(event['content'] for event in events if event.get('type') == 'stdout')
    stderr = ''.join(event['content'] for event in events if event.get('type') == 'stderr')
    assert stdout == ''.join(f'中文行-{i}\n' for i in range(lines))
    assert stderr == ''.join(f'stderr-{i}\n' for i in range(lines))
    assert events[-1]['exit_code'] == 0


async def test_exited_shell_with_inherited_open_pipe_keeps_bounded_grace(monkeypatch, tmp_path):
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    stdout.feed_data('一\n二\n三\n'.encode())
    stderr.feed_eof()
    process = SimpleNamespace(pid=123, returncode=0, stdout=stdout, stderr=stderr,
        wait=AsyncMock(return_value=0))
    monkeypatch.setattr(server, "EventSourceResponse", lambda generator: generator)
    monkeypatch.setattr(server, "_validate_desktop_lease", AsyncMock())
    monkeypatch.setattr(server, "resource_checkpoint", AsyncMock())
    monkeypatch.setattr(server, "prepare_child", lambda argv, env: (argv, env))
    monkeypatch.setattr(server, "_emit_execute_trace", lambda *a, **k: None)
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    started = time.monotonic()
    stream = await server.execute_stream(server.ExecuteRequest(command='echo fixture',
        workdir=str(tmp_path), timeout=5, idle_timeout=1), request())
    async with asyncio.timeout(2):
        events = [json.loads(event["data"]) async for event in stream]
    assert ''.join(event['content'] for event in events if event.get('type') == 'stdout') == '一\n二\n三\n'
    assert .4 <= time.monotonic() - started < 1.5
    assert events[-1]['exit_code'] == 0
