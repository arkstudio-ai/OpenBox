"""The formatter command quotes the file path for the sandbox shell.

The path comes from the model. Joined unquoted, a space split it into two
arguments and a metacharacter ran a second command in the sandbox.
"""
import shlex
from types import SimpleNamespace

import pytest

from lsp.format import auto_format


class _Sandbox:
    def __init__(self):
        self.commands = []

    async def execute(self, command, timeout=None):
        self.commands.append(command)
        return SimpleNamespace(exit_code=0, stderr="")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/work/my file.py",
    "/work/a.py; rm -rf ~ #.py",
    "/work/$(touch pwned).py",
    "/work/it's.py",
])
async def test_file_path_is_one_quoted_argument(path):
    sandbox = _Sandbox()
    assert await auto_format(sandbox, path) == "black"
    run = sandbox.commands[-1]
    assert shlex.split(run) == ["black", "--quiet", path]


@pytest.mark.asyncio
async def test_plain_path_command_is_unchanged():
    sandbox = _Sandbox()
    await auto_format(sandbox, "/work/main.go")
    assert sandbox.commands == ["which gofmt 2>/dev/null", "gofmt -w /work/main.go"]
