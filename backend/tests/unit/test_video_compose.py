"""video_compose: own the assets, compile safely, submit once, settle durably."""
import json
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import tool.video_compose as vc
from tool.tool import ToolContext
from tool.video_compose import VideoComposeArgs, execute_compose
from video.ims_client import ImsJobState

BUCKET = "bossip-media-sh"
HOST = f"{BUCKET}.oss-cn-shanghai.aliyuncs.com"


class FakeOss:
    bucket = BUCKET
    host = HOST

    def __init__(self):
        self.heads: dict[str, dict] = {}

    async def head(self, key):
        return self.heads.get(key)


class FakeIms:
    def __init__(self):
        self.submits: list[dict] = []
        self.states: dict[str, ImsJobState] = {}

    async def submit(self, **kw):
        self.submits.append(kw)
        job_id = f"ims-{len(self.submits)}"
        self.states[job_id] = ImsJobState(job_id, "in_progress", "Processing", None, None, None, None)
        return job_id

    async def get(self, job_id):
        return self.states[job_id]

    def finish(self, job_id, *, ok=True):
        self.states[job_id] = (
            ImsJobState(job_id, "completed", "Success", f"https://{HOST}/x.mp4", 9.5, "", "")
            if ok else ImsJobState(job_id, "failed", "Failed", None, None, "InvalidMaterial", "media not found")
        )


@pytest.fixture
def env(monkeypatch):
    oss, ims = FakeOss(), FakeIms()
    monkeypatch.setattr("core.oss.get_oss", lambda: oss)
    monkeypatch.setattr(vc.ims_client, "submit_media_producing_job", ims.submit)
    monkeypatch.setattr(vc.ims_client, "get_media_producing_job", ims.get)
    # No chat attachment or sandbox delivery in unit tests.
    monkeypatch.setattr("tool.video_production._attach_completed", _noop_false)
    monkeypatch.setattr("tool.video_production._try_materialize", _noop_none)
    return oss, ims


async def _noop_false(*_a, **_k):
    return False


async def _noop_none(*_a, **_k):
    return None


async def _user_with_asset(*, mime="video/mp4", status="ready"):
    # The unit DB is sqlite without FK enforcement (see test_video_job_recovery),
    # so users/workspaces need no rows of their own.
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    uid = "u_" + uuid.uuid4().hex[:8]
    wid = "w_" + uuid.uuid4().hex[:8]
    aid = "asset_" + uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(FileAsset(id=aid, user_id=uid, workspace_id=wid, session_id=None, project_id=None, name="shot1.mp4",
                         oss_key=f"assets/{uid}/{aid}/shot1.mp4", mime=mime, size=10, status=status, source="agent",
                         transient=False, created_at=now))
    return ToolContext(user_id=uid, workspace_id=wid, session_id="s1", message_id="m1"), aid


def _timeline(asset_ref, second_ref=None):
    shots = [{"asset": asset_ref, "duration_sec": 5, "transition_out": {"type": "fade", "seconds": 0.5}}]
    shots.append({"asset": second_ref or asset_ref, "duration_sec": 5})
    return {
        "shots": shots,
        "captions": [{"from_sec": 0.2, "to_sec": 2.4, "text": "第一句"}],
        "texts": [{"text": "钩子", "from_sec": 0, "to_sec": 3, "y": 0.125,
                   "style": {"size": 56, "color": "#FFD700"},
                   "motion": {"in_effect": "slide_down_in", "in_sec": 0.5, "out_effect": "fade_out", "out_sec": 0.6}}],
    }


def _kv(result):
    return dict(line.split("=", 1) for line in result.output.splitlines() if "=" in line)


# ── args ─────────────────────────────────────────────────────────────────────

def test_submit_requires_key_and_timeline_and_job_actions_require_job_id():
    with pytest.raises(ValidationError, match="idempotency_key"):
        VideoComposeArgs(action="submit", timeline={"shots": []})
    with pytest.raises(ValidationError, match="requires timeline"):
        VideoComposeArgs(action="validate")
    with pytest.raises(ValidationError, match="requires job_id"):
        VideoComposeArgs(action="wait")
    VideoComposeArgs(action="schema")


