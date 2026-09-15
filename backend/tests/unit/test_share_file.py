"""File delivery outcomes reach the real tool hooks and trajectory spool."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from agent.hooks import ToolHooks
from core.oss import OssNotConfigured
from question.runtime import RunRevoked
from tests.unit.trajectory_producer_support import recording_spool  # noqa: F401
from tool.share_file import share_file_tool
from tool.tool import ToolContext
from trajectory import TraceContext


@pytest.fixture
def delivery(monkeypatch):
    probe = AsyncMock(return_value=SimpleNamespace(exit_code=0, stdout="12\ntext/plain\n", stderr=""))
    upload = AsyncMock(return_value=("asset_shared", 12))
    oss = Mock(return_value=object())
    monkeypatch.setattr("core.oss.get_oss", oss)
    monkeypatch.setattr("sandbox.assets.attach_sandbox_image", upload)
    ctx = ToolContext(
        session_id="s1", user_id="u1", workspace_id="w1", message_id="m1",
        sandbox=SimpleNamespace(execute=probe),
        trace_context=TraceContext("u1", "s1", workspace_id="w1", run_id="run1"),
    )
    return SimpleNamespace(ctx=ctx, probe=probe, upload=upload, oss=oss)


async def share(ctx, *, attach=True):
    hooks = ToolHooks(ctx.session_id, ctx.user_id)
    hooks.authorize_tool = AsyncMock(return_value=None)
    return await hooks.wrap_execute(
        "share_file", share_file_tool.execute, {"file_path": "report.txt", "attach": attach}, ctx,
        part_id="call_share", tool_info=share_file_tool,
    )


@pytest.mark.parametrize("failure,title", [
    ("unconfigured", "share_file unavailable"),
    ("unreadable", "Cannot read /workspace/report.txt"),
    ("oversized", "File too large: /workspace/report.txt"),
    ("upload", "Upload failed: /workspace/report.txt"),
])
async def test_delivery_failure_is_recorded_as_failed(delivery, recording_spool, failure, title):
    if failure == "unconfigured":
        delivery.oss.side_effect = OssNotConfigured("OSS_BUCKET is not set")
    elif failure == "unreadable":
        delivery.probe.return_value = SimpleNamespace(exit_code=1, stdout="", stderr="Permission denied")
    elif failure == "oversized":
        delivery.probe.return_value.stdout = f"{512 * 1024 * 1024 + 1}\ntext/plain\n"
    else:
        delivery.upload.side_effect = RuntimeError("OSS returned 503")

    result = await share(delivery.ctx)

    assert result.title == title
    [finished] = recording_spool.events("tool.finished")
    assert finished["call_id"] == "call_share"
    assert finished["data"]["status"] == "failed"
    assert finished["data"]["model_output"] == result.output
    if failure == "upload":
        delivery.upload.assert_awaited_once()
    else:
        delivery.upload.assert_not_awaited()


@pytest.mark.parametrize("attach", [True, False])
async def test_successful_delivery_is_recorded_as_completed(delivery, recording_spool, attach):
    result = await share(delivery.ctx, attach=attach)

    assert result.metadata["asset_id"] == "asset_shared"
    assert result.metadata["attached"] is attach
    assert not result.metadata.get("error")
    delivery.upload.assert_awaited_once_with(
        delivery.ctx, "/workspace/report.txt", "text/plain", 12, name="report.txt",
        relation_kind="shared_file", relation_role="result", relation_label="report.txt", pin_part=attach,
    )
    [finished] = recording_spool.events("tool.finished")
    assert finished["data"]["status"] == "completed"


async def test_revoked_delivery_remains_cancelled(delivery, recording_spool):
    revoked = RunRevoked(SimpleNamespace(run_id=f"revoked_share_{uuid4().hex}"), "superseded", "tool")
    delivery.upload.side_effect = revoked

    with pytest.raises(RunRevoked) as raised:
        await share(delivery.ctx)

    assert raised.value is revoked
    [finished] = recording_spool.events("tool.finished")
    assert finished["data"]["status"] == "cancelled"
