"""Sandbox asset delivery installs its helper without a shared /tmp race."""
import re
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


def test_attach_accepts_every_relation_field_it_writes():
    """A name used in the body but missing from the signature is a NameError.

    It only fires when a caller actually reaches that line, so the whole suite
    stayed green while `share_file` was broken in production: adding
    `relation_ordinal` to the FileRelation without adding the parameter got
    past every test here because nothing called this function.
    """
    import inspect

    from sandbox.assets import attach_sandbox_image

    source = inspect.getsource(attach_sandbox_image)
    parameters = set(inspect.signature(attach_sandbox_image).parameters)

    used = set(re.findall(r"\b(relation_[a-z_]+|pin_part)\b", source))
    missing = used - parameters
    assert not missing, f"used in the body but not a parameter: {sorted(missing)}"


def test_relation_ordinal_reaches_the_file_relation():
    """Concurrent shots attach in completion order; the ordinal is what fixes it."""
    import inspect

    from sandbox.assets import attach_sandbox_image

    source = inspect.getsource(attach_sandbox_image)
    assert "ordinal=relation_ordinal" in source