# ── schema / validate ────────────────────────────────────────────────────────

async def test_schema_action_returns_json_schema_and_verified_names(env):
    result = await execute_compose(VideoComposeArgs(action="schema"), ToolContext(user_id="u"))
    assert "shots" in result.metadata["schema"]["properties"]
    assert "fade" in result.output and "slide_down_in" in result.output


async def test_validate_resolves_owned_asset_and_reports_duration(env):
    ctx, aid = await _user_with_asset()
    result = await execute_compose(VideoComposeArgs(action="validate", timeline=_timeline(aid)), ctx)
    assert result.metadata["valid"] is True
    assert _kv(result)["duration_sec"] == "9.5"


async def test_validate_rejects_bad_timeline_with_field_path(env):
    ctx, aid = await _user_with_asset()
    bad = _timeline(aid)
    bad["texts"][0]["motion"]["in_effect"] = "explode_in"
    result = await execute_compose(VideoComposeArgs(action="validate", timeline=bad), ctx)
    assert result.metadata["valid"] is False
    assert "explode_in" in result.output
    result = await execute_compose(VideoComposeArgs(action="validate", timeline={"shots": [{"asset": aid, "AdaptMode": "Cover"}]}), ctx)
    assert "not valid" in result.output and "AdaptMode" in result.output


# ── ownership ────────────────────────────────────────────────────────────────

async def test_foreign_asset_id_and_foreign_oss_keys_are_refused(env):
    ctx, _aid = await _user_with_asset()
    _other_ctx, other_aid = await _user_with_asset()
    for ref in (other_aid,
                f"https://{HOST}/assets/{_other_ctx.user_id}/{other_aid}/shot1.mp4",
                f"oss://{BUCKET}/assets/{_other_ctx.user_id}/x.mp4",
                f"https://other-bucket.oss-cn-shanghai.aliyuncs.com/assets/{ctx.user_id}/x.mp4",
                "https://imgur.com/x.mp4"):
        result = await execute_compose(VideoComposeArgs(action="validate", timeline=_timeline(ref)), ctx)
        assert result.metadata["valid"] is False, ref


async def test_own_oss_url_and_own_asset_id_both_resolve_to_https(env):
    oss, ims = env
    ctx, aid = await _user_with_asset()
    own_url = f"oss://{BUCKET}/assets/{ctx.user_id}/{aid}/shot1.mp4"
    result = await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid, own_url)), ctx)
    assert result.metadata["status"] == "in_progress"
    sent = json.loads(ims.submits[0]["timeline"])
    urls = [c["MediaURL"] for c in sent["VideoTracks"][0]["VideoTrackClips"]]
    assert urls == [f"https://{HOST}/assets/{ctx.user_id}/{aid}/shot1.mp4"] * 2


async def test_non_ready_or_non_media_assets_are_refused(env):
    ctx, aid = await _user_with_asset(status="pending")
    result = await execute_compose(VideoComposeArgs(action="validate", timeline=_timeline(aid)), ctx)
    assert "not a ready asset" in result.output
    ctx, aid = await _user_with_asset(mime="audio/mpeg")
    result = await execute_compose(VideoComposeArgs(action="validate", timeline=_timeline(aid)), ctx)
    assert "must be video or image" in result.output


# ── submit ───────────────────────────────────────────────────────────────────

