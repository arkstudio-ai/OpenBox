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


@pytest.mark.parametrize("status", [408, 409, 500, 502, 503])
def test_uncertain_responses_are_not_classified_as_safe_rejections(status):
    # 429 moved out on 2026-10-10: a rate-limit refusal creates no task.
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


@pytest.mark.parametrize('outcome', ['accepted', 'rejected', 'timeout', 'persist_failure'])
async def test_runninghub_durable_submit_is_single_and_saves_quote(monkeypatch, outcome):
    from billing.media import quote_generation
    from tool.video_runninghub import MODEL
    entry = VideoModelConfig(id=MODEL, channel='runninghub', resolutions=['480p', '768p'],
                             duration_range=(5, 15), supports_smart_duration=False)
    config = OpenBoxConfig(video_generation=VideoGenerationConfig(model=MODEL, models=[entry],
                            default_resolution='768p', default_duration=5, dedupe=False))
    ctx = await new_context()
    target = replace(route(), model=MODEL, channel='runninghub', wire_format='runninghub_v2')
    monkeypatch.setattr('core.config.get_config', lambda: config)
    monkeypatch.setattr(vp, '_configured_target', lambda _model: (target, config.video_generation))
    if outcome == 'persist_failure':
        original_update = vp._update_job
        writes = 0
        async def fail_first_identity_write(job_id, **kwargs):
            nonlocal writes
            if kwargs.get('provider_task_id'):
                writes += 1
                if writes == 1:
                    raise RuntimeError('temporary DB failure after paid acceptance')
            return await original_update(job_id, **kwargs)
        monkeypatch.setattr(vp, '_update_job', fail_first_identity_write)
    async def no_selection(_ctx):
        return None
    monkeypatch.setattr(vp, '_session_video_model_id', no_selection)
    monkeypatch.setattr(vp, '_session_video_resolution', no_selection)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path.endswith('/text-to-video')
        if outcome == 'timeout':
            raise httpx.ReadTimeout('SECRET', request=request)
        if outcome == 'rejected':
            return httpx.Response(200, json={'errorCode': '605', 'errorMessage': 'SECRET', 'taskId': None})
        return httpx.Response(200, json={'taskId': '2102958721100820482', 'status': 'QUEUED'})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    args = vp.VideoGenerateArgs(action='submit', prompt='小狗玩球', idempotency_key='same')
    first = await vp.execute_generate(args, ctx)
    assert 'SECRET' not in first.output
    job = await vp._owned_job(first.metadata['job_id'], ctx, 'segment')
    price = quote_generation(MODEL, '768p', 5)
    assert job.request_data['billing_quote']['credits'] == str(price.credits)
    if outcome in {'accepted', 'persist_failure'}:
        assert job.provider_task_id == '2102958721100820482'
    elif outcome == 'rejected':
        assert job.status == 'failed' and job.provider_task_id is None
        assert first.metadata['submission_outcome'] == 'rejected'
        # RunningHub 605 is a balance refusal: unavailable, not charged, ask first.
        assert first.metadata['unavailable'] == 'balance'
        assert '未受理，不扣费' in first.output and 'never switch models silently' in first.output
    else:
        assert job.provider_task_id is None and first.metadata['do_not_resubmit'] is True
    again = await vp.execute_generate(args, ctx)
    assert again.metadata['job_id'] == job.id
    assert len(calls) == 1


# ── account refusals: definite, uncharged, and the person decides (2026-10-10) ──
# Seedance answered 403 at submit and the agent silently switched models 5 s
# later; MiniMax answered 429 on several days; RunningHub ran out of balance.


def _refusal(status, *, body=None, headers=None, text=None):
    request = httpx.Request("POST", "https://video.invalid/v1/videos?token=SECRET")
    content = {"text": text} if text is not None else {"json": body or {"error": {"message": "SECRET"}}}
    response = httpx.Response(status, request=request, headers=headers or {}, **content)
    return httpx.HTTPStatusError("SECRET signed URL", request=request, response=response)


@pytest.mark.parametrize("status,body,kind", [
    (401, {"error": {"message": "SECRET invalid token"}}, "permission"),
    (402, {"error": {"message": "SECRET payment required"}}, "balance"),
    (403, {"error": {"message": "SECRET model not enabled for this account"}}, "permission"),
    (403, {"error": {"message": "SECRET 用户额度不足"}}, "balance"),
    (429, {"error": {"message": "SECRET too many requests"}}, "rate_limit"),
    (429, {"error": {"code": "insufficient_quota", "message": "SECRET"}}, "balance"),
])
def test_account_refusals_are_definite_and_uncharged(status, body, kind):
    detail = providers.submission_rejection(_refusal(status, body=body))
    assert detail["submission_outcome"] == "rejected"
    assert (detail["unavailable"], detail["http_status"]) == (kind, status)
    assert detail["retryable"] is (kind == "rate_limit")
    assert "未受理，不扣费" in detail["message"]
    assert "SECRET" not in json.dumps(detail, ensure_ascii=False)


