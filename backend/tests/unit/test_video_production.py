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


def test_billable_submit_requires_idempotency_key():
    """The key is what stops a retry from paying a second time."""
    with pytest.raises(ValidationError, match="idempotency_key"):
        VideoGenerateArgs(action="submit", prompt="人物说一句话")


def test_generation_and_transcription_are_parallel_safe():
    assert video_generate_tool.parallel_safe is True
    assert video_transcribe_tool.parallel_safe is True


def test_generation_schema_exposes_content_parameters_for_open_requests():
    """The tool is the video primitive, so the shot is describable through it.

    It used to expose control-plane fields only, which meant the sole way to
    generate anything was to drive a whole approved production. Content
    parameters belong here; what stays out is anything that would let a caller
    reach past ownership (references are asset ids, never URLs).
    """
    properties = set(VideoGenerateArgs.model_json_schema()["properties"])

    assert {
        "prompt",
        "model",
        "resolution",
        "ratio",
        "duration",
        "generate_audio",
        "watermark",
        "seed",
        "input_assets",
    } <= properties
    assert {
        "action",
        "job_id",
        "idempotency_key",
        "wait_seconds",
        "after_version",
        "wait_iteration",
    } <= properties
    assert not {"url", "image_url", "video_url", "api_key", "base_url"} & properties


def test_generation_needs_only_a_prompt_and_a_key():
    """A prompt and a key is the whole contract: no project, no approvals."""
    args = VideoGenerateArgs(
        action="submit", prompt="一只猫跳上窗台", idempotency_key="open:cat:1"
    )

    assert args.prompt == "一只猫跳上窗台"
    assert not hasattr(args, "production_id")


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


def test_video_skill_teaches_craft_and_leaves_enforcement_to_the_tools():
    """The skill is knowledge now, so it must read like knowledge.

    It used to restate the server's gates step by step, which made it a manual
    for a state machine rather than advice about making a video. What it owes
    the reader is what actually goes wrong and how to avoid it; ownership,
    billing and idempotency are the tools' job and are not re-litigated here.
    """
    skill_dir = (
        Path(__file__).resolve().parents[2] / ".openbox" / "skills" / "video-production"
    )
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    metadata, skill = parse_frontmatter(text)

    # A dependency declaration, not a grant: loading a skill never widens the
    # callable tool set (see docs/SKILL_TOOL_DECOUPLING_PLAN.md).
    assert set(metadata["allowed-tools"]) == {
        "video_generate",
        "video_transcribe",
        "image_gen",
        "creator_context",
        "share_file",
        "bash",
    }
    assert "video_project" not in text and "video_render" not in text

    # Progressive disclosure: the body stays small, detail lives alongside it.
    assert len(text.splitlines()) <= 200
    for name in ("prompt-recipes.md", "model-guide.md", "quality.md"):
        assert (skill_dir / "references" / name).is_file()
        assert name in skill

    # The craft it must carry.
    assert "全片一致的画面基底" in skill or "anchor" in skill
    assert "无字幕" in skill
    assert "actual transcript" in skill
    assert "seed" in skill
    assert "A timeout is normal, and a paid task is never replaced" in skill
    assert "polling_paused=true" in skill

    # And the posture: advice that can be departed from.
    assert "not a pipeline" in skill
    assert "it advises, it never" in skill


def test_video_skill_scripts_are_bundled_for_the_agent_to_run():
    scripts = (
        Path(__file__).resolve().parents[2]
        / ".openbox" / "skills" / "video-production" / "scripts"
    )

    assert {item.name for item in scripts.iterdir()} >= {
        "lint_prompt.py",
        "compare_transcript.py",
        "build_ass.py",
        "compose.sh",
        "extract_audio.sh",
        "state.py",
    }