async def test_submit_reserves_job_and_asset_then_sends_compiled_request(env):
    oss, ims = env
    ctx, aid = await _user_with_asset()
    result = await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid), filename="成片 v1.mp4"), ctx)
    kv = _kv(result)
    assert kv["status"] == "in_progress" and kv["provider_task_id"] == "ims-1"
    sent = ims.submits[0]
    output = json.loads(sent["output_media_config"])
    assert output["MediaURL"].startswith(f"https://{HOST}/assets/{ctx.user_id}/") and output["MediaURL"].endswith("/成片_v1.mp4")
    assert (output["Width"], output["Height"]) == (720, 1280)
    clip = json.loads(sent["timeline"])["VideoTracks"][0]["VideoTrackClips"][0]
    assert (clip["X"], clip["Y"], clip["Width"], clip["Height"], clip["AdaptMode"]) == (0, 0, 720, 1280, "Cover")
    assert sent["client_token"].startswith("obx-") and len(sent["client_token"]) <= 64
    assert json.loads(sent["user_data"])["openbox_job"] == kv["job_id"]

    from db.base import get_db_session
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        job = await db.get(VideoJob, kv["job_id"])
    assert job.kind == "compose" and job.model == "ims" and job.output_asset_id
    assert job.request_data["ims_timeline"]["VideoTracks"] and job.request_data["duration_sec"] == 9.5


async def test_same_idempotency_key_submits_once_and_conflicting_content_is_refused(env):
    oss, ims = env
    ctx, aid = await _user_with_asset()
    first = await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)
    again = await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)
    assert again.metadata["job_id"] == first.metadata["job_id"]
    assert len(ims.submits) == 1
    changed = _timeline(aid)
    changed["captions"][0]["text"] = "改了"
    conflict = await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=changed), ctx)
    assert "conflict" in conflict.output and len(ims.submits) == 1


async def test_ims_submit_failure_fails_job_and_asset(env, monkeypatch):
    oss, ims = env
    ctx, aid = await _user_with_asset()
    from video.ims_client import ImsNotActivated

    async def boom(**_kw):
        raise ImsNotActivated("Forbidden", "IMS is not activated")
    monkeypatch.setattr(vc.ims_client, "submit_media_producing_job", boom)
    result = await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)
    assert "not activated" in result.output
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        job = await db.get(VideoJob, result.metadata["job_id"])
        asset = await db.get(FileAsset, job.output_asset_id)
    assert job.status == "failed" and asset.status == "failed"


async def test_daily_limit_is_back_pressure(env, monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config().video_compose, "daily_job_limit", 1)
    ctx, aid = await _user_with_asset()
    await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)
    result = await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-2", timeline=_timeline(aid)), ctx)
    assert "daily composition limit" in result.output


# ── status / wait / cancel ───────────────────────────────────────────────────

async def test_wait_completes_marks_asset_ready_with_size(env):
    oss, ims = env
    ctx, aid = await _user_with_asset()
    submitted = await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)
    job_id = submitted.metadata["job_id"]
    running = await execute_compose(VideoComposeArgs(action="status", job_id=job_id), ctx)
    assert running.metadata["still_running"] is True and _kv(running)["status"] == "in_progress"

    from db.base import get_db_session
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        job = await db.get(VideoJob, job_id)
        from db.models.file_asset import FileAsset
        asset = await db.get(FileAsset, job.output_asset_id)
    oss.heads[asset.oss_key] = {"size": 1234567}
    ims.finish("ims-1")
    done = await execute_compose(VideoComposeArgs(action="wait", job_id=job_id, wait_seconds=1), ctx)
    kv = _kv(done)
    assert kv["status"] == "completed" and kv["asset_id"] == asset.id and kv["bytes"] == "1234567"
    assert "download_url" in kv and kv["duration_sec"] == "9.5"
    assert done.metadata["still_running"] is False


async def test_ims_failure_surfaces_code_and_fails_asset(env):
    oss, ims = env
    ctx, aid = await _user_with_asset()
    job_id = (await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)).metadata["job_id"]
    ims.finish("ims-1", ok=False)
    result = await execute_compose(VideoComposeArgs(action="status", job_id=job_id), ctx)
    kv = _kv(result)
    assert kv["status"] == "failed" and "InvalidMaterial" in kv["error"]


async def test_polling_pauses_after_the_inline_budget(env):
    ctx, aid = await _user_with_asset()
    job_id = (await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)).metadata["job_id"]
    result = await execute_compose(VideoComposeArgs(action="wait", job_id=job_id, wait_seconds=0, wait_iteration=13), ctx)
    assert result.metadata["polling_paused"] is True and result.metadata["do_not_resubmit"] is True
    assert "polling_paused=true" in result.output


