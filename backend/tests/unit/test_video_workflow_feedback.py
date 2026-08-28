"""Per-segment user feedback, session resolution, and replan guards."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import tool.video_workflow as wf
from db.base import get_db_session
from db.models.user import User
from db.models.video_production import VideoSegment
from tool.tool import ToolContext
from tool.video_workflow import VideoProjectArgs, execute_project


async def _seed(status: str = "generated", output_asset_id: str | None = "asset_out"):
    """A user + production + one active segment inserted directly."""
    suffix = uuid4().hex[:10]
    user_id = f"user_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"vf-{suffix}", created_at=now, updated_at=now))
    ctx = ToolContext(session_id=f"session_{suffix}", user_id=user_id, message_id="m1", part_id="p1")
    created = await execute_project(
        VideoProjectArgs(action="create", title="题目", brief="简介"), ctx
    )
    production_id = created.metadata["production_id"]
    segment_id = f"segment_{suffix}"
    async with get_db_session() as db:
        db.add(
            VideoSegment(
                id=segment_id,
                production_id=production_id,
                ordinal=1,
                revision=1,
                is_active=True,
                role="body",
                script_text="一句话",
                prompt="固定镜头@一句话",
                content_hash="hash1",
                input_asset_ids=[],
                lint_data={},
                status=status,
                output_asset_id=output_asset_id,
                created_at=now,
                updated_at=now,
            )
        )
    return ctx, production_id, segment_id


@pytest.mark.asyncio
async def test_set_segment_feedback_persists_status_and_note():
    ctx, production_id, segment_id = await _seed()
    result = await execute_project(
        VideoProjectArgs(
            action="set_segment_feedback",
            production_id=production_id,
            segment_id=segment_id,
            feedback="rejected",
            feedback_note="口型和台词对不上",
        ),
        ctx,
    )
    segment = next(
        row for row in result.metadata["segments"] if row["segment_id"] == segment_id
    )
    assert segment["review_status"] == "user_rejected"
    assert segment["review_note"] == "口型和台词对不上"
    assert f"segment_1_review_note=口型和台词对不上" in result.output


def test_rejected_feedback_requires_note():
    with pytest.raises(ValueError, match="feedback_note"):
        VideoProjectArgs(
            action="set_segment_feedback", segment_id="s", feedback="rejected"
        )
    # Approved needs no note.
    VideoProjectArgs(action="set_segment_feedback", segment_id="s", feedback="approved")


@pytest.mark.asyncio
async def test_feedback_rejects_non_generated_and_stale_segments():
    ctx, production_id, _segment_id = await _seed(status="planned", output_asset_id=None)
    not_reviewable = await execute_project(
        VideoProjectArgs(
            action="set_segment_feedback",
            production_id=production_id,
            segment_id=_segment_id,
            feedback="approved",
        ),
        ctx,
    )
    assert not_reviewable.title == "Segment not reviewable"

    stale = await execute_project(
        VideoProjectArgs(
            action="set_segment_feedback",
            production_id=production_id,
            segment_id="segment_that_never_existed",
            feedback="approved",
        ),
        ctx,
    )
    assert stale.title == "Active segment not found"


@pytest.mark.asyncio
async def test_feedback_is_ownership_scoped():
    ctx, production_id, segment_id = await _seed()
    now = datetime.now(timezone.utc)
    intruder_id = f"user_{uuid4().hex[:10]}"
    async with get_db_session() as db:
        db.add(User(id=intruder_id, username=intruder_id, created_at=now, updated_at=now))
    intruder = ToolContext(session_id="other_session", user_id=intruder_id)
    result = await execute_project(
        VideoProjectArgs(
            action="set_segment_feedback",
            production_id=production_id,
            segment_id=segment_id,
            feedback="approved",
        ),
        intruder,
    )
    assert result.title == "Video production not found"


@pytest.mark.asyncio
async def test_user_rejected_routes_to_needs_segment_revision(monkeypatch):
    async def always_approved(_db, _production_id, _kind, _scope):
        return SimpleNamespace(max_calls=5, used_calls=0)

    monkeypatch.setattr(wf, "_matching_approval", always_approved)
    production = SimpleNamespace(
        id="prod", script_hash="sh", plan_hash="ph", quality_policy="required",
        render_asset_id=None, resolution="720p", ratio="9:16", channel_name="",
    )
    segment = SimpleNamespace(
        id="seg", status="generated", output_asset_id="asset", review_status="user_rejected",
        stt_verdict="ok", transcript_text="t", stt_similarity=0.95,
    )
    status = await wf._derive_status(None, production, [segment])
    assert status == "needs_segment_revision"
    # An approved segment sails on toward quality/render.
    segment.review_status = "user_approved"
    assert await wf._derive_status(None, production, [segment]) != "needs_segment_revision"


# ── session resolution ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_without_production_id_resolves_sessions_production():
    ctx, production_id, _segment_id = await _seed()
    result = await execute_project(VideoProjectArgs(action="status"), ctx)
    assert result.metadata.get("production_id") == production_id


@pytest.mark.asyncio
async def test_missing_session_resolution_gives_actionable_error():
    now = datetime.now(timezone.utc)
    user_id = f"user_{uuid4().hex[:10]}"
    async with get_db_session() as db:
        db.add(User(id=user_id, username=user_id, created_at=now, updated_at=now))
    ctx = ToolContext(session_id=f"session_{uuid4().hex[:8]}", user_id=user_id)
    result = await execute_project(VideoProjectArgs(action="status"), ctx)
    assert result.title == "Video production not found"
    assert "Never retry with a guessed id" in result.output


@pytest.mark.asyncio
async def test_explicit_production_id_still_wins():
    ctx_a, production_a, _seg = await _seed()
    # A second production in the same session becomes the "active" one…
    created = await execute_project(
        VideoProjectArgs(action="create", title="第二部", brief="另一支"), ctx_a
    )
    assert created.metadata["production_id"] != production_a
    # …but an explicit id still targets the first.
    result = await execute_project(
        VideoProjectArgs(action="status", production_id=production_a), ctx_a
    )
    assert result.metadata["production_id"] == production_a


# ── replan guards ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_script_refuses_to_deactivate_generated_without_allow_replan():
    ctx, production_id, segment_id = await _seed(status="generated")
    refusal = await execute_project(
        VideoProjectArgs(
            action="set_script", production_id=production_id, script_text="全新讲稿"
        ),
        ctx,
    )
    assert refusal.title == "Replan confirmation required"
    # The old segment survives, still active.
    async with get_db_session() as db:
        from sqlalchemy import select

        row = (
            await db.execute(select(VideoSegment).where(VideoSegment.id == segment_id))
        ).scalar_one()
        assert row.is_active is True

    allowed = await execute_project(
        VideoProjectArgs(
            action="set_script",
            production_id=production_id,
            script_text="全新讲稿",
            allow_replan=True,
        ),
        ctx,
    )
    assert allowed.title == "Video production status"
    async with get_db_session() as db:
        from sqlalchemy import select

        row = (
            await db.execute(select(VideoSegment).where(VideoSegment.id == segment_id))
        ).scalar_one()
        # Deactivated, never deleted: the paid output stays as an inactive revision.
        assert row.is_active is False
        assert row.output_asset_id == "asset_out"


@pytest.mark.asyncio
async def test_replan_hard_refused_while_jobs_in_flight():
    ctx, production_id, _segment_id = await _seed(status="generating", output_asset_id=None)
    refusal = await execute_project(
        VideoProjectArgs(
            action="set_script",
            production_id=production_id,
            script_text="全新讲稿",
            allow_replan=True,  # even the explicit flag cannot override money in flight
        ),
        ctx,
    )
    assert refusal.title == "Segments still running"
