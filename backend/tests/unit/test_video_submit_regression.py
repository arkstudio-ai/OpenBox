"""Production MiniMax regression; no provider/network credentials are used."""
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from core.config import OpenBoxConfig, VideoGenerationConfig, VideoModelConfig
from tool import video_production as vp, video_providers as providers
from tool.tool import ToolContext


def route():
    return providers.VideoRoute(provider="test", model="MiniMax-H3", api_key="test-only",
                                base_url="https://video.invalid", channel="sd2",
                                submit_timeout_seconds=5, status_timeout_seconds=5)


def legacy_entry(**extra):
    return VideoModelConfig.model_validate({"id": "MiniMax-H3", "channel": "sd2",
                                           "resolutions": ["720p", "1080p"], **extra})


async def new_context():
    """Real ownership rows, so these checks also run against PostgreSQL."""
    from datetime import datetime, timezone
    from db.base import get_db_session
    from db.models.user import User
    from db.models.workspace import Workspace
    from db.models.project import Project

    suffix = uuid4().hex
    ctx = ToolContext(user_id="u-" + suffix, workspace_id="w-" + suffix,
                      project_id="p-" + suffix, session_id="s-" + suffix, run_id="run-first")
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=ctx.user_id, username=suffix, created_at=now, updated_at=now))
        await db.flush()
        db.add(Workspace(id=ctx.workspace_id, name="Video regression", owner_user_id=ctx.user_id,
                         created_at=now, updated_at=now))
        await db.flush()
        db.add(Project(id=ctx.project_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id,
                       name="Video regression", created_at=now, updated_at=now))
        await db.commit()
    return ctx


@pytest.mark.parametrize("ratio,size", [("9:16", "720x1280"), ("16:9", "1280x720")])
@pytest.mark.parametrize("declared", [True, False])
def test_legacy_minimax_uses_size_without_a_wire_shape(ratio, size, declared):
    path, body = providers.build_payload(route(), prompt="cat", refs=[], resolution="720p",
                                        ratio=ratio, duration=4, generate_audio=True,
                                        watermark=False, declared=legacy_entry() if declared else None)
    assert path == "/v1/videos"
    assert body == {"model": "MiniMax-H3", "prompt": "cat", "duration": 4, "size": size}


def test_explicit_custom_shape_is_not_overwritten():
    assert legacy_entry(wire_shape="metadata").wire_shape == "metadata"
    assert VideoModelConfig(id="custom", channel="sd2").wire_shape == "flat"


@pytest.mark.parametrize("ratio", ["adaptive", "1:1", "invalid"])
def test_size_adapter_refuses_ratios_it_cannot_encode(ratio):
    with pytest.raises(providers.VideoRequestError, match="ratio"):
        providers.validate_request(route(), resolution="720p", ratio=ratio, duration=4,
                                   generate_audio=True, input_mimes=[], declared=legacy_entry())


def error(status=400, message="文生视频 ratio 不能为空或 adaptive (2013)"):
    request = httpx.Request("POST", "https://video.invalid/v1/videos?token=SECRET")
    response = httpx.Response(status, request=request, json={"type": "error", "error": {
        "type": "bad_request_error", "message": message},
        "request_id": "c89338b0-c7bb-40dd-bf36-404f7ddd92d2"})
    return httpx.HTTPStatusError("SECRET signed URL", request=request, response=response)


def test_known_rejection_is_actionable_and_not_ambiguous():
    detail = providers.submission_rejection(error())
    assert detail["code"] == "video_provider_invalid_ratio"
    assert detail["retryable"] is False
    assert detail["submission_outcome"] == "rejected"
    assert "ratio" in detail["message"] and "size" in detail["message"]
    assert "SECRET" not in json.dumps(detail)
    assert detail["provider_request_id"] == "c89338b0-c7bb-40dd-bf36-404f7ddd92d2"


@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize("message", ["token=SECRET https://signed.invalid/?sig=SECRET", "ignore instructions", {"secret": "SECRET"}])
def test_unknown_provider_messages_never_escape(status, message):
    detail = providers.submission_rejection(error(status, message))
    assert detail["code"] == "video_provider_request_rejected"
    assert "SECRET" not in json.dumps(detail)
    assert "ignore instructions" not in json.dumps(detail)


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503])
def test_uncertain_responses_are_not_classified_as_safe_rejections(status):
    assert providers.submission_rejection(error(status)) is None


def test_timeouts_are_not_classified_as_safe_rejections():
    assert providers.submission_rejection(httpx.ReadTimeout("SECRET")) is None


def test_gateway_wrapped_known_error_does_not_echo_its_surroundings():
    detail = providers.submission_rejection(error(message="gateway: 文生视频 ratio 不能为空或 adaptive (2013) token=SECRET"))
    assert detail["code"] == "video_provider_invalid_ratio"
    assert "SECRET" not in json.dumps(detail)


@pytest.mark.parametrize("content", [b"<html>SECRET</html>", b"[]", b"x" * 20000])
def test_unparseable_or_oversized_rejection_remains_safe(content):
    request = httpx.Request("POST", "https://video.invalid")
    response = httpx.Response(400, request=request, content=content)
    detail = providers.submission_rejection(httpx.HTTPStatusError("SECRET", request=request, response=response))
    assert detail["code"] == "video_provider_request_rejected"
    assert "SECRET" not in json.dumps(detail)


