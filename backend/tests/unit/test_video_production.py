"""Video tools validate billable and render calls conservatively."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from core.markdown import parse_frontmatter
import tool.video_production as video_mod
from tool.video_production import (
    VideoProviderTarget,
    VideoTranscriptionTarget,
    VideoGenerateArgs,
    VideoRenderArgs,
    _auth_header,
    _bossip_video_payload,
    _provider_status,
    _provider_submit,
    _provider_video_url,
    _provider_transcribe,
    _presigned_provider_refs,
    _resolve_generation_inputs,
    _validate_generation,
    video_generate_tool,
    video_transcribe_tool,
    video_render_tool,
)
from tool.video_providers import provider_route_fingerprint


@pytest.mark.asyncio
async def test_dashscope_transcription_submit_poll_and_result(monkeypatch):
    import httpx

    class FakeResponse:
        def __init__(self, data):
            self._data = data
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            assert url.endswith("/api/v1/services/audio/asr/transcription")
            assert headers["X-DashScope-Async"] == "enable"
            assert json == {
                "model": "fun-asr",
                "input": {"file_urls": ["https://oss.test/speech.mp3"]},
                "parameters": {"channel_id": [0], "language_hints": ["zh"]},
            }
            return FakeResponse({"output": {"task_id": "task-1", "task_status": "PENDING"}})

        async def get(self, url, *, headers=None):
            if url.endswith("/api/v1/tasks/task-1"):
                return FakeResponse(
                    {
                        "output": {
                            "task_status": "SUCCEEDED",
                            "results": [
                                {
                                    "subtask_status": "SUCCEEDED",
                                    "transcription_url": "https://result.test/transcript.json",
                                }
                            ],
                        }
                    }
                )
            assert url == "https://result.test/transcript.json"
            return FakeResponse(
                {
                    "properties": {"original_duration_in_milliseconds": 6123},
                    "transcripts": [{"text": "上海的早晨"}, {"text": "从外滩开始。"}],
                }
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    target = VideoTranscriptionTarget(
        engine="dashscope",
        model="fun-asr",
        api_key="test-only",
        base_url="https://dashscope.aliyuncs.com",
        timeout_seconds=30,
        poll_interval_seconds=0.25,
        similarity_threshold=0.9,
    )

    result = await _provider_transcribe(target, "https://oss.test/speech.mp3")

    assert result == {
        "text": "上海的早晨\n从外滩开始。",
        "duration_ms": 6123,
        "model": "fun-asr",
        "provider": "dashscope",
    }


def test_independent_segment_tools_are_parallel_safe_but_render_is_not():
    assert video_generate_tool.parallel_safe is True
    assert video_transcribe_tool.parallel_safe is True
    assert video_render_tool.parallel_safe is False

def test_billable_submit_requires_idempotency_key():
    with pytest.raises(ValidationError, match="idempotency_key"):
        VideoGenerateArgs(action="submit", prompt="人物说一句话")
    with pytest.raises(ValidationError, match="idempotency_key"):
        VideoRenderArgs(action="submit", segment_assets=["asset-1"])


def test_generation_schema_exposes_only_control_plane_fields():
    properties = set(VideoGenerateArgs.model_json_schema()["properties"])

    assert properties == {
        "action",
        "job_id",
        "production_id",
        "segment_id",
        "idempotency_key",
        "wait_seconds",
        "after_version",
        "wait_iteration",
    }


def test_generation_wait_schema_and_runtime_share_the_optional_iteration_default():
    schema = VideoGenerateArgs.model_json_schema()
    args = VideoGenerateArgs(action="wait", job_id="video_1", after_version=0)

    assert args.wait_iteration == 0
    assert "wait_iteration" not in schema["required"]


@pytest.mark.asyncio
async def test_generation_wait_timeout_returns_a_versioned_running_snapshot(monkeypatch):
    updated_at = datetime.now(timezone.utc)
    version = int(updated_at.timestamp() * 1_000_000)
    job = SimpleNamespace(
        id="video_1",
        kind="segment",
        status="in_progress",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="provider_1",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=updated_at,
    )
    target = SimpleNamespace(model="video-model-1", channel="ark")
    job.request_data = {
        "provider_route_fingerprint": provider_route_fingerprint(target),
        "provider_wire_format": "tokenspace_contents",
    }
    settings = SimpleNamespace(poll_interval_seconds=5)
    provider_calls = 0

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def fake_status(_target, _provider_task_id):
        nonlocal provider_calls
        provider_calls += 1
        return {"status": "processing"}

    async def fake_update_output(_message):
        return None

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (target, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", fake_status)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(
            action="wait",
            job_id=job.id,
            wait_seconds=0,
            after_version=version,
            wait_iteration=3,
        ),
        SimpleNamespace(user_id="user_1", update_output=fake_update_output),
    )

    assert provider_calls == 0
    assert result.metadata == {
        "job_id": job.id,
        "status": "in_progress",
        "asset_id": None,
        "attached": False,
        "ambiguous_submit": False,
        "still_running": True,
        "timed_out": True,
        "version": version,
        "retry_after_seconds": 5,
    }
    assert "still_running=true" in result.output
    assert f"version={version}" in result.output
    assert f"next_wait_after_version={version} next_wait_iteration=4" in result.output


@pytest.mark.asyncio
async def test_generation_wait_pauses_inline_polling_without_cancelling_or_resubmitting(
    monkeypatch,
):
    updated_at = datetime.now(timezone.utc)
    version = int(updated_at.timestamp() * 1_000_000)
    job = SimpleNamespace(
        id="video_slow_provider",
        kind="segment",
        status="in_progress",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="provider_1",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=updated_at,
    )
    target = SimpleNamespace(model="video-model-1", channel="ark")
    job.request_data = {
        "provider_route_fingerprint": provider_route_fingerprint(target),
        "provider_wire_format": "tokenspace_contents",
    }
    settings = SimpleNamespace(poll_interval_seconds=5)

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def fake_update_output(_message):
        return None

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (target, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(
            action="wait",
            job_id=job.id,
            wait_seconds=0,
            after_version=version,
            wait_iteration=video_mod._MAX_INLINE_GENERATION_WAITS,
        ),
        SimpleNamespace(user_id="user_1", update_output=fake_update_output),
    )

    assert result.title == "Video still processing"
    assert result.metadata["status"] == "in_progress"
    assert result.metadata["still_running"] is True
    assert result.metadata["polling_paused"] is True
    assert result.metadata["do_not_resubmit"] is True
    assert result.metadata["next_check_after_seconds"] == 60
    assert "polling_paused=true" in result.output
    assert "stop this assistant run now" in result.output
    assert "do not cancel" in result.output
    assert "next_wait_iteration" not in result.output


@pytest.mark.asyncio
async def test_generation_wait_provider_timeout_returns_running_snapshot(monkeypatch):
    updated_at = datetime.now(timezone.utc)
    job = SimpleNamespace(
        id="video_provider_timeout",
        kind="segment",
        status="in_progress",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="provider_1",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=updated_at,
    )
    target = SimpleNamespace(model="video-model-1", channel="ark")
    job.request_data = {
        "provider_route_fingerprint": provider_route_fingerprint(target),
        "provider_wire_format": "tokenspace_contents",
    }
    settings = SimpleNamespace(poll_interval_seconds=5)

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def fake_status(_target, _provider_task_id):
        raise TimeoutError("provider status timed out")

    async def fake_update_output(_message):
        return None

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (target, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", fake_status)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(
            action="wait",
            job_id=job.id,
            after_version=0,
            wait_iteration=0,
            wait_seconds=0.1,
        ),
        SimpleNamespace(user_id="user_1", update_output=fake_update_output),
    )

    assert result.title == "Video generation status"
    assert result.metadata["status"] == "in_progress"
    assert result.metadata["still_running"] is True
    assert result.metadata["timed_out"] is True
    assert "still_running=true" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "wait", "cancel"])
async def test_generation_control_blocks_fingerprint_mismatch_without_provider_io(
    monkeypatch, action
):
    from tool.video_providers import provider_route_fingerprint

    submitted_route = VideoProviderTarget(
        provider="doubao",
        model="video-model-1",
        api_key="sk-original",
        base_url="https://api.original.test",
        submit_timeout_seconds=30,
        status_timeout_seconds=10,
    )
    current_route = VideoProviderTarget(
        provider="doubao",
        model="video-model-1",
        api_key="sk-current",
        base_url="https://api.current.test",
        submit_timeout_seconds=30,
        status_timeout_seconds=10,
    )
    job = SimpleNamespace(
        id=f"video_route_mismatch_{action}",
        kind="segment",
        status="in_progress",
        production_id="production_1",
        segment_id="segment_1",
        request_data={
            "provider_route_fingerprint": provider_route_fingerprint(submitted_route),
            "provider_wire_format": "tokenspace_contents",
        },
        provider_task_id="provider_1",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=datetime.now(timezone.utc),
    )
    settings = SimpleNamespace(poll_interval_seconds=5)
    provider_calls: list[str] = []

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def forbidden_provider_call(*_args, **_kwargs):
        provider_calls.append("called")
        raise AssertionError("mismatched route must perform zero provider I/O")

    async def forbidden_update(*_args, **_kwargs):
        raise AssertionError("mismatched route must not mutate the durable job")

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (current_route, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", forbidden_provider_call)
    monkeypatch.setattr(video_mod, "_provider_cancel", forbidden_provider_call)
    monkeypatch.setattr(video_mod, "_update_job", forbidden_update)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(action=action, job_id=job.id, wait_seconds=0),
        SimpleNamespace(user_id="user_1"),
    )

    assert provider_calls == []
    assert result.title == "Video generation recovery blocked"
    assert result.metadata["recovery_blocked"] is True
    assert result.metadata["provider_state_unknown"] is True
    assert result.metadata["do_not_resubmit"] is True
    assert result.metadata["still_running"] is False
    assert result.metadata["timed_out"] is False
    assert "recovery_blocked=true" in result.output
    assert "provider_state_unknown=true" in result.output
    assert "still_running=false" in result.output
    assert "instruction=do_not_resubmit" in result.output
    assert "retry_after_seconds" not in result.output
    assert "retry_after_seconds" not in result.metadata


@pytest.mark.asyncio
async def test_generation_control_blocks_pre_relay_legacy_job_on_relay(monkeypatch):
    current_route = VideoProviderTarget(
        provider="doubao",
        model="video-model-1",
        api_key="sk-relay",
        base_url="https://openapi.bossipai.com.cn",
        submit_timeout_seconds=30,
        status_timeout_seconds=10,
        wire_format="bossip_videos",
    )
    job = SimpleNamespace(
        id="video_legacy_direct",
        kind="segment",
        status="in_progress",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="cgt-old-direct-task",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=datetime.now(timezone.utc),
    )
    settings = SimpleNamespace(poll_interval_seconds=5)

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def must_not_poll(*_args):
        raise AssertionError("pre-relay task id must not be sent to the relay")

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (current_route, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", must_not_poll)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(action="status", job_id=job.id),
        SimpleNamespace(user_id="user_1"),
    )

    assert result.metadata["recovery_blocked"] is True
    assert result.metadata["provider_state_unknown"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "wait", "cancel"])
async def test_generation_control_blocks_legacy_matching_wire_without_fingerprint(
    monkeypatch, action
):
    """A wire match cannot prove that endpoint and provider account still match."""
    current_route = VideoProviderTarget(
        provider="doubao",
        model="video-model-1",
        api_key="sk-current-account",
        base_url="https://api.current-account.test",
        submit_timeout_seconds=30,
        status_timeout_seconds=10,
        wire_format="tokenspace_contents",
    )
    job = SimpleNamespace(
        id=f"video_legacy_same_wire_{action}",
        kind="segment",
        status="in_progress",
        production_id="production_1",
        segment_id="segment_1",
        request_data={"provider_wire_format": "tokenspace_contents"},
        provider_task_id="legacy-provider-task",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=datetime.now(timezone.utc),
    )
    settings = SimpleNamespace(poll_interval_seconds=5)

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def must_not_poll(*_args):
        raise AssertionError("legacy task without a full fingerprint must not be polled")

    async def must_not_mutate(*_args, **_kwargs):
        raise AssertionError("legacy route quarantine must not mutate the durable job")

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (current_route, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", must_not_poll)
    monkeypatch.setattr(video_mod, "_provider_cancel", must_not_poll)
    monkeypatch.setattr(video_mod, "_update_job", must_not_mutate)
    monkeypatch.setattr(video_mod, "_mark_asset", must_not_mutate)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(action=action, job_id=job.id, wait_seconds=0),
        SimpleNamespace(user_id="user_1"),
    )

    assert result.metadata["recovery_blocked"] is True
    assert result.metadata["provider_state_unknown"] is True
    assert result.metadata["do_not_resubmit"] is True
    assert result.metadata["recovery_reason"] == "legacy_provider_route_unverifiable"
    assert "retry_after_seconds" not in result.metadata
    assert "retry_after_seconds" not in result.output


@pytest.mark.asyncio
async def test_generation_control_blocks_when_persisted_route_is_unavailable(monkeypatch):
    job = SimpleNamespace(
        id="video_route_unavailable",
        kind="segment",
        status="in_progress",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="provider_1",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="removed-model",
        updated_at=datetime.now(timezone.utc),
    )

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def must_not_poll(*_args):
        raise AssertionError("an unavailable route must perform zero provider I/O")

    def unavailable(_model):
        raise RuntimeError("provider was removed")

    monkeypatch.setattr(video_mod, "_configured_target", unavailable)
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", must_not_poll)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(action="status", job_id=job.id),
        SimpleNamespace(user_id="user_1"),
    )

    assert result.metadata["recovery_blocked"] is True
    assert result.metadata["provider_state_unknown"] is True
    assert result.metadata["still_running"] is False
    assert result.metadata["do_not_resubmit"] is True
    assert result.metadata["recovery_reason"] == "provider_route_unavailable"


@pytest.mark.asyncio
async def test_generation_terminal_status_does_not_require_provider_config(monkeypatch):
    job = SimpleNamespace(
        id="video_terminal_without_route",
        kind="segment",
        status="failed",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="provider_1",
        sandbox_job_id=None,
        output_asset_id=None,
        error="provider failed before its route was removed",
        model="removed-model",
        updated_at=datetime.now(timezone.utc),
    )

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    def unavailable(_model):
        raise RuntimeError("provider was removed")

    monkeypatch.setattr(video_mod, "_configured_target", unavailable)
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(action="status", job_id=job.id),
        SimpleNamespace(user_id="user_1"),
    )

    assert result.title == "Video generation status"
    assert result.metadata["status"] == "failed"
    assert result.metadata["still_running"] is False
    assert result.metadata.get("recovery_blocked") is not True


@pytest.mark.asyncio
async def test_generation_status_guards_stale_untracked_finalizing_before_mutation(
    monkeypatch,
):
    current_route = VideoProviderTarget(
        provider="doubao",
        model="video-model-1",
        api_key="sk-relay",
        base_url="https://openapi.bossipai.com.cn",
        submit_timeout_seconds=30,
        status_timeout_seconds=10,
        wire_format="bossip_videos",
    )
    job = SimpleNamespace(
        id="video_stale_finalizing_mismatch",
        kind="segment",
        status="finalizing",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="cgt-old-direct-task",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=301),
    )
    settings = SimpleNamespace(poll_interval_seconds=5)

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("stale route mismatch must do zero provider I/O and DB mutation")

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (current_route, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", forbidden)
    monkeypatch.setattr(video_mod, "_update_job", forbidden)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(action="status", job_id=job.id),
        SimpleNamespace(user_id="user_1"),
    )

    assert job.status == "finalizing"
    assert result.metadata["recovery_blocked"] is True
    assert result.metadata["provider_state_unknown"] is True


@pytest.mark.asyncio
async def test_generation_wait_keeps_tracked_stale_finalization_local(monkeypatch):
    current_route = VideoProviderTarget(
        provider="doubao",
        model="video-model-1",
        api_key="sk-relay",
        base_url="https://openapi.bossipai.com.cn",
        submit_timeout_seconds=30,
        status_timeout_seconds=10,
        wire_format="bossip_videos",
    )
    job = SimpleNamespace(
        id="video_tracked_stale_finalizing",
        kind="segment",
        status="finalizing",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="cgt-old-direct-task",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=301),
    )
    settings = SimpleNamespace(poll_interval_seconds=0.01)
    provider_calls: list[str] = []

    async def finish_locally():
        await asyncio.sleep(0)
        job.status = "completed"
        job.updated_at = datetime.now(timezone.utc)
        return job

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def must_not_poll(*_args):
        provider_calls.append("called")
        raise AssertionError("a tracked finalization must finish locally")

    async def fake_update_output(_message):
        return None

    async def fake_attach(_job, _ctx):
        return False

    task = asyncio.create_task(finish_locally())
    video_mod._SEGMENT_FINALIZATION_TASKS[job.id] = task
    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (current_route, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", must_not_poll)
    monkeypatch.setattr(video_mod, "_attach_completed", fake_attach)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    try:
        result = await video_mod.execute_generate(
            VideoGenerateArgs(action="wait", job_id=job.id, wait_seconds=1),
            SimpleNamespace(user_id="user_1", update_output=fake_update_output),
        )
    finally:
        video_mod._SEGMENT_FINALIZATION_TASKS.pop(job.id, None)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert provider_calls == []
    assert result.metadata["status"] == "completed"
    assert result.metadata.get("recovery_blocked") is not True


@pytest.mark.asyncio
async def test_tracked_finalization_failure_cannot_bypass_route_guard(monkeypatch):
    current_route = VideoProviderTarget(
        provider="doubao",
        model="video-model-1",
        api_key="sk-relay",
        base_url="https://openapi.bossipai.com.cn",
        submit_timeout_seconds=30,
        status_timeout_seconds=10,
        wire_format="bossip_videos",
    )
    job = SimpleNamespace(
        id="video_tracked_transfer_failed",
        kind="segment",
        status="finalizing",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="cgt-old-direct-task",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=301),
    )
    settings = SimpleNamespace(poll_interval_seconds=0.01)
    provider_calls: list[str] = []

    async def fail_local_transfer():
        await asyncio.sleep(0)
        job.status = "transfer_failed"
        job.error = "copy failed"
        job.updated_at = datetime.now(timezone.utc)
        return job

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def must_not_poll(*_args):
        provider_calls.append("called")
        raise AssertionError("transfer_failed must pass the saved route guard")

    async def fake_update_output(_message):
        return None

    task = asyncio.create_task(fail_local_transfer())
    video_mod._SEGMENT_FINALIZATION_TASKS[job.id] = task
    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (current_route, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", must_not_poll)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    try:
        result = await video_mod.execute_generate(
            VideoGenerateArgs(action="wait", job_id=job.id, wait_seconds=1),
            SimpleNamespace(user_id="user_1", update_output=fake_update_output),
        )
    finally:
        video_mod._SEGMENT_FINALIZATION_TASKS.pop(job.id, None)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert provider_calls == []
    assert job.status == "transfer_failed"
    assert result.metadata["recovery_blocked"] is True
    assert result.metadata["provider_state_unknown"] is True
    assert result.metadata["still_running"] is False


@pytest.mark.asyncio
async def test_generation_status_freezes_non_provider_finalization_decision(monkeypatch):
    current_route = VideoProviderTarget(
        provider="doubao",
        model="video-model-1",
        api_key="sk-relay",
        base_url="https://openapi.bossipai.com.cn",
        submit_timeout_seconds=30,
        status_timeout_seconds=10,
        wire_format="bossip_videos",
    )
    job = SimpleNamespace(
        id="video_finalizing_guard_race",
        kind="segment",
        status="finalizing",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="cgt-old-direct-task",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        # Deliberately stale by loop time. The patched helper models an initial
        # young/tracked snapshot that became stale/untracked after the guard.
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=301),
    )
    settings = SimpleNamespace(poll_interval_seconds=0.01)

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("an unguarded call must not upgrade to provider recovery")

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (current_route, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_stale_finalization_needs_provider", lambda _job: False)
    monkeypatch.setattr(video_mod, "_provider_status", forbidden)
    monkeypatch.setattr(video_mod, "_update_job", forbidden)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(action="status", job_id=job.id),
        SimpleNamespace(user_id="user_1"),
    )

    assert job.status == "finalizing"
    assert result.metadata["status"] == "finalizing"
    assert result.metadata.get("recovery_blocked") is not True


@pytest.mark.asyncio
async def test_generation_wait_finalizing_reloads_with_backoff_until_timeout(monkeypatch):
    updated_at = datetime.now(timezone.utc)
    job = SimpleNamespace(
        id="video_finalizing",
        kind="segment",
        status="finalizing",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="provider_1",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=updated_at,
    )
    target = SimpleNamespace(model="video-model-1", channel="ark")
    job.request_data = {
        "provider_route_fingerprint": provider_route_fingerprint(target),
        "provider_wire_format": "tokenspace_contents",
    }
    settings = SimpleNamespace(poll_interval_seconds=0.01)
    reloads = 0

    async def fake_owned(_job_id, _ctx, _kind):
        nonlocal reloads
        reloads += 1
        return job

    async def fake_update_output(_message):
        return None

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (target, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    started = asyncio.get_running_loop().time()
    result = await video_mod.execute_generate(
        VideoGenerateArgs(
            action="wait",
            job_id=job.id,
            after_version=int(updated_at.timestamp() * 1_000_000),
            wait_iteration=1,
            wait_seconds=0.03,
        ),
        SimpleNamespace(user_id="user_1", update_output=fake_update_output),
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed >= 0.02
    assert reloads >= 2
    assert result.metadata["status"] == "finalizing"
    assert result.metadata["still_running"] is True
    assert result.metadata["timed_out"] is True


@pytest.mark.asyncio
async def test_generation_wait_timeout_does_not_cancel_oss_finalization(monkeypatch):
    updated_at = datetime.now(timezone.utc)
    job = SimpleNamespace(
        id="video_shielded_finalization",
        kind="segment",
        status="in_progress",
        production_id="production_1",
        segment_id="segment_1",
        request_data={},
        provider_task_id="provider_1",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=updated_at,
    )
    target = SimpleNamespace(model="video-model-1", channel="ark")
    job.request_data = {
        "provider_route_fingerprint": provider_route_fingerprint(target),
        "provider_wire_format": "tokenspace_contents",
    }
    settings = SimpleNamespace(poll_interval_seconds=0.01)
    release = asyncio.Event()

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def fake_status(_target, _provider_task_id):
        return {"status": "succeeded", "video_url": "https://cdn.example/video.mp4"}

    async def fake_finalize(_job, _data, _ctx, _settings, _target):
        job.status = "finalizing"
        job.updated_at = datetime.now(timezone.utc)
        await release.wait()
        job.status = "completed"
        job.updated_at = datetime.now(timezone.utc)
        return job

    async def fake_update_output(_message):
        return None

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (target, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", fake_status)
    monkeypatch.setattr(video_mod, "_finalize_segment", fake_finalize)
    monkeypatch.setattr(video_mod, "_job_asset", lambda _job: asyncio.sleep(0, result=None))

    result = await video_mod.execute_generate(
        VideoGenerateArgs(
            action="wait",
            job_id=job.id,
            after_version=0,
            wait_iteration=0,
            wait_seconds=0.01,
        ),
        SimpleNamespace(user_id="user_1", update_output=fake_update_output),
    )

    task = video_mod._SEGMENT_FINALIZATION_TASKS[job.id]
    assert task.cancelled() is False
    assert task.done() is False
    assert result.metadata["status"] == "finalizing"
    assert result.metadata["timed_out"] is True

    release.set()
    await task
    await asyncio.sleep(0)
    assert job.status == "completed"
    assert job.id not in video_mod._SEGMENT_FINALIZATION_TASKS


@pytest.mark.asyncio
async def test_generation_wait_returns_when_snapshot_version_advances(monkeypatch):
    updated_at = datetime.now(timezone.utc)
    old_version = int(updated_at.timestamp() * 1_000_000)
    job = SimpleNamespace(
        id="video_2",
        kind="segment",
        status="queued",
        production_id="production_1",
        segment_id="segment_2",
        request_data={},
        provider_task_id="provider_2",
        sandbox_job_id=None,
        output_asset_id=None,
        error=None,
        model="video-model-1",
        updated_at=updated_at,
    )
    target = SimpleNamespace(model="video-model-1", channel="ark")
    job.request_data = {
        "provider_route_fingerprint": provider_route_fingerprint(target),
        "provider_wire_format": "tokenspace_contents",
    }
    settings = SimpleNamespace(poll_interval_seconds=5)

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def fake_status(_target, _provider_task_id):
        return {"status": "processing"}

    async def fake_update(_job_id, **values):
        for key, value in values.items():
            setattr(job, key, value)
        job.updated_at = updated_at + timedelta(microseconds=1)

    async def fake_asset(_job):
        return None

    async def fake_update_output(_message):
        return None

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (target, settings))
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    monkeypatch.setattr(video_mod, "_provider_status", fake_status)
    monkeypatch.setattr(video_mod, "_update_job", fake_update)
    monkeypatch.setattr(video_mod, "_job_asset", fake_asset)

    result = await video_mod.execute_generate(
        VideoGenerateArgs(
            action="wait",
            job_id=job.id,
            after_version=old_version,
            wait_iteration=0,
        ),
        SimpleNamespace(user_id="user_1", update_output=fake_update_output),
    )

    assert result.metadata["status"] == "in_progress"
    assert result.metadata["version"] > old_version
    assert result.metadata["still_running"] is True
    assert result.metadata["timed_out"] is False
    assert "next_wait_iteration=1" in result.output


def test_completed_segment_explicitly_hands_off_to_transcription():
    job = SimpleNamespace(
        id="video_123",
        kind="segment",
        status="completed",
        production_id="production_123",
        segment_id="segment_123",
        request_data={},
        provider_task_id="provider_123",
        sandbox_job_id=None,
        error=None,
    )

    output = "\n".join(video_mod._job_lines(job))

    assert "next_action=video_transcribe.submit" in output
    assert "transcription_idempotency_key=production_123:segment_123:stt" in output
    assert "do not call video_generate again" in output


def test_render_completion_exposes_stable_download_handoff():
    job = SimpleNamespace(
        id="video_render_123",
        kind="render",
        status="completed",
        production_id="production_123",
        segment_id=None,
        request_data={"subtitles": True},
        provider_task_id=None,
        sandbox_job_id="sandbox_render_123",
        error=None,
    )
    asset = SimpleNamespace(
        id="asset_final_123",
        user_id="user_123",
        name="final.mp4",
        size=1234,
        status="ready",
    )

    with patch.object(video_mod, "create_asset_download_token", return_value="download-token"):
        output = "\n".join(video_mod._job_lines(job, asset))

    assert (
        "download_url=/api/assets/asset_final_123/download?token=download-token"
        in output
    )
    assert "never construct a markdown URL" in output


def test_render_captions_must_match_segments():
    with pytest.raises(ValidationError, match="captions"):
        VideoRenderArgs(
            action="submit",
            production_id="production_1",
            idempotency_key="travel-final-v1",
            segment_assets=["a", "b"],
            captions=["only one"],
        )


def test_render_defaults_to_fast_auto_path_and_source_resolution():
    args = VideoRenderArgs(
        action="submit",
        production_id="production_1",
        idempotency_key="travel-final-v1",
        segment_assets=["asset-1"],
    )
    assert args.render_engine == "auto"
    assert (args.width, args.height) == (720, 1280)
    assert args.wait_iteration == 0


@pytest.mark.asyncio
async def test_character_reference_is_an_image_first_and_is_deduplicated(monkeypatch):
    portrait = SimpleNamespace(id="asset_portrait", mime="image/png")
    scene = SimpleNamespace(id="asset_scene", mime="video/mp4")

    async def fake_find(ref, _ctx):
        assert ref == "asset_portrait"
        return portrait

    async def fake_inputs(refs, _ctx):
        assert refs == ["asset_portrait", "asset_scene"]
        return [portrait, scene]

    monkeypatch.setattr(video_mod, "_find_owned_asset", fake_find)
    monkeypatch.setattr(video_mod, "_resolve_inputs", fake_inputs)

    rows, character = await _resolve_generation_inputs(
        "asset_portrait",
        ["asset_portrait", "asset_scene"],
        SimpleNamespace(),
    )

    assert character is portrait
    assert [row.id for row in rows] == ["asset_portrait", "asset_scene"]


@pytest.mark.asyncio
async def test_character_reference_rejects_video_assets(monkeypatch):
    async def fake_find(_ref, _ctx):
        return SimpleNamespace(id="asset_clip", mime="video/mp4")

    monkeypatch.setattr(video_mod, "_find_owned_asset", fake_find)

    with pytest.raises(RuntimeError, match="must be an image"):
        await _resolve_generation_inputs("asset_clip", [], SimpleNamespace())


@pytest.mark.asyncio
async def test_submit_records_and_sends_character_reference_first(monkeypatch):
    target = SimpleNamespace(
        model="doubao-seedance-2-0-260128",
        provider="doubao",
        api_key="sk-submit-secret",
        base_url="https://api.tokenspace.test",
        channel="ark",
        auth_scheme="bearer",
        wire_format="tokenspace_contents",
    )
    settings = SimpleNamespace(
        default_resolution="720p",
        default_ratio="9:16",
        default_duration=-1,
        default_generate_audio=True,
        default_watermark=False,
        provider_input_url_ttl_seconds=600,
    )
    portrait = SimpleNamespace(
        id="asset_portrait",
        mime="image/png",
        oss_key="assets/user/portrait.png",
    )
    scene = SimpleNamespace(
        id="asset_scene",
        mime="video/mp4",
        oss_key="assets/user/scene.mp4",
    )
    output_asset = SimpleNamespace(id="asset_output", status="pending")
    job = SimpleNamespace(
        id="video_job",
        status="submitting",
        request_data={},
        provider_task_id=None,
        sandbox_job_id=None,
        output_asset_id=output_asset.id,
        production_id="production_1",
        segment_id="segment_1",
        error=None,
    )
    observed = {}

    async def fake_resolve(character, refs, _ctx):
        assert character == "asset_portrait"
        assert refs == ["asset_scene"]
        return [portrait, scene], portrait

    def fake_refs(rows, **kwargs):
        assert rows == [portrait, scene]
        assert kwargs["input_url_ttl_seconds"] == 600
        return [
            {
                "kind": "image",
                "url": "https://oss.test/asset-provider-portrait",
                "role": "reference_image",
            },
            {
                "kind": "video",
                "url": "https://oss.test/asset-provider-scene",
                "role": "reference_video",
            },
        ]

    async def fake_create(**kwargs):
        observed["request_data"] = kwargs["request_data"]
        observed["request_hash"] = kwargs["request_hash"]
        job.request_data = kwargs["request_data"]
        return job, output_asset, True

    async def fake_submit(_target, payload):
        observed["payload"] = payload
        return {"id": "provider_task", "status": "running"}

    async def fake_update(_job_id, **values):
        for key, value in values.items():
            setattr(job, key, value)

    async def fake_owned(_job_id, _ctx, _kind):
        return job

    async def fake_progress(_message):
        raise asyncio.CancelledError

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (target, settings))
    monkeypatch.setattr(video_mod, "_resolve_generation_inputs", fake_resolve)
    monkeypatch.setattr(video_mod, "_presigned_provider_refs", fake_refs)
    monkeypatch.setattr(video_mod, "_create_pending_job", fake_create)
    monkeypatch.setattr(video_mod, "_provider_submit", fake_submit)
    monkeypatch.setattr(video_mod, "_update_job", fake_update)
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)
    import tool.video_workflow as workflow_mod

    async def fake_prepare(_ctx, production_id, segment_id):
        assert production_id == "production_1"
        assert segment_id == "segment_1"
        return {
            "production_id": production_id,
            "segment_id": segment_id,
            "prompt": "主持人说一句话",
            "character_reference_asset": "asset_portrait",
            "input_assets": ["asset_scene"],
            "resolution": "720p",
            "ratio": "9:16",
            "duration": -1,
            "generate_audio": True,
            "watermark": False,
            "content_hash": "content-hash",
            "plan_hash": "plan-hash",
        }

    async def fake_mark(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow_mod, "prepare_segment_submission", fake_prepare)
    monkeypatch.setattr(workflow_mod, "mark_segment_job", fake_mark)

    result = await video_mod.execute_generate(
        VideoGenerateArgs(
            action="submit",
            production_id="production_1",
            segment_id="segment_1",
            idempotency_key="production_1:segment_1:generate",
        ),
        SimpleNamespace(update_output=fake_progress, user_id="user_1"),
    )

    assert observed["request_data"]["character_reference_asset_id"] == "asset_portrait"
    assert observed["request_data"]["input_asset_ids"] == ["asset_portrait", "asset_scene"]
    from tool.video_providers import provider_route_fingerprint

    assert observed["request_data"]["provider_route_fingerprint"] == provider_route_fingerprint(
        target
    )
    assert "sk-submit-secret" not in json.dumps(observed["request_data"], sort_keys=True)
    from tool.video_workflow import content_hash

    legacy_request_data = dict(observed["request_data"])
    legacy_request_data.pop("provider_route_fingerprint")
    assert observed["request_hash"] == content_hash(
        {
            "kind": "segment",
            "model": target.model,
            "prompt": "主持人说一句话",
            "request_data": legacy_request_data,
        }
    )
    content = observed["payload"]["content"]
    assert content[1]["role"] == "reference_image"
    assert content[1]["image_url"]["url"] == "https://oss.test/asset-provider-portrait"
    assert content[2]["role"] == "reference_video"
    assert "character_reference_asset_id=asset_portrait" in result.output


def test_seedance_spoken_video_constraints():
    _validate_generation("doubao-seedance-2-0-260128", "1080p", -1, True)
    with pytest.raises(RuntimeError, match="standard model"):
        _validate_generation("doubao-seedance-2-0-fast-260128", "720p", 5, True)
    with pytest.raises(RuntimeError, match="only"):
        _validate_generation("doubao-seedance-2-5-260628", "1080p", 5, True)
    with pytest.raises(RuntimeError, match="4-30"):
        _validate_generation("doubao-seedance-2-5-260628", "720p", -1, True)


def test_provider_auth_is_normalized_to_bearer():
    assert _auth_header("sk-secret") == "Bearer sk-secret"
    assert _auth_header("Bearer sk-secret") == "Bearer sk-secret"


def _bossip_target() -> VideoProviderTarget:
    return VideoProviderTarget(
        provider="doubao",
        model="doubao-seedance-2-0-260128",
        api_key="sk-gateway",
        base_url="https://openapi.bossipai.com.cn",
        wire_format="bossip_videos",
        submit_timeout_seconds=180,
        status_timeout_seconds=60,
    )


def test_bossip_relay_payload_uses_public_video_contract():
    body = _bossip_video_payload(
        _bossip_target(),
        {
            "model": "doubao-seedance-2-0-260128",
            "content": [
                {"type": "text", "text": "主持人自然介绍产品"},
                {"type": "image_url", "image_url": {"url": "https://oss.test/host.png"}},
                {"type": "image_url", "image_url": {"url": "https://oss.test/room.png"}},
                {"type": "video_url", "video_url": {"url": "https://oss.test/motion.mp4"}},
            ],
            "resolution": "720p",
            "ratio": "9:16",
            "duration": -1,
            "generate_audio": True,
            "watermark": False,
        },
    )

    assert body == {
        "model": "video-sd-720p-proⅠ",
        "prompt": "主持人自然介绍产品",
        "resolution": "720p",
        "ratio": "9:16",
        "generate_audio": True,
        "watermark": False,
        "image_url": "https://oss.test/host.png",
        "extra_images": ["https://oss.test/room.png"],
        "extra_videos": ["https://oss.test/motion.mp4"],
    }


def test_bossip_relay_rejects_480p_spoken_video_instead_of_silently_dropping_audio():
    with pytest.raises(RuntimeError, match="does not support generated audio"):
        _bossip_video_payload(
            _bossip_target(),
            {
                "content": [{"type": "text", "text": "测试视频"}],
                "resolution": "480p",
                "generate_audio": True,
            },
        )


def test_provider_inputs_are_scoped_urls_for_normal_image_to_video(monkeypatch):
    class FakeOss:
        def presign_get(self, key, expires_sec):
            return f"https://oss.test/{key}?ttl={expires_sec}"

    monkeypatch.setattr("core.oss.get_oss", lambda: FakeOss())
    portrait = SimpleNamespace(id="portrait", mime="image/png", oss_key="assets/portrait.png")
    motion = SimpleNamespace(id="motion", mime="video/mp4", oss_key="assets/motion.mp4")

    refs = _presigned_provider_refs(
        [portrait, motion],
        input_url_ttl_seconds=3600,
    )

    assert refs == [
        {
            "kind": "image",
            "url": "https://oss.test/assets/portrait.png?ttl=3600",
            "role": "reference_image",
        },
        {
            "kind": "video",
            "url": "https://oss.test/assets/motion.mp4?ttl=3600",
            "role": "reference_video",
        },
    ]


@pytest.mark.asyncio
async def test_bossip_relay_submit_and_status_use_v1_videos(monkeypatch):
    import httpx

    calls = []

    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            calls.append(("POST", url, headers, json))
            return FakeResponse({"id": "task_public", "status": "processing"})

        async def get(self, url, *, headers):
            calls.append(("GET", url, headers, None))
            return FakeResponse(
                {"id": "task_public", "status": "completed", "video_url": "https://result.test/out.mp4"}
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    payload = {
        "content": [{"type": "text", "text": "测试视频"}],
        "resolution": "720p",
        "ratio": "9:16",
        "duration": 5,
    }

    submitted = await _provider_submit(_bossip_target(), payload)
    status = await _provider_status(_bossip_target(), submitted["id"])

    assert calls[0][0:2] == ("POST", "https://openapi.bossipai.com.cn/v1/videos")
    assert calls[0][2]["Authorization"] == "Bearer sk-gateway"
    assert calls[0][3]["model"] == "video-sd-720p-proⅠ"
    assert calls[1][0:2] == ("GET", "https://openapi.bossipai.com.cn/v1/videos/task_public")
    assert _provider_video_url(status) == "https://result.test/out.mp4"


def test_video_skill_preserves_reference_recovery_and_teaching_contract():
    skill_path = (
        Path(__file__).resolve().parents[2]
        / ".openbox"
        / "skills"
        / "video-production"
        / "SKILL.md"
    )
    metadata, skill = parse_frontmatter(skill_path.read_text(encoding="utf-8"))

    assert metadata["allowed-tools"] == [
        "image_gen",
        "video_project",
        "video_generate",
        "video_transcribe",
        "video_render",
        "creator_context",
    ]
    assert len(skill_path.read_text(encoding="utf-8").splitlines()) <= 90
    assert "generate it once with `image_gen`" in skill
    assert "read its ready `asset_id`" in skill
    assert "`/workspace/...` is inspection-only" in skill
    assert "never repeat the host" in skill
    assert "do not stop the turn after this read" in skill
    assert "A prose-only “if this is okay” question is" in skill
    assert "native approval card" in skill
    assert "`spend` approval" in skill
    assert "accepted actual STT" in skill
    assert "wait_iteration" in skill
    assert "returned `version`" in skill
    assert "A timeout is normal" in skill
    assert "`recovery_blocked=true`" in skill
    assert 'role="broll"' in skill
    assert "`全片一致的画面基底：<anchor>`" in skill
    assert "`自然肢体动作：...`" in skill
    assert "recount" in skill
    assert "Leave `model` unset" in skill
    assert "`@<exact dialogue>`" in skill
    assert "`无字幕`" in skill
    assert "Submit independent planned segments together" in skill
    assert "Keep dependent actions for one job ordered" in skill
    assert "`polling_paused=true`" in skill
    assert "resume that exact job only in a later turn" in skill