def test_an_html_refusal_page_still_classifies_by_status():
    detail = providers.submission_rejection(_refusal(403, text="<html><h1>403 Forbidden</h1>SECRET</html>"))
    assert detail["unavailable"] == "permission" and "SECRET" not in json.dumps(detail)


@pytest.mark.parametrize("body", [{"id": "task_123", "error": {"message": "slow down"}},
                                  {"data": {"task_id": "t-9"}}])
@pytest.mark.parametrize("status", [403, 429])
def test_a_refusal_that_names_a_task_stays_ambiguous(body, status):
    assert providers.submission_rejection(_refusal(status, body=body)) is None


def test_only_a_clean_rate_limit_earns_one_short_automatic_retry():
    assert providers.rate_limit_retry_delay(_refusal(429)) == providers.RATE_LIMIT_RETRY_SECONDS
    assert providers.rate_limit_retry_delay(_refusal(429, headers={"Retry-After": "2"})) == 2.0
    assert providers.rate_limit_retry_delay(_refusal(429, headers={"Retry-After": "120"})) is None
    assert providers.rate_limit_retry_delay(_refusal(429, body={"error": {"code": "insufficient_quota"}})) is None
    assert providers.rate_limit_retry_delay(_refusal(429, body={"id": "task_1"})) is None
    assert providers.rate_limit_retry_delay(_refusal(403)) is None
    assert providers.rate_limit_retry_delay(httpx.ReadTimeout("slow")) is None


def _gateway_config(key: str) -> OpenBoxConfig:
    """Two credentials, so alternatives can prefer another account's route."""
    return OpenBoxConfig.model_validate({
        "provider": {"gw": {"api_key": f"gw-{key}", "base_url": "https://gw.invalid"},
                     "other": {"api_key": f"other-{key}", "base_url": "https://other.invalid"}},
        "video_generation": {
            "model": "MiniMax-H3", "dedupe": False, "channel_providers": {"sd2": "gw"},
            "models": [
                {"id": "MiniMax-H3", "channel": "sd2", "wire_shape": "size", "resolutions": ["720p"],
                 "ratios": ["9:16", "16:9"], "duration_range": [4, 15]},
                {"id": "wan3.0-video", "channel": "sd2", "wire_shape": "metadata",
                 "resolutions": ["480p", "720p", "1080p"], "duration_range": [2, 30]},
                {"id": "wan3.0-video-prime", "name": "Wan 3.0 Prime", "channel": "sd2", "provider": "other",
                 "wire_shape": "metadata", "resolutions": ["720p", "1080p"], "duration_range": [2, 30]},
            ]},
    })


def _gateway_env(monkeypatch, handler) -> OpenBoxConfig:
    config = _gateway_config(uuid4().hex)
    monkeypatch.setattr("core.config.get_config", lambda: config)

    async def no_selection(_ctx):
        return None

    monkeypatch.setattr(vp, "_session_video_model_id", no_selection)
    monkeypatch.setattr(vp, "_session_video_resolution", no_selection)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    return config


SHOT = dict(action="submit", prompt="cat", resolution="720p", ratio="9:16", duration=5)


@pytest.mark.asyncio
async def test_permission_refusal_asks_before_switching_and_marks_the_model(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"error": {"message": "SECRET: token cannot use MiniMax-H3"}})

    _gateway_env(monkeypatch, handler)
    ctx = await new_context()
    first = await vp.execute_generate(vp.VideoGenerateArgs(**SHOT, idempotency_key="one"), ctx)

    assert first.title == "Video model unavailable"
    assert first.metadata["submission_outcome"] == "rejected"
    assert (first.metadata["unavailable"], first.metadata["code"]) == ("permission", "video_provider_permission_denied")
    assert "未受理，不扣费" in first.output and "MiniMax-H3 暂不可用" in first.output
    assert "never switch models silently" in first.output and "Ask the" in first.output
    # Another account's route first, then the same gateway; each priced.
    assert [item["model"] for item in first.metadata["alternatives"]] == ["wan3.0-video-prime", "wan3.0-video"]
    # The current T1 catalogue quotes sale prices, not the provider's cost.
    assert "wan3.0-video-prime (Wan 3.0 Prime) 720p 5s ≈10.15 credits" in first.output
    assert "wan3.0-video (wan3.0-video) 720p 5s ≈7 credits" in first.output
    assert "SECRET" not in first.output and "SECRET" not in json.dumps(first.metadata, ensure_ascii=False)
    job = await vp._owned_job(first.metadata["job_id"], ctx, "segment")
    assert job.status == "failed" and job.result_data["submit_error"]["unavailable"] == "permission"

    # The same model in the same run is refused locally, with the same question.
    again = await vp.execute_generate(vp.VideoGenerateArgs(**SHOT, idempotency_key="two"), ctx)
    assert again.metadata["submission_blocked"] is True and len(calls) == 1
    assert "Ask the" in again.output

    models = await vp.execute_generate(vp.VideoGenerateArgs(action="models"), ctx)
    rows = {line.split()[0]: line for line in models.output.splitlines() if line.startswith("  id=")}
    assert "暂不可用" in rows["id=MiniMax-H3"] and "暂不可用" not in rows["id=wan3.0-video"]
    assert models.metadata["unavailable_models"] == ["MiniMax-H3"]
    estimate = await vp.execute_generate(vp.VideoGenerateArgs(
        action="estimate", prompt="cat", resolution="720p", ratio="9:16", duration=5), ctx)
    assert estimate.metadata["model_unavailable"] is True and "暂不可用" in estimate.output


