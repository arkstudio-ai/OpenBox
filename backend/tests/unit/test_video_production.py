"""Skill-only video tools validate billable and render calls conservatively."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.agent import AGENTS
from agent.tool_resolution import resolve_step_tools
from core.markdown import parse_frontmatter
import tool.video_production as video_mod
from tool.registry import register_builtin_tools
from tool.video_production import (
    VideoTranscriptionTarget,
    VideoGenerateArgs,
    VideoRenderArgs,
    _auth_header,
    _provider_transcribe,
    _resolve_generation_inputs,
    _validate_generation,
    video_generate_tool,
    video_transcribe_tool,
    video_render_tool,
)


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


def test_video_tools_are_skill_only_and_not_parallel_safe():
    assert video_generate_tool.skill_only is True
    assert video_transcribe_tool.skill_only is True
    assert video_render_tool.skill_only is True
    assert video_generate_tool.parallel_safe is False
    assert video_transcribe_tool.parallel_safe is False
    assert video_render_tool.parallel_safe is False


@pytest.mark.asyncio
async def test_video_schemas_are_absent_until_the_skill_activates_them():
    register_builtin_tools()

    ordinary = await resolve_step_tools(AGENTS["build"], None, [])
    assert "image_gen" not in ordinary
    assert "video_project" not in ordinary
    assert "video_generate" not in ordinary
    assert "video_transcribe" not in ordinary
    assert "video_render" not in ordinary

    loaded = await resolve_step_tools(
        AGENTS["build"],
        None,
        [],
        activated_tools={"image_gen", "video_project", "video_generate", "video_transcribe", "video_render"},
    )
    assert loaded["image_gen"].skill_only is True
    assert loaded["video_generate"].skill_only is True
    assert loaded["video_transcribe"].skill_only is True
    assert loaded["video_render"].skill_only is True


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
    }


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
    import core.oss

    target = SimpleNamespace(
        model="doubao-seedance-2-0-260128",
        provider="doubao",
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

    class FakeOss:
        def presign_get(self, key, expires_sec):
            return f"https://oss.test/{key}?ttl={expires_sec}"

    async def fake_resolve(character, refs, _ctx):
        assert character == "asset_portrait"
        assert refs == ["asset_scene"]
        return [portrait, scene], portrait

    async def fake_create(**kwargs):
        observed["request_data"] = kwargs["request_data"]
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
    monkeypatch.setattr(core.oss, "get_oss", lambda: FakeOss())
    monkeypatch.setattr(video_mod, "_resolve_generation_inputs", fake_resolve)
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
            "spend_approval_id": "approval_1",
        }

    async def fake_consume(approval_id):
        assert approval_id == "approval_1"

    async def fake_mark(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow_mod, "prepare_segment_submission", fake_prepare)
    monkeypatch.setattr(workflow_mod, "consume_spend_approval", fake_consume)
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
    content = observed["payload"]["content"]
    assert content[1]["role"] == "reference_image"
    assert "portrait.png" in content[1]["image_url"]["url"]
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


def test_video_skill_preserves_host_identity_and_source_resolution():
    skill_path = (
        Path(__file__).resolve().parents[2]
        / ".openbox"
        / "skills"
        / "video-production"
        / "SKILL.md"
    )
    metadata, skill = parse_frontmatter(skill_path.read_text(encoding="utf-8"))

    assert metadata["allowed-tools"] == [
        "image_gen", "video_project", "video_generate", "video_transcribe", "video_render",
        "creator_context",
    ]
    assert "`image_gen` once" in skill
    assert "Reuse that exact `asset_id` across every segment" in skill
    assert "request `spend` approval" in skill
    assert "accepted STT text" in skill
    assert 'render_engine="auto"' in skill
    assert "wait_iteration" in skill
    assert "exact returned `version`" in skill
    assert "Do not wrap" in skill
    assert "generic Batch/parallel tool" in skill
    assert "generation_job_id" in skill
