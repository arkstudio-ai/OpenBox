"""Skill-only video tools validate billable and render calls conservatively."""
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
    VideoGenerateArgs,
    VideoRenderArgs,
    _auth_header,
    _resolve_generation_inputs,
    _validate_generation,
    video_generate_tool,
    video_render_tool,
)


def test_video_tools_are_skill_only_and_not_parallel_safe():
    assert video_generate_tool.skill_only is True
    assert video_render_tool.skill_only is True
    assert video_generate_tool.parallel_safe is False
    assert video_render_tool.parallel_safe is False


@pytest.mark.asyncio
async def test_video_schemas_are_absent_until_the_skill_activates_them():
    register_builtin_tools()

    ordinary = await resolve_step_tools(AGENTS["build"], None, [])
    assert "image_gen" not in ordinary
    assert "video_generate" not in ordinary
    assert "video_render" not in ordinary

    loaded = await resolve_step_tools(
        AGENTS["build"],
        None,
        [],
        activated_tools={"image_gen", "video_generate", "video_render"},
    )
    assert loaded["image_gen"].skill_only is True
    assert loaded["video_generate"].skill_only is True
    assert loaded["video_render"].skill_only is True


def test_billable_submit_requires_idempotency_key():
    with pytest.raises(ValidationError, match="idempotency_key"):
        VideoGenerateArgs(action="submit", prompt="人物说一句话")
    with pytest.raises(ValidationError, match="idempotency_key"):
        VideoRenderArgs(action="submit", segment_assets=["asset-1"])


def test_render_captions_must_match_segments():
    with pytest.raises(ValidationError, match="captions"):
        VideoRenderArgs(
            action="submit",
            idempotency_key="travel-final-v1",
            segment_assets=["a", "b"],
            captions=["only one"],
        )


def test_render_defaults_to_fast_auto_path_and_source_resolution():
    args = VideoRenderArgs(
        action="submit",
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
        return None

    monkeypatch.setattr(video_mod, "_configured_target", lambda _model: (target, settings))
    monkeypatch.setattr(core.oss, "get_oss", lambda: FakeOss())
    monkeypatch.setattr(video_mod, "_resolve_generation_inputs", fake_resolve)
    monkeypatch.setattr(video_mod, "_create_pending_job", fake_create)
    monkeypatch.setattr(video_mod, "_provider_submit", fake_submit)
    monkeypatch.setattr(video_mod, "_update_job", fake_update)
    monkeypatch.setattr(video_mod, "_owned_job", fake_owned)

    result = await video_mod.execute_generate(
        VideoGenerateArgs(
            action="submit",
            idempotency_key="trip-segment-01-v1",
            prompt="主持人说一句话",
            character_reference_asset="asset_portrait",
            input_assets=["asset_scene"],
        ),
        SimpleNamespace(update_output=fake_progress),
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

    assert metadata["allowed-tools"] == ["image_gen", "video_generate", "video_render"]
    assert "call `image_gen` once" in skill
    assert "character_reference_asset=<portrait asset_id>" in skill
    assert "reuse that exact asset for every" in skill
    assert "Never vary seeds while claiming" in skill
    assert "width=720" in skill
    assert "height=1280" in skill
    assert 'render_engine="auto"' in skill
    assert "wait_iteration=<counter>" in skill
    assert "never invent or increment a version" in skill
