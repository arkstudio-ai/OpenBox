"""The video model is frozen onto a segment when it is submitted.

Switching models mid-production must never disturb work already under way: a
segment that is generating keeps the model it started with, and a new pick only
reaches segments that have not been submitted yet. The write-back is what makes
that true — without it, a retry or a reconciliation of an in-flight segment
would silently re-resolve and could submit against a different (and differently
priced) model than the one the user approved and started.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from db.base import get_db_session
from db.models.session import Session as SessionORM
from db.models.user import User
from db.models.video_production import VideoProduction, VideoSegment
from tool.video_workflow import resolve_segment_model


async def _seed(
    *,
    session_video_model: str | None,
    segment_model: str | None = None,
    generation_job_id: str | None = None,
):
    suffix = uuid4().hex[:10]
    user_id, session_id = f"user_{suffix}", f"session_{suffix}"
    production_id, segment_id = f"prod_{suffix}", f"seg_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"vm-{suffix}", created_at=now, updated_at=now))
        db.add(SessionORM(
            id=session_id, user_id=user_id, project_id="default",
            video_model=session_video_model, created_at=now, updated_at=now,
        ))
        db.add(VideoProduction(
            id=production_id, user_id=user_id, session_id=session_id,
            title="题目", brief="简介", created_at=now, updated_at=now,
        ))
        db.add(VideoSegment(
            id=segment_id, production_id=production_id, ordinal=1, revision=1,
            is_active=True, role="body", script_text="一句话", prompt="固定镜头@一句话",
            content_hash="h1", input_asset_ids=[], lint_data={}, status="planned",
            model=segment_model, generation_job_id=generation_job_id,
            created_at=now, updated_at=now,
        ))
    return user_id, production_id, segment_id


async def _resolve(user_id, production_id, segment_id):
    async with get_db_session() as db:
        production = await db.get(VideoProduction, production_id)
        segment = await db.get(VideoSegment, segment_id)
        return await resolve_segment_model(db, production, segment, user_id)


@pytest.mark.asyncio
async def test_the_conversations_pick_is_used_when_the_segment_has_none():
    ids = await _seed(session_video_model="wan3.0-video")
    assert await _resolve(*ids) == "wan3.0-video"


@pytest.mark.asyncio
async def test_a_segment_already_submitted_ignores_a_later_switch():
    """In-flight immunity: a retry must not be retargeted by a later switch.

    "Already submitted" is what having a generation job means — the runtime
    froze the model when it went out, so a reconciliation resubmits the same
    thing rather than whatever the composer says now.
    """
    ids = await _seed(
        session_video_model="wan3.0-video-prime",
        segment_model="wan3.0-video",
        generation_job_id="video_already_running",
    )
    assert await _resolve(*ids) == "wan3.0-video"


@pytest.mark.asyncio
async def test_an_agents_guess_never_beats_the_composer_pick():
    """A planned segment carries a suggestion, not a decision.

    Agents fill this field in unbidden: one sent the picker's tier label
    ("standard") as a model id, then settled on video-sd-1080p-pro while the
    person had plainly selected Wan 3.0. The control the person operated wins.
    """
    ids = await _seed(session_video_model="wan3.0-video", segment_model="video-sd-1080p-pro")
    assert await _resolve(*ids) == "wan3.0-video"


@pytest.mark.asyncio
async def test_a_planned_model_still_applies_when_nobody_picked():
    """With no pick to respect, the planned value is the best information."""
    ids = await _seed(session_video_model=None, segment_model="video-sd-1080p-pro")
    assert await _resolve(*ids) == "video-sd-1080p-pro"


@pytest.mark.asyncio
async def test_no_pick_anywhere_falls_through_to_the_deployment_default():
    """None, not a guessed id — the caller then uses video_generation.model."""
    ids = await _seed(session_video_model=None)
    assert await _resolve(*ids) is None
    ids = await _seed(session_video_model="")
    assert await _resolve(*ids) is None


@pytest.mark.asyncio
async def test_another_users_session_never_supplies_the_model():
    """Ownership is checked even though the production was reached by id."""
    _, production_id, segment_id = await _seed(session_video_model="wan3.0-video")
    assert await _resolve("user_someone_else", production_id, segment_id) is None


@pytest.mark.asyncio
async def test_a_production_without_a_session_falls_through():
    suffix = uuid4().hex[:10]
    user_id = f"user_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"vm-{suffix}", created_at=now, updated_at=now))
        db.add(VideoProduction(
            id=f"prod_{suffix}", user_id=user_id, session_id=None,
            title="题目", brief="简介", created_at=now, updated_at=now,
        ))
        db.add(VideoSegment(
            id=f"seg_{suffix}", production_id=f"prod_{suffix}", ordinal=1, revision=1,
            is_active=True, role="body", script_text="x", prompt="x",
            content_hash="h", input_asset_ids=[], lint_data={}, status="planned",
            created_at=now, updated_at=now,
        ))
    assert await _resolve(user_id, f"prod_{suffix}", f"seg_{suffix}") is None


@pytest.mark.asyncio
async def test_submission_freezes_the_pick_onto_the_segment(monkeypatch):
    """The write-back, end to end through the real approval chain.

    Resolving without persisting would leave the segment unpinned: a retry or
    reconciliation after the user switched models would re-resolve and submit
    against the new one, spending on a model the in-flight work never approved.
    """
    from db.models.file_asset import FileAsset
    from tool.tool import ToolContext
    from tool.video_workflow import (
        SegmentSpec,
        VideoProjectArgs,
        execute_project,
        prepare_segment_submission,
    )

    suffix = uuid4().hex[:10]
    user_id, session_id = f"user_{suffix}", f"session_{suffix}"
    portrait_id = f"asset_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"vs-{suffix}", created_at=now, updated_at=now))
        db.add(SessionORM(
            id=session_id, user_id=user_id, project_id="default",
            video_model="wan3.0-video", created_at=now, updated_at=now,
        ))
        db.add(FileAsset(
            id=portrait_id, user_id=user_id, session_id=None, project_id=None,
            name="portrait.png", oss_key=f"assets/{user_id}/{portrait_id}/portrait.png",
            mime="image/png", size=100, status="ready", source="agent",
            transient=False, created_at=now,
        ))

    async def approve_first(*, questions, **_kwargs):
        return [[questions[0].options[0].label]]

    monkeypatch.setattr("question.question.ask", approve_first)
    ctx = ToolContext(session_id=session_id, user_id=user_id, message_id="m1", part_id="p1")

    created = await execute_project(
        VideoProjectArgs(action="create", title="上海旅行", brief="制作上海旅游口播短视频"), ctx
    )
    production_id = created.metadata["production_id"]
    script = "上海的清晨从梧桐树影开始"
    await execute_project(
        VideoProjectArgs(action="set_script", production_id=production_id, script_text=script), ctx
    )
    await execute_project(
        VideoProjectArgs(action="request_approval", production_id=production_id,
                         approval_kind="script"), ctx
    )
    anchor = "参考图片1的人物坐在明亮整洁的旅行分享区，人物造型和机位全程一致"
    prompt = (
        f"固定镜头中景，{anchor}，面对镜头开口说出@{script}，"
        "手势随语气自然舒展，语气亲切，无字幕"
    )
    planned = await execute_project(
        VideoProjectArgs(
            action="set_segments", production_id=production_id, visual_anchor=anchor,
            character_reference_asset=portrait_id,
            segments=[SegmentSpec(ordinal=1, role="hook", script_text=script, prompt=prompt)],
        ),
        ctx,
    )
    segment_id = planned.metadata["segments"][0]["segment_id"]
    for kind in ("segments", "spend"):
        await execute_project(
            VideoProjectArgs(action="request_approval", production_id=production_id,
                             approval_kind=kind), ctx
        )

    async with get_db_session() as db:
        assert (await db.get(VideoSegment, segment_id)).model is None

    gate = await prepare_segment_submission(ctx, production_id, segment_id)
    assert gate["model"] == "wan3.0-video"

    async with get_db_session() as db:
        assert (await db.get(VideoSegment, segment_id)).model == "wan3.0-video"

    # Once it is out — which is what having a generation job means — a switch
    # must not retarget it. The handler links that job right after this gate.
    async with get_db_session() as db:
        session = await db.get(SessionORM, session_id)
        session.video_model = "wan3.0-video-prime"
        segment = await db.get(VideoSegment, segment_id)
        segment.generation_job_id = "video_already_running"
        await db.commit()
    async with get_db_session() as db:
        production = await db.get(VideoProduction, production_id)
        segment = await db.get(VideoSegment, segment_id)
        assert await resolve_segment_model(db, production, segment, user_id) == "wan3.0-video"


@pytest.mark.asyncio
async def test_spend_card_names_the_model_that_will_be_billed(monkeypatch):
    """The approval card must not say Seedance while Wan is what runs.

    This is the moment the user authorises money. The text used to hard-code
    "Seedance" regardless of the segment's frozen model, so a Wan 3.0 plan was
    approved under the wrong name.
    """
    from tool.video_workflow import _pending_segment_models

    user_id, production_id, segment_id = await _seed(session_video_model="wan3.0-video")
    async with get_db_session() as db:
        production = await db.get(VideoProduction, production_id)
        pending = [await db.get(VideoSegment, segment_id)]
        models = await _pending_segment_models(db, production, pending, user_id)
    # The display name, matching what the composer offered — see the docstring
    # on _pending_segment_models.
    assert models == ["Wan 3.0"]


@pytest.mark.asyncio
async def test_spend_card_falls_back_to_the_deployment_default():
    from core.config import get_config
    from tool.video_workflow import _pending_segment_models

    user_id, production_id, segment_id = await _seed(session_video_model=None)
    async with get_db_session() as db:
        production = await db.get(VideoProduction, production_id)
        pending = [await db.get(VideoSegment, segment_id)]
        models = await _pending_segment_models(db, production, pending, user_id)
    declared = {m.id: (m.name or m.id) for m in get_config().video_generation.models}
    default = get_config().video_generation.model
    assert models == [declared.get(default, default)]
