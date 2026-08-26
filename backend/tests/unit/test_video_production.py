"""Skill-only video tools validate billable and render calls conservatively."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.agent import AGENTS
from agent.tool_resolution import resolve_step_tools
from tool.registry import register_builtin_tools
from tool.video_production import (
    VideoGenerateArgs,
    VideoRenderArgs,
    _auth_header,
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
    assert "video_generate" not in ordinary
    assert "video_render" not in ordinary

    loaded = await resolve_step_tools(
        AGENTS["build"],
        None,
        [],
        activated_tools={"video_generate", "video_render"},
    )
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
    skill = (
        Path(__file__).resolve().parents[2]
        / ".openbox"
        / "skills"
        / "video-production"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "reuse that exact asset for every" in skill
    assert "Never vary seeds while claiming" in skill
    assert "width=720" in skill
    assert "height=1280" in skill