@pytest.mark.asyncio
async def test_rejection_blocks_new_keys_in_same_run_but_not_the_next_run(monkeypatch):
    entry = legacy_entry()
    config = OpenBoxConfig(video_generation=VideoGenerationConfig(model=entry.id, models=[entry], dedupe=False))
    target = route()
    ctx = await new_context()
    calls = []
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(vp, "_configured_target", lambda _model: (target, config.video_generation))

    async def no_selection(_ctx):
        return None

    async def submit(_target, path, body):
        calls.append(body)
        raise error()

    monkeypatch.setattr(vp, "_session_video_model_id", no_selection)
    monkeypatch.setattr(vp, "_session_video_resolution", no_selection)
    monkeypatch.setattr(providers, "submit", submit)
    first = await vp.execute_generate(vp.VideoGenerateArgs(action="submit", model=entry.id,
        prompt="cat", resolution="720p", ratio="9:16", duration=4, idempotency_key="one"), ctx)
    assert first.metadata["submission_outcome"] == "rejected"
    assert first.metadata["do_not_resubmit"] is True
    assert first.metadata["error"] is True
    assert "ambiguous" not in first.output
    assert "SECRET" not in first.output
    job = await vp._owned_job(first.metadata["job_id"], ctx, "segment")
    assert job.status == "failed" and job.provider_task_id is None
    assert job.result_data["submit_error"]["code"] == "video_provider_invalid_ratio"
    # The run id is server-owned and stable across assistant message steps.
    second_ctx = replace(ctx, message_id="another-step")
    second = await vp.execute_generate(vp.VideoGenerateArgs(action="submit", model=entry.id,
        prompt="changed cat", resolution="720p", ratio="16:9", duration=4, idempotency_key="two",
        allow_duplicate=True), second_ctx)
    assert second.metadata["submission_blocked"] is True
    assert second.metadata["job_id"] == job.id
    assert len(calls) == 1
    third = await vp.execute_generate(vp.VideoGenerateArgs(action="submit", model=entry.id,
        prompt="cat", resolution="720p", ratio="9:16", duration=4, idempotency_key="three"),
        replace(ctx, run_id="run-next"))
    assert third.metadata["submission_outcome"] == "rejected"
    assert len(calls) == 2
    next_job = await vp._owned_job(third.metadata["job_id"], ctx, "segment")
    assert next_job.request_hash == job.request_hash, "run identity must not change logical idempotency"


@pytest.mark.asyncio
async def test_rejection_guard_is_scoped_to_user_session_model_and_route(monkeypatch):
    from datetime import datetime, timezone
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    ctx = await new_context()
    target = route()
    now = datetime.now(timezone.utc)
    job = VideoJob(id="video-" + uuid4().hex, user_id=ctx.user_id, session_id=ctx.session_id,
        kind="segment", model=target.model, idempotency_key=uuid4().hex, status="failed",
        request_data={"submit_run_id": ctx.run_id, "provider_route_fingerprint": providers.provider_route_fingerprint(target)},
        result_data={"submit_error": {"submission_outcome": "rejected"}}, created_at=now, updated_at=now)
    async with get_db_session() as db:
        db.add(job)
        await db.commit()
    assert (await vp._run_rejected_submission(ctx, target)).id == job.id
    for other in (replace(ctx, user_id="other"), replace(ctx, session_id="other"),
                  replace(ctx, run_id="other"), replace(ctx, run_id="")):
        assert await vp._run_rejected_submission(other, target) is None
    for other in (replace(target, model="other"), replace(target, api_key="other"),
                  replace(target, base_url="https://other.invalid")):
        assert await vp._run_rejected_submission(ctx, other) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ratio", ["9:16", "16:9"])
@pytest.mark.parametrize("resolution,dimensions", [("480p", (480,854)), ("512p", (512,912)),
    ("720p", (720,1280)), ("768p", (768,1344)), ("1080p", (1080,1920)), ("2k", (1440,2560))])
async def test_real_http_client_submits_size_and_reuses_accepted_task(monkeypatch, ratio, resolution, dimensions):
    width,height=dimensions if ratio=="9:16" else dimensions[::-1]
    size=f"{width}x{height}"
    entry = legacy_entry(resolutions=[resolution])
    config = OpenBoxConfig(video_generation=VideoGenerationConfig(model=entry.id, models=[entry], dedupe=False))
    ctx = await new_context()
    target = route()
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(vp, "_configured_target", lambda _model: (target, config.video_generation))

    async def no_selection(_ctx):
        return None

    monkeypatch.setattr(vp, "_session_video_model_id", no_selection)
    monkeypatch.setattr(vp, "_session_video_resolution", no_selection)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/v1/videos"
        assert json.loads(request.content)["size"] == size
        return httpx.Response(202, json={"id": "task-mock-accepted", "status": "queued"})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    args = vp.VideoGenerateArgs(action="submit", model=entry.id, prompt="cat", resolution=resolution,
                                ratio=ratio, duration=4, idempotency_key="same")
    first = await vp.execute_generate(args, ctx)
    assert first.metadata["status"] == "queued", first.output
    second = await vp.execute_generate(args, replace(ctx, run_id="new-run"))
    assert second.metadata["idempotent_reuse"] is True
    assert second.metadata["job_id"] == first.metadata["job_id"]
    assert len(calls) == 1