@pytest.mark.asyncio
async def test_a_clean_rate_limit_is_retried_once_then_the_person_decides(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"message": "SECRET slow down"}})

    _gateway_env(monkeypatch, handler)
    monkeypatch.setattr(providers, "RATE_LIMIT_RETRY_SECONDS", 0.01)
    ctx = await new_context()
    result = await vp.execute_generate(vp.VideoGenerateArgs(**SHOT, idempotency_key="busy"), ctx)

    assert len(calls) == 2
    assert result.metadata["unavailable"] == "rate_limit" and result.metadata["auto_retried"] is True
    assert "retried once" in result.output and "未受理，不扣费" in result.output
    job = await vp._owned_job(result.metadata["job_id"], ctx, "segment")
    assert job.result_data["submit_error"]["code"] == "video_provider_rate_limited"
    # A rate limit is not an account problem: the menu does not mark the model.
    models = await vp.execute_generate(vp.VideoGenerateArgs(action="models"), ctx)
    assert models.metadata["unavailable_models"] == []


@pytest.mark.asyncio
async def test_a_rate_limit_that_clears_on_the_retry_submits_one_task(monkeypatch):
    answers = [httpx.Response(429, json={"error": {"message": "busy"}}),
               httpx.Response(202, json={"id": "task-after-wait", "status": "queued"})]
    calls = []

    def handler(request):
        calls.append(request)
        return answers[len(calls) - 1]

    _gateway_env(monkeypatch, handler)
    monkeypatch.setattr(providers, "RATE_LIMIT_RETRY_SECONDS", 0.01)
    ctx = await new_context()
    result = await vp.execute_generate(vp.VideoGenerateArgs(**SHOT, idempotency_key="later"), ctx)

    assert result.metadata["status"] == "queued" and len(calls) == 2
    job = await vp._owned_job(result.metadata["job_id"], ctx, "segment")
    assert job.provider_task_id == "task-after-wait"


@pytest.mark.asyncio
async def test_only_recent_account_refusals_on_the_current_route_mark_a_model():
    from datetime import datetime, timedelta, timezone
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    config = _gateway_config(uuid4().hex)
    ctx = await new_context()
    now = datetime.now(timezone.utc)

    def refused(model, kind, *, minutes, route_config=config):
        fingerprint = providers.provider_route_fingerprint(providers.resolve_route(model, route_config))
        at = now - timedelta(minutes=minutes)
        return VideoJob(id="video-" + uuid4().hex, user_id=ctx.user_id, session_id=ctx.session_id,
                        kind="segment", model=model, idempotency_key=uuid4().hex, status="failed",
                        request_data={"provider_route_fingerprint": fingerprint},
                        result_data={"submit_error": {"submission_outcome": "rejected", "unavailable": kind}},
                        created_at=at, updated_at=at)

    async with get_db_session() as db:
        db.add_all([
            refused("MiniMax-H3", "permission", minutes=5),
            refused("wan3.0-video", "rate_limit", minutes=1),           # transient, not a mark
            refused("wan3.0-video-prime", "balance", minutes=45),       # outside the window
        ])
        await db.commit()

    marks = await vp._recently_unavailable_models(config)
    assert set(marks) == {"MiniMax-H3"} and marks["MiniMax-H3"]["reason"] == "permission"
    assert "5 分钟前" in vp._availability_text(marks["MiniMax-H3"])
    # A replaced credential is another route: the old refusal no longer applies.
    assert await vp._recently_unavailable_models(_gateway_config(uuid4().hex)) == {}
