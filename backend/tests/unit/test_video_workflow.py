"""Hash-bound spoken-video workflow, prompt lint, STT, and render source gates."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.user import User
from db.models.video_job import VideoJob
from tool.tool import ToolContext
from tool.video_workflow import (
    SegmentSpec,
    VideoProjectArgs,
    compare_transcript,
    consume_spend_approval,
    execute_project,
    lint_segment_prompt,
    mark_segment_job,
    prepare_render_submission,
    prepare_segment_submission,
    record_segment_transcript,
)


def test_prompt_lint_requires_recipe_and_valid_reference_numbers():
    anchor = "参考图片1的人物坐在明亮整洁的旅行分享区，人物造型和机位全程一致"
    valid = (
        f"固定镜头中景，{anchor}，面对镜头开口说出@上海的清晨从梧桐树影开始，"
        "手势随语气自然舒展，语气亲切，无字幕"
    )
    result = lint_segment_prompt(
        script_text="上海的清晨从梧桐树影开始",
        prompt=valid,
        visual_anchor=anchor,
        image_count=1,
        video_count=0,
    )
    assert result["ok"] is True

    invalid = lint_segment_prompt(
        script_text="上海的清晨从梧桐树影开始",
        prompt=valid.replace("参考图片1", "参考图片2").replace("无字幕", "显示字幕"),
        visual_anchor=anchor,
        image_count=1,
        video_count=0,
    )
    assert invalid["ok"] is False
    assert any("参考图片2" in message for message in invalid["failures"])
    assert any("无字幕" in message for message in invalid["failures"])


def test_stt_comparison_keeps_phrase_omission_as_suspect():
    result = compare_transcript(
        "上海不只有外滩，还有梧桐区最松弛的日常",
        "上海不只有外滩，还有最松弛的日常",
    )
    assert result["verdict"] == "suspect"
    assert any("梧桐区" in note for note in result["notes"])


def test_stt_comparison_flags_single_character_substitution():
    result = compare_transcript(
        "沿着滨江慢慢走，拍照特别出片。",
        "沿着滨江慢慢走，拍照特别出花。",
    )

    assert result["similarity"] > 0.90
    assert result["verdict"] == "suspect"
    assert result["notes"] == ["疑似念错「片→花」"]


@pytest.mark.asyncio
async def test_hash_bound_approvals_spend_stt_and_render_caption_source(monkeypatch):
    suffix = uuid4().hex[:10]
    user_id = f"user_{suffix}"
    portrait_id = f"asset_portrait_{suffix}"
    output_id = f"asset_video_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            User(
                id=user_id,
                username=f"video-{suffix}",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            FileAsset(
                id=portrait_id,
                user_id=user_id,
                session_id=None,
                project_id=None,
                name="host.png",
                oss_key=f"assets/{user_id}/{portrait_id}/host.png",
                mime="image/png",
                size=100,
                status="ready",
                source="agent",
                transient=False,
                created_at=now,
            )
        )
        db.add(
            FileAsset(
                id=output_id,
                user_id=user_id,
                session_id=None,
                project_id=None,
                name="segment.mp4",
                oss_key=f"assets/{user_id}/{output_id}/segment.mp4",
                mime="video/mp4",
                size=1000,
                status="ready",
                source="agent",
                transient=False,
                created_at=now,
            )
        )

    async def approve_first(*, questions, **_kwargs):
        return [[questions[0].options[0].label]]

    monkeypatch.setattr("question.question.ask", approve_first)
    ctx = ToolContext(session_id=f"session_{suffix}", user_id=user_id, message_id="message_1", part_id="part_1")
    created = await execute_project(
        VideoProjectArgs(
            action="create",
            title="上海旅行",
            brief="制作上海旅游口播短视频",
            target_duration_seconds=30,
        ),
        ctx,
    )
    production_id = created.metadata["production_id"]
    script = "上海的清晨从梧桐树影开始"
    await execute_project(
        VideoProjectArgs(action="set_script", production_id=production_id, script_text=script),
        ctx,
    )
    approved_script = await execute_project(
        VideoProjectArgs(
            action="request_approval", production_id=production_id, approval_kind="script"
        ),
        ctx,
    )
    assert approved_script.metadata["approvals"]["script"] is True

    anchor = "参考图片1的人物坐在明亮整洁的旅行分享区，人物造型和机位全程一致"
    prompt = (
        f"固定镜头中景，{anchor}，面对镜头开口说出@{script}，"
        "手势随语气自然舒展，语气亲切，无字幕"
    )
    planned = await execute_project(
        VideoProjectArgs(
            action="set_segments",
            production_id=production_id,
            visual_anchor=anchor,
            character_reference_asset=portrait_id,
            segments=[SegmentSpec(ordinal=1, role="hook", script_text=script, prompt=prompt)],
        ),
        ctx,
    )
    assert f"visual_anchor={anchor}" in planned.output
    assert f"character_asset_id={portrait_id}" in planned.output
    segment_id = planned.metadata["segments"][0]["segment_id"]
    await execute_project(
        VideoProjectArgs(
            action="request_approval", production_id=production_id, approval_kind="segments"
        ),
        ctx,
    )
    spent = await execute_project(
        VideoProjectArgs(
            action="request_approval", production_id=production_id, approval_kind="spend"
        ),
        ctx,
    )
    assert spent.metadata["status"] == "spend_ok"
    gate = await prepare_segment_submission(ctx, production_id, segment_id)
    assert gate["prompt"] == prompt
    assert gate["character_reference_asset"] == portrait_id
    await consume_spend_approval(gate["spend_approval_id"])
    with pytest.raises(RuntimeError, match="limit is exhausted"):
        await prepare_segment_submission(ctx, production_id, segment_id)

    existing_job_id = f"video_existing_{suffix}"
    async with get_db_session() as db:
        db.add(
            VideoJob(
                id=existing_job_id,
                user_id=user_id,
                session_id=ctx.session_id,
                project_id=None,
                kind="segment",
                production_id=production_id,
                segment_id=segment_id,
                idempotency_key=f"{production_id}:{segment_id}:generate",
                request_hash="existing-request",
                status="submitting",
                model="doubao-seedance-2-0-260128",
                provider_task_id=None,
                sandbox_job_id=None,
                prompt=prompt,
                request_data={},
                result_data={},
                output_asset_id=output_id,
                error=None,
                attempt=0,
                attached_message_id=None,
                created_at=now,
                updated_at=now,
                started_at=None,
                completed_at=None,
            )
        )
    await mark_segment_job(segment_id, existing_job_id, status="submitting")
    reconciliation = await prepare_segment_submission(ctx, production_id, segment_id)
    assert reconciliation["reconciling_existing"] is True

    await mark_segment_job(segment_id, "video_job_failed", status="failed")
    failed = await execute_project(
        VideoProjectArgs(action="status", production_id=production_id),
        ctx,
    )
    assert failed.metadata["status"] == "needs_segment_revision"
    assert failed.metadata["segments"][0]["generation_job_id"] == "video_job_failed"
    assert "segment_1_generation_job_id=video_job_failed" in failed.output
    blocked_spend = await execute_project(
        VideoProjectArgs(
            action="request_approval",
            production_id=production_id,
            approval_kind="spend",
        ),
        ctx,
    )
    assert blocked_spend.title == "Resolve active segment jobs first"
    assert "segment 1=failed" in blocked_spend.output
    with pytest.raises(RuntimeError, match="newly planned revision"):
        await prepare_segment_submission(ctx, production_id, segment_id)

    await mark_segment_job(segment_id, "video_job_test", status="completed", output_asset_id=output_id)
    comparison = await record_segment_transcript(
        segment_id,
        script,
        {"text": script, "model": "seed-asr"},
        threshold=0.90,
    )
    assert comparison["verdict"] == "ok"
    status_with_transcript = await execute_project(
        VideoProjectArgs(action="status", production_id=production_id),
        ctx,
    )
    assert f"segment_1_transcript={script}" in status_with_transcript.output
    assert "segment_1_stt_notes=[]" in status_with_transcript.output
    assert "segment_1_review_status=accepted" in status_with_transcript.output
    quality = await execute_project(
        VideoProjectArgs(
            action="request_approval", production_id=production_id, approval_kind="quality"
        ),
        ctx,
    )
    assert quality.metadata["approvals"]["quality"] is True
    render = await execute_project(
        VideoProjectArgs(
            action="request_approval", production_id=production_id, approval_kind="render"
        ),
        ctx,
    )
    assert render.metadata["render_idempotency_key"]
    render_gate = await prepare_render_submission(ctx, production_id)
    assert render_gate["segment_assets"] == [output_id]
    assert render_gate["captions"] == [script]
    assert render_gate["subtitles"] is True

    revised = await execute_project(
        VideoProjectArgs(
            action="revise_segment",
            production_id=production_id,
            segment_id=segment_id,
            revision_reason="测试选择性重生",
        ),
        ctx,
    )
    assert revised.metadata["segments"][0]["revision"] == 2
    assert revised.metadata["approvals"]["segments"] is False
    with pytest.raises(RuntimeError, match="current segment plan is not approved"):
        await prepare_segment_submission(
            ctx, production_id, revised.metadata["segments"][0]["segment_id"]
        )

    shortened_script = "上海清晨从梧桐树影开始"
    revised_with_dialogue = await execute_project(
        VideoProjectArgs(
            action="revise_segment",
            production_id=production_id,
            segment_id=revised.metadata["segments"][0]["segment_id"],
            script_text=shortened_script,
            revision_reason="缩短疑似念错的台词",
        ),
        ctx,
    )
    active = revised_with_dialogue.metadata["segments"][0]
    assert active["revision"] == 3
    assert active["script_text"] == shortened_script
    assert f"@{shortened_script}" in active["prompt"]
    assert f"@{script}" not in active["prompt"]
    assert revised_with_dialogue.metadata["script_text"] == shortened_script
    assert revised_with_dialogue.metadata["approvals"]["script"] is False
    assert revised_with_dialogue.metadata["approvals"]["segments"] is False

    from db.models.video_production import VideoSegment

    async with get_db_session() as db:
        paid_source = await db.get(VideoSegment, segment_id)
        assert paid_source is not None
        assert paid_source.is_active is False
        assert paid_source.output_asset_id == output_id
