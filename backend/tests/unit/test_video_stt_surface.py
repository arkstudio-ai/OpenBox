"""STT completion keeps the public Part Surface and AgentEvent log atomic."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from agent.driver import LeaseLostError, reserve_run
from bus.events import PART_UPDATED
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from db.models.video_production import VideoProduction, VideoSegment
from session.agent_event_log import verify_agent_event_parity
from tool.tool import ToolContext
from tool.video_production import _finalize_transcription
from tool.video_workflow import content_hash, record_segment_transcript


async def _seed_stt_surface() -> dict[str, str]:
    suffix = uuid4().hex[:10]
    ids = {
        "user": f"stt_user_{suffix}",
        "project": f"stt_project_{suffix}",
        "session": f"stt_session_{suffix}",
        "message": f"stt_message_{suffix}",
        "part": f"stt_part_{suffix}",
        "asset": f"stt_asset_{suffix}",
        "production": f"stt_prod_{suffix}",
        "segment": f"stt_segment_{suffix}",
    }
    now = datetime.now(timezone.utc)
    script = "上海的清晨从梧桐树影开始"
    initial_part = {
        "id": ids["part"],
        "session_id": ids["session"],
        "message_id": ids["message"],
        "type": "file",
        "asset_id": ids["asset"],
        "name": "segment.mp4",
        "mime": "video/mp4",
        "relation": {"metadata": {"existing": "kept"}},
    }
    async with get_db_session() as db:
        db.add(User(
            id=ids["user"],
            username=ids["user"],
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=ids["project"],
            user_id=ids["user"],
            name="STT project",
            slug=ids["project"],
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=ids["session"],
            user_id=ids["user"],
            project_id=ids["project"],
            title="STT session",
            agent="build",
            model="test/model",
            status="idle",
            token_usage={},
            tool_exposure_state={},
            created_at=now,
            updated_at=now,
        ))
        db.add(Message(
            id=ids["message"],
            session_id=ids["session"],
            user_id=ids["user"],
            role="assistant",
            finish="stop",
            created_at=now,
        ))
        db.add(Part(
            id=ids["part"],
            session_id=ids["session"],
            message_id=ids["message"],
            user_id=ids["user"],
            type="file",
            data=initial_part,
            created_at=now,
        ))
        db.add(FileAsset(
            id=ids["asset"],
            user_id=ids["user"],
            session_id=ids["session"],
            project_id=ids["project"],
            name="segment.mp4",
            oss_key=f"assets/{ids['user']}/{ids['asset']}/segment.mp4",
            mime="video/mp4",
            size=1024,
            status="ready",
            source="agent",
            transient=False,
            created_at=now,
        ))
        db.add(VideoProduction(
            id=ids["production"],
            user_id=ids["user"],
            session_id=ids["session"],
            project_id=ids["project"],
            title="STT production",
            brief="Verify the transcript Surface update",
            mode="standard",
            status="generated",
            target_duration_seconds=30,
            ratio="9:16",
            resolution="720p",
            quality_policy="required",
            subtitles=None,
            channel_name="",
            visual_anchor="same presenter",
            character_asset_id=None,
            character_reference_type="virtual",
            character_identity_id=None,
            script_text=script,
            script_hash=content_hash({"script_text": script}),
            plan_hash="",
            render_asset_id=None,
            error=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        ))
        db.add(VideoSegment(
            id=ids["segment"],
            production_id=ids["production"],
            ordinal=1,
            revision=1,
            is_active=True,
            role="hook",
            script_text=script,
            prompt=f"fixed shot @{script}",
            content_hash=content_hash({"segment": script}),
            model="test-video-model",
            input_asset_ids=[],
            lint_data={"ok": True},
            status="generated",
            generation_job_id="stt-generation-job",
            output_asset_id=ids["asset"],
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
        ))
    ids["script"] = script
    return ids


@pytest.mark.asyncio
async def test_stt_completion_updates_surface_event_and_sse_together(monkeypatch):
    ids = await _seed_stt_surface()
    lease = await reserve_run(ids["session"], ids["user"], run_id="run-stt-surface")
    fence = (ids["session"], lease.run_id, lease.generation)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr("bus.bus._redis_client", None)
    monkeypatch.setattr(
        "bus.bus.publish",
        lambda event_type, payload: published.append((event_type, payload)),
    )

    comparison = await record_segment_transcript(
        ids["segment"],
        ids["script"],
        {"text": ids["script"], "model": "test-asr"},
        user_id=ids["user"],
        threshold=0.90,
        session_id=ids["session"],
        run_fence=fence,
    )

    assert comparison["verdict"] == "ok"
    report = await verify_agent_event_parity(
        ids["session"],
        user_id=ids["user"],
        require_closed=False,
    )
    assert report.ok is True
    assert report.projection_matches is True
    async with get_db_session() as db:
        part = await db.get(Part, ids["part"])
        events = list((await db.execute(
            select(AgentEvent)
            .where(AgentEvent.session_id == ids["session"])
            .order_by(AgentEvent.sequence)
        )).scalars())
    assert part is not None
    metadata = part.data["relation"]["metadata"]
    assert metadata == {
        "existing": "kept",
        "production_id": ids["production"],
        "segment_id": ids["segment"],
        "transcript": ids["script"],
        "stt_verdict": "ok",
        "stt_similarity": 1.0,
    }
    assert [event.kind for event in events] == ["surface.seed", "part.updated"]
    assert events[-1].generation == lease.generation
    assert published == [(PART_UPDATED, {
        "userId": ids["user"],
        "sessionId": ids["session"],
        "messageId": ids["message"],
        "part": {
            key: value
            for key, value in part.data.items()
            if key not in ("session_id", "message_id", "state")
        },
        "generation": lease.generation,
    })]
    assert await lease.release(session_status="idle") is True


@pytest.mark.asyncio
async def test_stale_stt_generation_changes_nothing_and_publishes_nothing(monkeypatch):
    ids = await _seed_stt_surface()
    stale = await reserve_run(ids["session"], ids["user"], run_id="run-stt-stale")
    stale_fence = (ids["session"], stale.run_id, stale.generation)
    assert await stale.release(session_status="idle") is True
    current = await reserve_run(ids["session"], ids["user"], run_id="run-stt-current")
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr("bus.bus._redis_client", None)
    monkeypatch.setattr(
        "bus.bus.publish",
        lambda event_type, payload: published.append((event_type, payload)),
    )

    with pytest.raises(LeaseLostError):
        await record_segment_transcript(
            ids["segment"],
            ids["script"],
            {"text": ids["script"], "model": "test-asr"},
            user_id=ids["user"],
            threshold=0.90,
            session_id=ids["session"],
            run_fence=stale_fence,
        )

    async with get_db_session() as db:
        part = await db.get(Part, ids["part"])
        segment = await db.get(VideoSegment, ids["segment"])
        event_count = (await db.execute(
            select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == ids["session"]
            )
        )).scalar_one()
    assert part is not None
    assert part.data["relation"] == {"metadata": {"existing": "kept"}}
    assert segment is not None
    assert segment.transcript_text is None
    assert event_count == 0
    assert published == []
    assert await current.release(session_status="idle") is True


@pytest.mark.asyncio
async def test_stt_part_and_domain_changes_roll_back_when_event_append_fails(
    monkeypatch,
):
    ids = await _seed_stt_surface()
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr("bus.bus._redis_client", None)
    monkeypatch.setattr(
        "bus.bus.publish",
        lambda event_type, payload: published.append((event_type, payload)),
    )

    async def fail_append(*_args, **_kwargs):
        raise RuntimeError("event append failed")

    monkeypatch.setattr(
        "session.agent_event_log.append_part_event_locked",
        fail_append,
    )

    with pytest.raises(RuntimeError, match="event append failed"):
        await record_segment_transcript(
            ids["segment"],
            ids["script"],
            {"text": ids["script"], "model": "test-asr"},
            user_id=ids["user"],
            threshold=0.90,
            session_id=ids["session"],
        )

    async with get_db_session() as db:
        part = await db.get(Part, ids["part"])
        segment = await db.get(VideoSegment, ids["segment"])
        event_count = (await db.execute(
            select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == ids["session"]
            )
        )).scalar_one()
    assert part is not None
    assert part.data["relation"] == {"metadata": {"existing": "kept"}}
    assert segment is not None
    assert segment.transcript_text is None
    assert event_count == 0
    assert published == []


def test_stt_completion_has_no_raw_part_data_assignment():
    source = inspect.getsource(record_segment_transcript)
    assert "row.data =" not in source


@pytest.mark.asyncio
async def test_stt_finalizer_forwards_and_does_not_swallow_lease_loss(monkeypatch):
    fence = ("stt-session", "stt-run", 7)
    ctx = ToolContext(
        session_id=fence[0],
        user_id="stt-user",
        run_id=fence[1],
        run_generation=fence[2],
    )
    job = SimpleNamespace(
        id="stt-job",
        segment_id="stt-segment",
        result_data={"transcript": {"text": "spoken words"}},
    )
    audio = SimpleNamespace(id="stt-audio", oss_key="audio/key.mp3")
    seen: dict = {}
    updates: list[dict] = []

    class Oss:
        async def head(self, _key):
            return {"size": 123}

    async def fake_job_asset(_job):
        return audio

    async def fake_mark_asset(*_args, **_kwargs):
        return None

    async def reject_stale(*_args, **kwargs):
        seen.update(kwargs)
        raise LeaseLostError("stale STT finalizer")

    async def capture_update(_job_id, **values):
        updates.append(values)

    monkeypatch.setattr("tool.video_production._job_asset", fake_job_asset)
    monkeypatch.setattr("tool.video_production._mark_asset", fake_mark_asset)
    monkeypatch.setattr(
        "tool.video_workflow.record_segment_transcript",
        reject_stale,
    )
    monkeypatch.setattr("tool.video_production._update_job", capture_update)

    with pytest.raises(LeaseLostError, match="stale STT finalizer"):
        await _finalize_transcription(
            job,
            ctx,
            SimpleNamespace(similarity_threshold=0.9),
            Oss(),
            {},
        )

    assert seen["session_id"] == fence[0]
    assert seen["run_fence"] == fence
    assert updates == []
