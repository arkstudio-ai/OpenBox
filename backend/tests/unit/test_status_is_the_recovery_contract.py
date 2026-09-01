"""Regression anchor for rebuilding a video turn from ``video_project status``."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.user import User
from db.models.video_job import VideoJob
from db.models.video_production import VideoApproval, VideoProduction, VideoSegment
from tool.tool import ToolContext
from tool.video_workflow import (
    VideoProjectArgs,
    _plan_hash,
    content_hash,
    execute_project,
    quality_scope,
    render_idempotency_key,
    render_scope,
    spend_scope,
)


@pytest.mark.asyncio
async def test_status_is_the_complete_recovery_contract():
    """A restarted agent must recover safely from status alone, without memory or job APIs."""
    suffix = uuid4().hex[:10]
    user_id = f"user_recovery_{suffix}"
    session_id = f"session_recovery_{suffix}"
    production_id = f"production_recovery_{suffix}"
    first_segment_id = f"segment_recovery_1_{suffix}"
    second_segment_id = f"segment_recovery_2_{suffix}"
    first_job_id = f"video_recovery_1_{suffix}"
    second_job_id = f"video_recovery_2_{suffix}"
    output_asset_id = f"asset_recovery_{suffix}"
    now = datetime.now(timezone.utc)
    script = "第一段介绍主题，第二段给出结论。"
    script_hash = content_hash({"script_text": script})
    anchor = "同一位虚拟主持人在明亮演播室，人物造型和机位全程一致"

    production = VideoProduction(
        id=production_id,
        user_id=user_id,
        session_id=session_id,
        project_id=None,
        title="恢复契约测试",
        brief="验证重启后只凭 status 续跑",
        mode="standard",
        status="spend_ok",
        target_duration_seconds=30,
        ratio="9:16",
        resolution="720p",
        quality_policy="required",
        subtitles=None,
        channel_name="",
        visual_anchor=anchor,
        character_asset_id=None,
        script_text=script,
        script_hash=script_hash,
        plan_hash="",
        render_asset_id=None,
        error=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    first = VideoSegment(
        id=first_segment_id,
        production_id=production_id,
        ordinal=1,
        revision=1,
        is_active=True,
        role="hook",
        script_text="第一段介绍主题，",
        prompt="第一段完整提示词",
        content_hash=content_hash({"segment": 1}),
        model="doubao-seedance-2-0-260128",
        input_asset_ids=[],
        lint_data={"ok": True},
        status="generated",
        generation_job_id=first_job_id,
        output_asset_id=output_asset_id,
        transcript_text=None,
        transcript_data={},
        stt_similarity=None,
        stt_verdict=None,
        stt_notes=[],
        stt_checked_at=None,
        review_status=None,
        review_note=None,
        created_at=now,
        updated_at=now,
    )
    second = VideoSegment(
        id=second_segment_id,
        production_id=production_id,
        ordinal=2,
        revision=1,
        is_active=True,
        role="closing",
        script_text="第二段给出结论。",
        prompt="第二段完整提示词",
        content_hash=content_hash({"segment": 2}),
        model="wan3.0-video",
        input_asset_ids=[],
        lint_data={"ok": True},
        status="submitting",
        generation_job_id=second_job_id,
        output_asset_id=None,
        transcript_text=None,
        transcript_data={},
        stt_similarity=None,
        stt_verdict=None,
        stt_notes=[],
        stt_checked_at=None,
        review_status=None,
        review_note=None,
        created_at=now,
        updated_at=now,
    )
    segments = [first, second]
    production.plan_hash = _plan_hash(production, segments)
    scopes = {
        "script": production.script_hash,
        "segments": production.plan_hash,
        "spend": spend_scope(production, segments),
        "quality": quality_scope(production, segments),
        "render": render_scope(production, segments),
    }

    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"recovery-{suffix}", created_at=now, updated_at=now))
        db.add(
            FileAsset(
                id=output_asset_id,
                user_id=user_id,
                session_id=session_id,
                project_id=None,
                name="segment-1.mp4",
                oss_key=f"assets/{user_id}/{output_asset_id}/segment-1.mp4",
                mime="video/mp4",
                size=1024,
                status="ready",
                source="agent",
                transient=False,
                created_at=now,
            )
        )
        db.add(production)
        db.add_all(segments)
        db.add_all(
            [
                VideoJob(
                    id=first_job_id,
                    user_id=user_id,
                    session_id=session_id,
                    project_id=None,
                    kind="segment",
                    production_id=production_id,
                    segment_id=first_segment_id,
                    idempotency_key=f"{production_id}:{first_segment_id}:generate",
                    request_hash="first-request",
                    status="completed",
                    model=first.model,
                    provider_task_id="provider-completed",
                    sandbox_job_id=None,
                    prompt=first.prompt,
                    request_data={},
                    result_data={},
                    output_asset_id=output_asset_id,
                    error=None,
                    attempt=1,
                    attached_message_id=None,
                    created_at=now,
                    updated_at=now,
                    started_at=now,
                    completed_at=now,
                ),
                VideoJob(
                    id=second_job_id,
                    user_id=user_id,
                    session_id=session_id,
                    project_id=None,
                    kind="segment",
                    production_id=production_id,
                    segment_id=second_segment_id,
                    idempotency_key=f"{production_id}:{second_segment_id}:generate",
                    request_hash="second-request",
                    status="submitting",
                    model=second.model,
                    provider_task_id=None,
                    sandbox_job_id=None,
                    prompt=second.prompt,
                    request_data={},
                    result_data={},
                    output_asset_id=None,
                    error=None,
                    attempt=0,
                    attached_message_id=None,
                    created_at=now,
                    updated_at=now,
                    started_at=None,
                    completed_at=None,
                ),
            ]
        )
        approval_scopes = {
            "script": scopes["script"],
            "segments": scopes["segments"],
            "spend": scopes["spend"],
            "quality": "stale-quality-scope",
            "render": "stale-render-scope",
        }
        for index, (kind, scope_hash) in enumerate(approval_scopes.items(), start=1):
            db.add(
                VideoApproval(
                    id=f"approval_recovery_{index}_{suffix}",
                    production_id=production_id,
                    user_id=user_id,
                    session_id=session_id,
                    kind=kind,
                    scope_hash=scope_hash,
                    decision="approved",
                    answer="测试批准",
                    evidence_message_id=None,
                    evidence_part_id=None,
                    metadata_data={},
                    created_at=now,
                )
            )
        # A negative decision can be evidence for the exact current hash while
        # correctly leaving the render gate closed. Recovery must not call that
        # evidence "stale" merely because it was not an approval.
        db.add(
            VideoApproval(
                id=f"approval_recovery_rejected_{suffix}",
                production_id=production_id,
                user_id=user_id,
                session_id=session_id,
                kind="render",
                scope_hash=scopes["render"],
                decision="rejected",
                answer="先不成片",
                evidence_message_id=None,
                evidence_part_id=None,
                metadata_data={},
                created_at=now + timedelta(microseconds=1),
            )
        )

    ctx = ToolContext(
        session_id=session_id,
        user_id=user_id,
        message_id="message_recovery",
        part_id="part_recovery",
    )
    recovered = await execute_project(VideoProjectArgs(action="status"), ctx)

    assert recovered.metadata["status"] == "generating"
    recovered_segments = recovered.metadata["segments"]
    assert [row["status"] for row in recovered_segments] == ["generated", "submitting"]
    assert [row["generation_job_id"] for row in recovered_segments] == [
        first_job_id,
        second_job_id,
    ]
    assert [row["model"] for row in recovered_segments] == [first.model, second.model]

    assert recovered.metadata["approvals"] == {
        "script": True,
        "segments": True,
        "spend": True,
        "quality": False,
        "render": False,
    }
    details = recovered.metadata["approval_details"]
    assert set(details) == {"script", "segments", "spend", "quality", "render"}
    for kind in ("script", "segments", "spend"):
        assert details[kind]["current_scope_hash"] == scopes[kind]
        assert details[kind]["approval_scope_hash"] == scopes[kind]
        assert details[kind]["decision"] == "approved"
        assert details[kind]["matches_current_hash"] is True
    assert details["quality"]["current_scope_hash"] == scopes["quality"]
    assert details["quality"]["approval_scope_hash"] == "stale-quality-scope"
    assert details["quality"]["decision"] == "approved"
    assert details["quality"]["matches_current_hash"] is False
    assert details["render"]["current_scope_hash"] == scopes["render"]
    assert details["render"]["approval_scope_hash"] == scopes["render"]
    assert details["render"]["decision"] == "rejected"
    assert details["render"]["matches_current_hash"] is True
    assert "spend_budget" not in recovered.metadata

    for row in recovered_segments:
        assert row["generation_idempotency_key"] == (
            f"{production_id}:{row['segment_id']}:generate"
        )
        assert row["transcription_idempotency_key"] == (
            f"{production_id}:{row['segment_id']}:stt"
        )
    assert recovered.metadata["render_idempotency_key"] == render_idempotency_key(
        production_id, scopes["render"]
    )
    # The status payload used to be validated against a JSON Schema bundled
    # with the skill. That file described the tool, so it lived on the wrong
    # side of the knowledge/capability line and went with the skill rewrite;
    # the field-level assertions above and below are the contract.
    assert "spend_budget=" not in recovered.output
    assert f"segment_1_model={first.model}" in recovered.output
    assert f"segment_2_generation_job_id={second_job_id}" in recovered.output
