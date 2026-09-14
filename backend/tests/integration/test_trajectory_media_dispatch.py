"""Real media adapters, durable dispatch identities, retained inputs and job polls."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from agent.trajectory import service_scope, retain_derived_media_inputs
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.trajectory import SessionTrajectory, TrajectoryEvent
from db.models.video_job import VideoJob
from tests.integration.test_trajectory_storage import tracedb
from tool.tool import ToolContext
from trajectory import TraceContext
from trajectory.jobs import CONTEXT_KEY
from trajectory.payload import read_payload
from trajectory.types import RecordingError, now


async def media_fixture(factory, monkeypatch, *, mime="image/png"):
    monkeypatch.setenv("BILLING_MODE", "off")
    monkeypatch.setattr("trajectory.artifacts.read_asset_bytes", AsyncMock(return_value=b"owned input bytes"))
    trace = TraceContext("a", "session_a_1", workspace_id="ws_a", turn_id="media_turn",
        run_id="media_run", agent_id="media_agent", request_id="parent_chat", call_id="media_call",
        message_id="media_message")
    ctx = ToolContext(user_id="a", workspace_id="ws_a", session_id="session_a_1",
                      message_id="media_message", trace_context=trace)
    ctx._trajectory_billing_event_id = "parent_chat_ledger"
    async with factory.begin() as db:
        db.add(Message(id=ctx.message_id, session_id=ctx.session_id, user_id="a", role="assistant",
                       summary=False, created_at=now()))
        db.add(FileAsset(id="source_media", user_id="a", workspace_id="ws_a", session_id=ctx.session_id,
            name="source", oss_key="assets/a/source", mime=mime, size=17, status="ready", is_deleted=False,
            created_at=now()))
        job = VideoJob(id="media_job", user_id="a", session_id=ctx.session_id, kind="segment",
            idempotency_key="media_fixture", status="dispatching", model="fixture", attempt=0,
            request_data={CONTEXT_KEY: trace.to_dict()}, result_data={}, created_at=now(), updated_at=now())
        db.add(job)
    return ctx, job, "https://fixture.oss.test/assets/a/source?Signature=temporary-media-secret"


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize("channel", ["ark", "bossip", "sd2", "task"])
async def test_video_dispatch_captures_final_wire_and_poll_does_not_create_request(tracedb, monkeypatch, channel):
    from tool import video_production as vp, video_providers as providers
    factory, _ = tracedb
    ctx, job, media_url = await media_fixture(factory, monkeypatch)
    route = providers.VideoRoute(provider="fixture", model="fixture-model", api_key="private-provider-key",
        base_url="https://provider.invalid", submit_timeout_seconds=10, status_timeout_seconds=10,
        channel="ark" if channel == "bossip" else channel,
        wire_format="bossip_videos" if channel == "bossip" else "tokenspace_contents")
    if channel == "bossip":
        monkeypatch.setattr(providers, "declared_model", lambda *_args: SimpleNamespace(wire_shape="metadata"))
    refs = [{"kind": "image", "url": media_url, "role": "reference_image"}]
    if channel in {"ark", "bossip"}:
        body = {"model": route.model, "content": [{"type": "text", "text": "create a portrait"},
            *vp._ark_reference_content(refs)], "resolution": "720p", "ratio": "9:16", "duration": 5,
            "generate_audio": True, "watermark": False}
        path = None
    else:
        path, body = providers.build_payload(route, prompt="create a portrait", refs=refs,
            resolution="720p", ratio="9:16", duration=5, generate_audio=True, watermark=False)
    sent = []
    def handler(request):
        sent.append((request.method, json.loads(request.content) if request.content else None))
        return httpx.Response(202 if request.method == "POST" else 200,
                              json={"id": "provider_job", "status": "queued"})
    mock_http(monkeypatch, handler)
    async with service_scope(ctx, job=job, asset_urls={media_url: "source_media"}):
        if path is None:
            await vp._provider_submit(route, body)
        else:
            await providers.submit(route, path, body)
        await vp._provider_status(route, "provider_job")
        await vp._provider_status(route, "provider_job")
    assert [method for method, _ in sent] == ["POST", "GET", "GET"]
    assert media_url in json.dumps(sent[0][1])  # Actual HTTP business input is unchanged.
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        prepared = [event for event in events if event.type == "request.prepared"]
        assert len(prepared) == 1
        request = prepared[0]
        assert request.data["capture_level"] == "provider_wire"
        assert request.data["billing_usage_event_id"] is None
        assert request.context["parent_call_id"] == "media_call"
        assert request.context["turn_id"] == "media_turn"
        snapshot = request.data["input"]["input"]
        captured = snapshot["business_body"]
        assert captured["model"] == sent[0][1]["model"]
        assert captured.get("prompt") == sent[0][1].get("prompt")
        assert "trajectory-media:" in json.dumps(captured)
        assert "temporary-media-secret" not in json.dumps([event.data for event in events])
        assert "private-provider-key" not in json.dumps([event.data for event in events])
        reference = next(iter(snapshot["media_inputs"].values()))["payload"]
        trajectory = await db.scalar(select(SessionTrajectory))
        _, retained = await read_payload(db, trajectory.id, reference["payload_id"], through_seq=trajectory.committed_seq)
        assert retained == b"owned input bytes"
        assert len([event for event in events if event.type == "request.delta"]) == 1
        polls = [event for event in events if event.type == "job.progress" and event.data.get("operation") == "video_status"]
        assert len(polls) == 2 and {event.request_id for event in polls} == {request.request_id}
        terminal = next(event for event in events if event.type == "request.finished")
        assert terminal.data["finish_reason"] == "accepted" and terminal.data["ttft_ms"] is None
        saved = await db.get(VideoJob, job.id)
        assert saved.status == "dispatching"  # An accepted HTTP request is not a finished video.
        assert saved.request_data["_trajectory_request_ids"] == [request.request_id]
        assert saved.request_data[CONTEXT_KEY] == ctx.trace_context.to_dict()


@pytest.mark.parametrize("engine", ["dashscope", "openai_url"])
async def test_asr_dispatch_and_observed_results_keep_one_request(tracedb, monkeypatch, engine):
    from tool.video_production import VideoTranscriptionTarget, _provider_transcribe
    factory, _ = tracedb
    ctx, job, audio_url = await media_fixture(factory, monkeypatch, mime="audio/mpeg")
    target = VideoTranscriptionTarget(engine=engine, model="fun-asr", api_key="private-asr-key",
        base_url="https://provider.invalid", timeout_seconds=10, poll_interval_seconds=0,
        similarity_threshold=0.9)
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            if engine == "openai_url":
                return httpx.Response(200, json={"text": "captured transcript", "duration_ms": 5000})
            return httpx.Response(200, json={"output": {"task_id": "asr_task", "task_status": "PENDING"}})
        if request.url.path.endswith("/asr_task"):
            return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED", "results": [
                {"subtask_status": "SUCCEEDED", "transcription_url": "https://provider.invalid/transcript"}]}})
        return httpx.Response(200, json={"transcripts": [{"text": "captured transcript"}],
                                        "properties": {"original_duration_in_milliseconds": 5000}})
    mock_http(monkeypatch, handler)
    async with service_scope(ctx, job=job, asset_urls={audio_url: "source_media"}):
        result = await _provider_transcribe(target, audio_url)
    assert result["text"] == "captured transcript" and result["duration_ms"] == 5000
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        assert len([event for event in events if event.type == "request.prepared"]) == 1
        assert len([event for event in events if event.type == "request.delta"]) == 1
        observations = [event for event in events if event.type == "job.progress"
                        and event.data.get("operation", "").startswith("transcription_")]
        assert len(observations) == (2 if engine == "dashscope" else 0)
        assert "captured transcript" in json.dumps([event.data for event in events])
        assert "temporary-media-secret" not in json.dumps([event.data for event in events])
        assert len([request for request in requests if request.method == "POST"]) == 1


async def test_ims_captures_final_sdk_query_and_polls_saved_job_context(tracedb, monkeypatch):
    from video import ims_client
    factory, _ = tracedb
    ctx, job, media_url = await media_fixture(factory, monkeypatch)
    timeline = json.dumps({"VideoTracks": [{"VideoTrackClips": [{"MediaURL": media_url, "Width": 720}]}]})
    observed = []
    async def call_api(action, request, runtime):
        observed.append((action.action, request.query))
        return ({"body": {"JobId": "ims_job"}} if action.action == "SubmitMediaProducingJob" else
                {"body": {"MediaProducingJob": {"JobId": "ims_job", "Status": "Processing"}}})
    monkeypatch.setattr("alibabacloud_tea_openapi.client.Client", lambda _config: SimpleNamespace(call_api_async=call_api))
    monkeypatch.setattr("core.aliyun.load_credentials", lambda: {"access_key_id": "private-access", "access_key_secret": "private-secret"})
    monkeypatch.setattr(ims_client, "_region", lambda: "cn-shanghai")
    monkeypatch.setattr(ims_client, "_endpoint", lambda _region: "ice.invalid")
    async with service_scope(ctx, job=job, asset_urls={media_url: "source_media"}):
        result = await ims_client.submit_media_producing_job(timeline=timeline,
            output_media_config='{"MediaURL":"https://output.invalid/render.mp4"}',
            client_token="idempotent-fixture", user_data='{"openbox_job":"media_job"}')
    assert result == "ims_job"
    async with factory() as db:
        saved = await db.get(VideoJob, job.id)
    from video.job_recovery import _recovery_context
    async with service_scope(_recovery_context(saved), job=saved):
        await ims_client.get_media_producing_job(result)
    assert observed[0][1]["Timeline"] == timeline
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        prepared = [event for event in events if event.type == "request.prepared"]
        assert len(prepared) == 1 and prepared[0].data["capture_level"] == "adapter_input"
        assert prepared[0].data["sdk_internal_attempts"] == "not_observed"
        assert "trajectory-media:" in prepared[0].data["input"]["input"]["business_body"]["Timeline"]
        poll = next(event for event in events if event.type == "job.progress" and event.data.get("operation") == "GetMediaProducingJob")
        assert poll.request_id == prepared[0].request_id and poll.context["turn_id"] == "media_turn"
        assert "private-access" not in json.dumps([event.data for event in events])
        assert "private-secret" not in json.dumps([event.data for event in events])


async def test_derived_media_remains_revocable_with_original_video(tracedb, monkeypatch):
    from tool.video_production import VideoTranscriptionTarget, _provider_transcribe
    from trajectory.artifacts import revoke_asset_in_tx
    factory, _ = tracedb
    ctx, job, _ = await media_fixture(factory, monkeypatch, mime="video/mp4")
    staged = "https://fixture.oss.test/analysis/a/media_job/audio.mp3?Signature=temporary-media-secret"
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(host="fixture.oss.test"))
    media = await retain_derived_media_inputs(ctx, [staged], "source_media")
    reference = media[staged]["payload"]
    mock_http(monkeypatch, lambda _request: httpx.Response(200, json={"text": "derived transcript"}))
    target = VideoTranscriptionTarget(engine="openai_url", model="asr", api_key="fixture",
        base_url="https://provider.invalid", timeout_seconds=10, poll_interval_seconds=0, similarity_threshold=0.9)
    async with service_scope(ctx, job=job, retained_media=media):
        await _provider_transcribe(target, staged)
    async with factory.begin() as db:
        source = await db.get(FileAsset, "source_media")
        source.is_deleted = True
        await revoke_asset_in_tx(db, source.id)
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        with pytest.raises(FileNotFoundError):
            await read_payload(db, trajectory.id, reference["payload_id"], through_seq=trajectory.committed_seq)
        from trajectory.repository import state_at
        state = await state_at(db, trajectory)
        assert "owned input bytes" not in json.dumps(state)
        assert "deleted" in json.dumps(state)


async def test_media_recording_failure_prevents_actual_submit(tracedb, monkeypatch):
    from tool.video_production import VideoProviderTarget, _provider_submit
    factory, _ = tracedb
    ctx, job, _ = await media_fixture(factory, monkeypatch)
    dispatched = []
    mock_http(monkeypatch, lambda _request: dispatched.append(True))
    monkeypatch.setattr("trajectory.artifacts.retain_request_media_in_tx", AsyncMock(side_effect=RecordingError("fixture")))
    target = VideoProviderTarget(provider="fixture", model="fixture", api_key="fixture",
        base_url="https://provider.invalid", submit_timeout_seconds=10, status_timeout_seconds=10)
    with pytest.raises(RecordingError):
        async with service_scope(ctx, job=job):
            await _provider_submit(target, {"model": "fixture", "content": [{"type": "text", "text": "generate"}]})
    assert not dispatched


async def test_explicit_media_retry_has_a_new_dispatch_id(tracedb, monkeypatch):
    from tool.video_production import VideoProviderTarget, _provider_submit
    factory, _ = tracedb
    ctx, job, _ = await media_fixture(factory, monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        return (httpx.Response(400, json={"error": {"code": "bad_request", "message": "provider refused"}})
                if len(calls) == 1 else httpx.Response(202, json={"id": "accepted_job", "status": "queued"}))
    mock_http(monkeypatch, handler)
    route = VideoProviderTarget(provider="fixture", model="fixture", api_key="fixture",
        base_url="https://provider.invalid", submit_timeout_seconds=10, status_timeout_seconds=10)
    body = {"model": "fixture", "content": [{"type": "text", "text": "generate"}]}
    with pytest.raises(httpx.HTTPStatusError):
        async with service_scope(ctx, job=job):
            await _provider_submit(route, body)
    async with service_scope(ctx, job=job):
        await _provider_submit(route, body)
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        starts = [event for event in events if event.type == "request.started"]
        terminals = [event for event in events if event.type == "request.finished"]
        assert len(starts) == len(terminals) == 2
        assert len({event.request_id for event in starts}) == 2
        assert [event.data["status"] for event in terminals] == ["failed", "completed"]
        assert len([event for event in events if event.type == "request.delta"]) == 2
        saved = await db.get(VideoJob, job.id)
        assert saved.request_data["_trajectory_request_ids"] == [event.request_id for event in starts]
        assert len(calls) == 2
