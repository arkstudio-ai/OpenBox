"""Re-sending an owned asset needs no sandbox and never emits a link."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import tool.asset_delivery as delivery
import tool.video_production as vp
from tool.share_file import ShareFileArgs, share_file_tool
from tool.tool import ToolContext
from tool.video_production import VideoGenerateArgs


def _asset(**over):
    base = dict(
        id="asset_1", user_id="u1", name="ep15.mp4", mime="video/mp4", size=1234,
        oss_key="assets/u1/asset_1/ep15.mp4", status="ready",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(**over):
    base = dict(session_id="s1", user_id="u1", workspace_id="w1", message_id="m1", part_id="call_1")
    base.update(over)
    return ToolContext(**base)


@pytest.mark.asyncio
async def test_attach_owned_asset_saves_a_final_video_card(monkeypatch):
    saved = []

    async def fake_save_part(part, is_new=False, *, user_id, run_fence=None):
        saved.append((part, is_new, user_id))

    monkeypatch.setattr("session.session.save_part", fake_save_part)
    result = await delivery.attach_owned_asset(_asset(), _ctx(), kind="video_final", role="final")

    assert result.metadata["attached"] is True and "attached=true" in result.output
    assert "http" not in result.output and "token=" not in result.output
    [(part, is_new, user_id)] = saved
    assert is_new is True and user_id == "u1"
    assert part.asset_id == "asset_1" and part.mime_type == "video/mp4" and part.size == 1234
    assert part.relation.kind == "video_final" and part.relation.role == "final"
    assert part.relation.metadata["redelivery"] is True
    assert part.message_id == "m1" and part.session_id == "s1"


@pytest.mark.asyncio
async def test_attach_without_reply_message_is_refused(monkeypatch):
    monkeypatch.setattr("session.session.save_part", AsyncMock())
    result = await delivery.attach_owned_asset(_asset(), _ctx(message_id=None), kind="video_final")
    assert result.metadata["error"] is True


def test_video_generate_attach_requires_a_reference():
    with pytest.raises(ValueError):
        VideoGenerateArgs(action="attach")
    assert VideoGenerateArgs(action="attach", asset_id="asset_1").action == "attach"
    assert VideoGenerateArgs(action="attach", job_id="video_1").job_id == "video_1"


@pytest.mark.asyncio
async def test_video_generate_attach_reattaches_by_asset_id(monkeypatch):
    calls = []

    async def fake_find(asset_id, ctx):
        return _asset(id=asset_id)

    async def fake_attach(asset, ctx, **kw):
        calls.append((asset.id, kw))
        return vp.ToolResult(title=asset.name, output="attached=true", metadata={"attached": True})

    monkeypatch.setattr(delivery, "find_owned_ready_asset", fake_find)
    monkeypatch.setattr(delivery, "attach_owned_asset", fake_attach)
    result = await vp.execute_generate(VideoGenerateArgs(action="attach", asset_id="asset_9"), _ctx())

    assert result.metadata["attached"] is True
    [(asset_id, kw)] = calls
    assert asset_id == "asset_9" and kw["kind"] == "video_final" and kw["role"] == "final"


@pytest.mark.asyncio
async def test_video_generate_attach_unknown_asset(monkeypatch):
    monkeypatch.setattr(delivery, "find_owned_ready_asset", AsyncMock(return_value=None))
    monkeypatch.setattr(vp, "_owned_job_any_kind", AsyncMock(return_value=None))
    result = await vp.execute_generate(VideoGenerateArgs(action="attach", job_id="video_x"), _ctx())
    assert result.metadata["error"] is True


def test_job_lines_carry_no_download_link():
    job = SimpleNamespace(
        id="video_1", status="completed", kind="segment", request_data={}, result_data={},
        production_id=None, segment_id=None, provider_task_id="task_1", sandbox_job_id=None, error=None, model="wan3.0-video",
    )
    text = "\n".join(vp._job_lines(job, _asset(), retry_after=None))
    assert "download_url" not in text and "token=" not in text
    assert "asset_id=asset_1" in text and "action=attach" in text


def test_share_file_takes_exactly_one_source():
    with pytest.raises(ValueError):
        ShareFileArgs()
    with pytest.raises(ValueError):
        ShareFileArgs(file_path="/workspace/a.mp4", asset_id="asset_1")
    assert ShareFileArgs(asset_id="asset_1").attach is True


@pytest.mark.asyncio
async def test_share_file_by_asset_id_needs_no_sandbox(monkeypatch):
    attach = AsyncMock(return_value=vp.ToolResult(title="ep15.mp4", output="attached=true", metadata={"attached": True}))
    monkeypatch.setattr(delivery, "find_owned_ready_asset", AsyncMock(return_value=_asset()))
    monkeypatch.setattr(delivery, "attach_owned_asset", attach)
    ctx = _ctx(sandbox=None)
    ctx.sandbox_error = {"code": "SANDBOX_SUBSCRIPTION_REQUIRED", "detail": "需要有效的付费套餐"}

    result = await share_file_tool.execute({"asset_id": "asset_1"}, ctx)

    assert result.metadata["attached"] is True
    attach.assert_awaited_once()
    assert attach.await_args.kwargs["kind"] == "video_final"


@pytest.mark.asyncio
async def test_share_file_by_path_without_sandbox_points_to_asset_id():
    ctx = _ctx(sandbox=None)
    ctx.sandbox_error = {"code": "SANDBOX_SUBSCRIPTION_REQUIRED", "detail": "需要有效的付费套餐"}
    result = await share_file_tool.execute({"file_path": "/workspace/a.mp4"}, ctx)
    assert result.title == "Sandbox unavailable"
    assert "需要有效的付费套餐" in result.output and "asset_id" in result.output
    assert result.metadata["code"] == "SANDBOX_SUBSCRIPTION_REQUIRED"
