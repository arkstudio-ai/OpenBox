"""Fresh takes reach the provider; retries reconcile the original paid job."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from core.config import OpenBoxConfig, VideoGenerationConfig, VideoModelConfig
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.project import Project
from db.models.user import User
from db.models.workspace import Workspace
from tool import video_production as vp, video_providers as providers
from tool.tool import ToolContext


async def _context():
    suffix = uuid4().hex
    now = datetime.now(timezone.utc)
    ctx = ToolContext(user_id="u-" + suffix, workspace_id="w-" + suffix,
                      project_id="p-" + suffix, session_id="s-" + suffix)
    async with get_db_session() as db:
        db.add(User(id=ctx.user_id, username=suffix, created_at=now, updated_at=now))
        await db.flush()
        db.add(Workspace(id=ctx.workspace_id, name="Fresh takes", owner_user_id=ctx.user_id,
                         created_at=now, updated_at=now))
        await db.flush()
        db.add(Project(id=ctx.project_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id,
                       name="Fresh takes", created_at=now, updated_at=now))
    return ctx


@pytest.fixture
def generation(monkeypatch):
    entry = VideoModelConfig(id="MiniMax-H3", channel="sd2", resolutions=["720p"])
    config = OpenBoxConfig(video_generation=VideoGenerationConfig(
        model=entry.id, models=[entry], dedupe=True, daily_job_limit=0))
    target = providers.VideoRoute(provider="test", model=entry.id, api_key="test-only",
        base_url="https://video.invalid", channel="sd2",
        submit_timeout_seconds=5, status_timeout_seconds=5)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(vp, "_configured_target", lambda _model: (target, config.video_generation))
    monkeypatch.setattr(vp, "_session_video_model_id", AsyncMock(return_value=None))
    monkeypatch.setattr(vp, "_session_video_resolution", AsyncMock(return_value=None))
    monkeypatch.setattr(vp, "_attach_completed", AsyncMock(return_value=True))
    # Before the fix, a completed row hits the cache and copies this object
    # instead of reaching the HTTP provider below.
    copies = AsyncMock(return_value={"size": 1234, "etag": "old-video"})
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(copy=copies))
    calls = []

    def handler(request):
        assert request.method == "POST" and request.url.path == "/v1/videos"
        calls.append(request)
        return httpx.Response(202, json={"id": f"provider-take-{len(calls)}", "status": "queued"})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    return SimpleNamespace(calls=calls, copies=copies, args=vp.VideoGenerateArgs(
        action="submit", model=entry.id, prompt=f"A couple walking through a park after the rain. Case {uuid4().hex}",
        resolution="720p", ratio="9:16", duration=4, idempotency_key="park:take1:shot1"))


async def _complete(result, ctx, *, legacy_reuse=False):
    job = await vp._owned_job(result.metadata["job_id"], ctx, "segment")
    data = {"reuse": True, "reused_from_job": "another-users-private-job"} if legacy_reuse else {}
    await vp._update_job(job.id, status="completed", completed_at=datetime.now(timezone.utc),
                         provider_task_id=None if legacy_reuse else job.provider_task_id, result_data=data)
    await vp._mark_asset(job.output_asset_id, status="ready", size=1234)
    return await vp._owned_job(job.id, ctx, "segment")


@pytest.mark.parametrize("scope", ["same_conversation", "new_conversation", "another_user"])
async def test_new_take_with_identical_content_reaches_provider(generation, scope):
    ctx = await _context()
    first = await vp.execute_generate(generation.args, ctx)
    assert first.metadata["status"] == "queued", first.output
    original = await _complete(first, ctx)
    next_ctx = (await _context() if scope == "another_user" else
                replace(ctx, session_id="new-" + ctx.session_id) if scope == "new_conversation" else ctx)
    second = await vp.execute_generate(generation.args.model_copy(update={
        "idempotency_key": "park:take2:shot1"}), next_ctx)
    assert second.metadata["status"] == "queued", second.output
    fresh = await vp._owned_job(second.metadata["job_id"], next_ctx, "segment")
    assert fresh.prompt_hash == original.prompt_hash
    assert fresh.request_hash == original.request_hash
    assert fresh.provider_task_id == "provider-take-2"
    assert fresh.output_asset_id != original.output_asset_id
    assert fresh.id != original.id and not fresh.result_data.get("reuse")
    assert len(generation.calls) == 2
    generation.copies.assert_not_awaited()


@pytest.mark.parametrize("completed", [False, True])
async def test_retry_same_key_never_submits_or_copies_again(generation, completed):
    ctx = await _context()
    first = await vp.execute_generate(generation.args, ctx)
    if completed:
        await _complete(first, ctx)
    # Even an explicit second-take flag cannot defeat same-key idempotency.
    retry = await vp.execute_generate(generation.args.model_copy(update={"allow_duplicate": True}),
                                      replace(ctx, session_id="new-" + ctx.session_id))
    assert retry.metadata["idempotent_reuse"] is True
    assert retry.metadata["job_id"] == first.metadata["job_id"]
    assert len(generation.calls) == 1
    generation.copies.assert_not_awaited()


async def test_identical_running_take_is_guarded_until_explicitly_requested(generation):
    ctx = await _context()
    first = await vp.execute_generate(generation.args, ctx)
    args = generation.args.model_copy(update={"idempotency_key": "park:take2:shot1"})
    blocked = await vp.execute_generate(args, ctx)
    assert blocked.metadata["duplicate_in_flight"] is True
    assert blocked.metadata["job_id"] == first.metadata["job_id"]
    assert len(generation.calls) == 1
    second = await vp.execute_generate(args.model_copy(update={"allow_duplicate": True}), ctx)
    assert second.metadata["status"] == "queued"
    assert second.metadata["job_id"] != first.metadata["job_id"]
    assert len(generation.calls) == 2


async def test_retry_of_original_take_wins_over_an_identical_new_take_in_flight(generation):
    ctx = await _context()
    original = await vp.execute_generate(generation.args, ctx)
    await _complete(original, ctx)
    second = await vp.execute_generate(generation.args.model_copy(update={
        "idempotency_key": "park:take2:shot1"}), ctx)
    assert second.metadata["status"] == "queued"
    retry = await vp.execute_generate(generation.args, ctx)
    assert retry.metadata["idempotent_reuse"] is True
    assert retry.metadata["job_id"] == original.metadata["job_id"]
    assert len(generation.calls) == 2


@pytest.mark.parametrize("action", ["submit", "status", "list"])
async def test_legacy_copied_output_is_reported_as_reused_not_regenerated(generation, action):
    ctx = await _context()
    first = await vp.execute_generate(generation.args, ctx)
    old = await _complete(first, ctx, legacy_reuse=True)
    args = (generation.args if action == "submit" else
            vp.VideoGenerateArgs(action="status", job_id=old.id if action == "status" else None))
    result = await vp.execute_generate(args, ctx)
    assert "generation_origin=reused_existing" in result.output
    assert "new_generation=false" in result.output
    assert "another-users-private-job" not in result.output
    assert "another-users-private-job" not in str(result.metadata)
    assert len(generation.calls) == 1


async def test_attach_returns_owned_existing_asset_without_generating(generation, monkeypatch):
    from tool.tool import ToolResult
    ctx = await _context()
    first = await vp.execute_generate(generation.args, ctx)
    old = await _complete(first, ctx)
    attached = AsyncMock(return_value=ToolResult(title="Video attached", output="asset_id=" + old.output_asset_id))
    monkeypatch.setattr("tool.asset_delivery.attach_owned_asset", attached)
    result = await vp.execute_generate(vp.VideoGenerateArgs(action="attach", job_id=old.id), ctx)
    assert "new_generation=false" in result.output
    assert attached.await_args.args[0].id == old.output_asset_id
    assert len(generation.calls) == 1
    async with get_db_session() as db:
        assert (await db.get(FileAsset, old.output_asset_id)).status == "ready"
