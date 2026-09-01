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


@pytest.mark.asyncio
async def test_attach_runs_end_to_end_and_carries_the_relation(monkeypatch):
    """Execute the body, not just its signature.

    Three tools share this function — share_file, view_image and the computer
    tool's screenshot — and until now nothing ever called it in a test. That
    is how a name used in the body but missing from the parameters shipped:
    `NameError` only fires on the line that runs, so 1215 green tests said
    nothing while file delivery was broken in production.
    """
    from sandbox import assets as module

    saved = {}

    class _Oss:
        bucket = "b"
        region = "cn-hangzhou"
        endpoint = "https://oss.test"

        def presign_put(self, key, mime, **kw):
            saved["key"] = key
            return "https://oss.test/put"

        async def head(self, key):
            return {"size": 1234}

    class _Sandbox:
        base_url = "http://desktop"

        async def execute(self, command, timeout=None):
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, row):
            saved["asset"] = row

        async def commit(self):
            return None

        async def execute(self, *a, **kw):
            return SimpleNamespace(first=lambda: None, scalars=lambda: SimpleNamespace(all=list))

    async def _save_part(part, **kw):
        saved["part"] = part

    monkeypatch.setattr(module, "ensure_cli", lambda *a, **kw: _noop())
    monkeypatch.setattr("core.oss.get_oss", lambda: _Oss())
    monkeypatch.setattr("db.base.get_db_session", lambda: _Session())
    monkeypatch.setattr("session.session.save_part", _save_part)
    monkeypatch.setattr(module, "_session_project", lambda *a, **kw: _none())

    ctx = SimpleNamespace(user_id="u1", session_id="s1", message_id="m1",
                          part_id="p1", sandbox=_Sandbox())

    asset_id, size = await module.attach_sandbox_image(
        ctx, "/workspace/shot1.mp4", "video/mp4", 1234, name="shot1.mp4",
        relation_kind="video_segment", relation_role="intermediate",
        relation_ordinal=7, relation_label="第 7 段",
    )

    assert asset_id.startswith("asset")
    assert size == 1234
    # The ordinal is the whole point: concurrent shots attach in completion
    # order, so without it the chat labels the last shot "第 2 段".
    assert saved["part"].relation.ordinal == 7
    assert saved["part"].relation.kind == "video_segment"


async def _noop():
    return None


async def _none():
    return None
