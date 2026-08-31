"""Sandbox asset delivery installs its helper without a shared /tmp race."""
from types import SimpleNamespace

import pytest

from sandbox import assets


@pytest.mark.asyncio
async def test_ensure_cli_uses_a_unique_temporary_file():
    commands: list[str] = []

    class Client:
        async def execute(self, command: str, *, timeout: int):
            commands.append(command)
            assert timeout == 30
            return SimpleNamespace(exit_code=0, stderr="")

    assets._installed.clear()
    await assets.ensure_cli(Client(), "desktop-test")
    await assets.ensure_cli(Client(), "desktop-test")

    assert len(commands) == 1
    assert "mktemp" in commands[0]
    assert '"$obx_file_tmp"' in commands[0]
    assert "> /tmp/.obx-file" not in commands[0]