async def test_cancel_closes_job_locally_and_hides_asset(env):
    ctx, aid = await _user_with_asset()
    job_id = (await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)).metadata["job_id"]
    result = await execute_compose(VideoComposeArgs(action="cancel", job_id=job_id), ctx)
    assert _kv(result)["status"] == "cancelled" and "IMS cannot cancel" in result.output


async def test_jobs_are_scoped_to_their_owner(env):
    ctx, aid = await _user_with_asset()
    job_id = (await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)).metadata["job_id"]
    other, _ = await _user_with_asset()
    result = await execute_compose(VideoComposeArgs(action="status", job_id=job_id), other)
    assert "not found" in result.title.lower()


# ── recovery ─────────────────────────────────────────────────────────────────

async def test_recovery_sweep_settles_stale_in_flight_compositions(env):
    from datetime import timedelta

    oss, ims = env
    ctx, aid = await _user_with_asset()
    job_id = (await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)).metadata["job_id"]
    from sqlalchemy import update

    from db.base import get_db_session
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        await db.execute(update(VideoJob).where(VideoJob.id == job_id).values(
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=10)))
    ims.finish("ims-1")
    from video import compose_recovery
    assert await compose_recovery.sweep() == 1
    async with get_db_session() as db:
        job = await db.get(VideoJob, job_id)
    assert job.status == "completed"


# ── wiring ───────────────────────────────────────────────────────────────────

def test_tool_is_registered_exposed_and_build_only():
    from agent.agent import AGENTS, BUILD_ONLY_WORKFLOW_TOOLS
    from agent.doom_loop import is_repeatable_poll
    from agent.tool_exposure import INTENT_PACKS

    assert "video_compose" in BUILD_ONLY_WORKFLOW_TOOLS
    assert "video_compose" in AGENTS["build"].tools
    assert "video_compose" in INTENT_PACKS["video"]
    assert is_repeatable_poll("video_compose", {"action": "wait", "job_id": "x"})
    assert not is_repeatable_poll("video_compose", {"action": "submit"})


async def test_missing_oss_configuration_is_named_not_swallowed(monkeypatch):
    from core.oss import OssNotConfigured

    def boom():
        raise OssNotConfigured("OSS_BUCKET is not set")
    monkeypatch.setattr("core.oss.get_oss", boom)
    ctx = ToolContext(user_id="u", workspace_id="w")
    result = await execute_compose(VideoComposeArgs(action="validate", timeline={"shots": [{"asset": "a"}]}), ctx)
    assert "OSS_BUCKET" in result.output and "OSS_REGION" in result.output
    result = await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline={"shots": [{"asset": "a"}]}), ctx)
    assert "OSS_BUCKET" in result.output


async def test_validate_quotes_credits_and_completion_records_a_shadow_usage_event(env, monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "shadow")
    oss, ims = env
    ctx, aid = await _user_with_asset()
    quoted = await execute_compose(VideoComposeArgs(action="validate", timeline=_timeline(aid)), ctx)
    kv = _kv(quoted)
    assert kv["estimated_credits"] == "0.03" and kv["minutes_billed"] == "1" and kv["tier"] == "720p"
    job_id = (await execute_compose(VideoComposeArgs(action="submit", idempotency_key="key-1", timeline=_timeline(aid)), ctx)).metadata["job_id"]
    ims.finish("ims-1")
    done = await execute_compose(VideoComposeArgs(action="wait", job_id=job_id, wait_seconds=1), ctx)
    assert _kv(done)["credits"] == "0.03"
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.billing import UsageEvent
    async with get_db_session() as db:
        event = (await db.execute(select(UsageEvent).where(UsageEvent.idempotency_key == f"compose:{job_id}"))).scalar_one()
    assert event.status == "shadow" and event.workspace_id == ctx.workspace_id and event.kind == "video_compose"
