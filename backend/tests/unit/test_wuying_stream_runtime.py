import json
from unittest.mock import AsyncMock

import pytest

from sandbox.stream_runtime import ensure_desktop_stream_output


async def test_current_guest_is_not_rewritten(monkeypatch):
    run = AsyncMock(return_value=json.dumps({'sha256': 'a' * 64, 'current': True}))
    monkeypatch.setattr('sandbox.channel.run_desktop_command', run)
    current = AsyncMock()
    await ensure_desktop_stream_output('ecd-current', authority_check=current)
    assert run.await_count == current.await_count == 1


async def test_old_guest_keeps_cas_backup_and_does_not_restart(monkeypatch):
    run = AsyncMock(side_effect=[json.dumps({'sha256': 'b' * 64, 'current': False}), '{}'])
    monkeypatch.setattr('sandbox.channel.run_desktop_command', run)
    current = AsyncMock()
    await ensure_desktop_stream_output('ecd-old', authority_check=current)
    assert current.await_count == run.await_count == 2
    command = run.call_args.args[1]
    assert 'b' * 64 in command and 'original-action_server.py' in command
    assert 'systemctl' not in command


async def test_lost_binding_prevents_publication(monkeypatch):
    run = AsyncMock(return_value=json.dumps({'sha256': 'c' * 64, 'current': False}))
    monkeypatch.setattr('sandbox.channel.run_desktop_command', run)
    current = AsyncMock(side_effect=[None, RuntimeError('binding changed')])
    with pytest.raises(RuntimeError, match='binding changed'):
        await ensure_desktop_stream_output('ecd-old', authority_check=current)
    assert run.await_count == 1
