"""Video production as a builtin skill (rebuild plan §11, PR#14/15).

The generic runtime owns admission, scheduling, waits, retries and recovery;
this package owns the domain: approvals, per-revision paid idempotency, the
provider protocol and OSS finalization. During the migration window the
handlers reuse the tool module's proven helpers (`tool.video_production`,
`tool.video_workflow`) — the platform core still imports neither.

segment.generate runs one segment through bounded, checkpointed steps:

  invocation 1 (no checkpoint): validate approvals → reserve the idempotent
    video_job row → consume the spend approval → assert lease → submit to the
    provider → persist the provider task id → WaitExternal.
  invocation 2+: one status pass — finalize on success (idempotent OSS copy +
    business-table commit), settle provider failure, otherwise WaitExternal
    with the provider's own pace. An ambiguous submit (no provider task id)
    goes to operator review and is never auto-resubmitted (§10.1).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.log import create_logger
from skill_runtime.registry import register_builtin
from skill_runtime.types import (
    Cancelled,
    Failed,
    Succeeded,
    WaitExternal,
    WaitUser,
)

log = create_logger("builtin.video_production")

SKILL_KEY = "builtin:video-production"

OPERATOR_REVIEW_PROMPT = (
    "视频提交结果不明确（没有拿到供应商任务 ID）。请人工核实供应商侧是否已产生付费任务："
    "确认未产生后取消本作业并重新提交新的修订；切勿在未核实前重复提交。"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tool_ctx(ctx):
    # No message_id: chat attachment stays with the conversational surfaces;
    # the job links its output through skill_job_artifacts instead.
    from tool.tool import ToolContext

    return ToolContext(
        session_id=ctx.session_id or "",
        user_id=ctx.user_id,
        project_id=ctx.project_id or "",
    )


def _poll_seconds(settings) -> float:
    return max(5.0, float(getattr(settings, "poll_interval_seconds", 10)))


async def run(ctx, operation: str, payload: dict, checkpoint: dict):
    if operation == "production.status":
        return await _production_status(ctx, payload)
    if operation == "segment.generate":
        return await _segment_generate(ctx, payload, checkpoint)
    return Failed(
        error_code="unknown_operation",
        message=f"video-production has no operation {operation!r}",
    )


# ---------------------------------------------------------------------------
# production.status — read-only derivation (Phase 4)
# ---------------------------------------------------------------------------

async def _production_status(ctx, payload: dict):
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.video_job import VideoJob
    from db.models.video_production import VideoProduction, VideoSegment

    production_id = str(payload.get("production_id") or "")
    async with get_db_session() as db:
        production = (
            await db.execute(
                select(VideoProduction).where(
                    VideoProduction.id == production_id,
                    VideoProduction.user_id == ctx.user_id,
                )
            )
        ).scalar_one_or_none()
        if production is None:
            return Failed(error_code="not_found", message="production is not owned by this user")
        segments = (
            await db.execute(
                select(VideoSegment)
                .where(
                    VideoSegment.production_id == production.id,
                    VideoSegment.is_active == True,  # noqa: E712
                )
                .order_by(VideoSegment.ordinal.asc())
            )
        ).scalars().all()
        jobs = (
            await db.execute(
                select(VideoJob)
                .where(
                    VideoJob.production_id == production.id,
                    VideoJob.user_id == ctx.user_id,
                )
                .order_by(VideoJob.created_at.desc())
            )
        ).scalars().all()

    latest_job_by_segment: dict[str, Any] = {}
    for job in jobs:
        if job.segment_id and job.segment_id not in latest_job_by_segment:
            latest_job_by_segment[job.segment_id] = job

    return Succeeded(
        result={
            "production_id": production.id,
            "status": production.status,
            "title": production.title,
            "segments": [
                {
                    "segment_id": seg.id,
                    "ordinal": seg.ordinal,
                    "revision": seg.revision,
                    "status": seg.status,
                    "output_asset_id": seg.output_asset_id,
                    "job_status": (
                        latest_job_by_segment[seg.id].status
                        if seg.id in latest_job_by_segment
                        else None
                    ),
                    "provider_task_id": (
                        latest_job_by_segment[seg.id].provider_task_id
                        if seg.id in latest_job_by_segment
                        else None
                    ),
                }
                for seg in segments
            ],
        }
    )


# ---------------------------------------------------------------------------
# segment.generate — the paid write path (Phase 5, behind skill_jobs_video_write)
# ---------------------------------------------------------------------------

async def _segment_generate(ctx, payload: dict, checkpoint: dict):
    from tool import video_production as vp

    if checkpoint.get("video_job_id"):
        return await _advance_existing(ctx, checkpoint)

    if await ctx.is_cancel_requested():
        return Cancelled()

    production_id = str(payload.get("production_id") or "")
    segment_id = str(payload.get("segment_id") or "")
    tctx = _tool_ctx(ctx)
    target, settings = vp._configured_target(None)

    from tool import video_workflow as vw

    await ctx.progress(phase="preparing")
    approved = await vw.prepare_segment_submission(tctx, production_id, segment_id)
    domain_key = f"{approved['production_id']}:{approved['segment_id']}:generate"

    prompt = approved["prompt"]
    resolution = approved["resolution"]
    ratio = approved["ratio"]
    duration = approved["duration"]
    generate_audio = approved["generate_audio"]
    watermark = approved["watermark"]
    vp._validate_generation(target.model, resolution, duration, generate_audio)

    inputs, character_reference = await vp._resolve_generation_inputs(
        approved["character_reference_asset"], approved["input_assets"], tctx
    )
    character_reference_type = approved.get("character_reference_type") or "virtual"
    if target.wire_format == "bossip_videos":
        provider_content, material_bindings = vp._relay_provider_inputs(
            inputs,
            character_reference,
            character_reference_type=character_reference_type,
            input_url_ttl_seconds=settings.provider_input_url_ttl_seconds,
        )
    else:
        provider_content, material_bindings = await vp._materialize_provider_inputs(
            inputs,
            character_reference,
            character_reference_type=character_reference_type,
            character_identity_id=approved.get("character_identity_id"),
            ctx=tctx,
        )

    request_data = {
        "production_id": approved["production_id"],
        "segment_id": approved["segment_id"],
        "content_hash": approved["content_hash"],
        "plan_hash": approved["plan_hash"],
        "input_asset_ids": [row.id for row in inputs],
        "character_reference_asset_id": (
            character_reference.id if character_reference else None
        ),
        "character_reference_type": character_reference_type,
        "character_identity_id": approved.get("character_identity_id"),
        "provider_material_bindings": material_bindings,
        "provider_wire_format": target.wire_format,
        "resolution": resolution,
        "ratio": ratio,
        "duration": duration,
        "generate_audio": generate_audio,
        "watermark": watermark,
        "skill_job_id": ctx.job_id,
    }
    request_hash = vw.content_hash(
        {
            "kind": "segment",
            "model": target.model,
            "prompt": prompt,
            "request_data": {k: v for k, v in request_data.items() if k != "skill_job_id"},
        }
    )

    job, asset, created = await vp._create_pending_job(
        ctx=tctx,
        kind="segment",
        idempotency_key=domain_key,
        model=target.model,
        prompt=prompt,
        request_data=request_data,
        filename=None,
        request_hash=request_hash,
        production_id=approved["production_id"],
        segment_id=approved["segment_id"],
    )

    if not created:
        # The revision already has a paid identity: adopt it, never resubmit.
        return await _adopt_existing(ctx, job, settings)

    try:
        await vw.consume_spend_approval(approved["spend_approval_id"])
    except Exception as exc:
        await vp._update_job(
            job.id,
            status="failed",
            error="approved generation call limit is unavailable",
            completed_at=_now(),
        )
        await vp._mark_asset(job.output_asset_id, status="failed")
        return Failed(error_code="spend_approval_unavailable", message=str(exc)[:500])

    await vw.mark_segment_job(approved["segment_id"], job.id, status="submitting")

    provider_payload: dict[str, Any] = {
        "model": target.model,
        "content": [{"type": "text", "text": prompt}, *provider_content],
        "resolution": resolution,
        "ratio": ratio,
        "duration": duration,
        "generate_audio": generate_audio,
        "watermark": watermark,
    }

    # Fencing only guards DB writes; the billable call needs a live lease.
    await ctx.assert_lease()
    try:
        submitted = await vp._provider_submit(target, provider_payload)
    except Exception as exc:
        # Unknown outcome: the POST may or may not have landed. Never resubmit
        # blindly — park for operator review (§10.1 submit_unknown).
        log.warning(f"video submit outcome unknown for job {job.id}: {exc}")
        return WaitUser(
            checkpoint={"video_job_id": job.id},
            prompt=OPERATOR_REVIEW_PROMPT,
            input_schema={"type": "object"},
        )

    state = vp._provider_state(submitted)
    stored_state = "in_progress" if state == "completed" else state
    await vp._update_job(
        job.id,
        provider_task_id=submitted["id"],
        status=stored_state,
        attempt=1,
        started_at=_now(),
        error=None,
    )

    await ctx.progress(
        {"provider_state": state, "provider_task_id": submitted["id"]},
        phase="provider_generate",
    )

    if state == "completed":
        return await _finalize(ctx, job.id, submitted, settings)
    if state in ("failed", "cancelled"):
        return await _settle_provider_terminal(job.id, state, submitted)
    return WaitExternal(
        checkpoint={"video_job_id": job.id},
        wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
        external_handle=submitted["id"],
        progress={"provider_state": state, "provider_task_id": submitted["id"]},
    )


async def _advance_existing(ctx, checkpoint: dict):
    from tool import video_production as vp

    target, settings = vp._configured_target(None)
    video_job_id = checkpoint["video_job_id"]
    job = await _owned_video_job(video_job_id, ctx.user_id)
    if job is None:
        return Failed(error_code="video_job_missing", message=f"video job {video_job_id} vanished")

    if job.status == "completed":
        return await _success(job)
    if job.status in ("failed", "cancelled"):
        return Failed(
            error_code=f"provider_{job.status}",
            message=job.error or f"video job ended {job.status}",
        )

    cancel_requested = await ctx.is_cancel_requested()

    if job.status == "finalizing":
        # A previous invocation (or the stopgap sweep) may still be copying to
        # OSS; only reclaim past the tool's own staleness threshold.
        from video.job_recovery import FINALIZING_STALE_SECONDS, _age_seconds, _reclaim_stale_finalizing

        if _age_seconds(job.updated_at) < FINALIZING_STALE_SECONDS:
            return WaitExternal(
                checkpoint=checkpoint,
                wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
                external_handle=job.provider_task_id,
                acknowledges_cancel=True,  # a paid output is mid-copy; finish it
            )
        await _reclaim_stale_finalizing(job.id)
        job = await _owned_video_job(video_job_id, ctx.user_id)

    if not job.provider_task_id:
        return WaitUser(
            checkpoint=checkpoint,
            prompt=OPERATOR_REVIEW_PROMPT,
            input_schema={"type": "object"},
        )

    # Even under a cancel request, settle against provider FACTS (§7.4): a
    # task that already succeeded keeps its paid output; only a still-running
    # task gets a provider-side cancel.
    data = await vp._provider_status(target, job.provider_task_id)
    state = vp._provider_state(data)

    if state == "completed":
        return await _finalize(ctx, job.id, data, settings, cancel_race=cancel_requested)
    if state in ("failed", "cancelled"):
        return await _settle_provider_terminal(job.id, state, data)

    if cancel_requested:
        return await _cancel_video_job(ctx, job, target)

    await vp._update_job(job.id, status=state, error=None)
    await ctx.progress({"provider_state": state}, phase="provider_generate")
    return WaitExternal(
        checkpoint=checkpoint,
        wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
        external_handle=job.provider_task_id,
    )


async def _adopt_existing(ctx, job, settings):
    """The domain idempotency key already owns a video_job: converge on it."""
    if job.status == "completed":
        return await _success(job)
    if job.status in ("failed", "cancelled"):
        return Failed(
            error_code=f"existing_{job.status}",
            message=job.error or f"the segment's paid job already ended {job.status}",
        )
    if job.status == "submitting" and not job.provider_task_id:
        return WaitUser(
            checkpoint={"video_job_id": job.id},
            prompt=OPERATOR_REVIEW_PROMPT,
            input_schema={"type": "object"},
        )
    await ctx.progress({"adopted_video_job": job.id}, phase="provider_generate")
    return WaitExternal(
        checkpoint={"video_job_id": job.id},
        wake_at=_now(),
        external_handle=job.provider_task_id,
    )


async def _finalize(ctx, video_job_id: str, data: dict, settings, *, cancel_race: bool = False):
    from tool import video_production as vp

    await ctx.progress(phase="asset_publish")
    job = await _owned_video_job(video_job_id, ctx.user_id)
    refreshed = await vp._finalize_segment(job, data, _tool_ctx(ctx), settings)
    if refreshed is not None and refreshed.status == "completed":
        return await _success(refreshed, cancel_race=cancel_race)
    # transfer_failed keeps the paid provider output recoverable; retry only
    # the OSS leg on the next wake. Provider success outranks a cancel (§7.4),
    # so this wait survives a cancel request.
    return WaitExternal(
        checkpoint={"video_job_id": video_job_id},
        wake_at=_now() + timedelta(seconds=30),
        external_handle=job.provider_task_id if job else None,
        progress={"provider_state": "succeeded", "finalize": "transfer_failed"},
        acknowledges_cancel=True,
    )


async def _settle_provider_terminal(video_job_id: str, state: str, data: dict):
    from tool import video_production as vp
    from tool.video_workflow import mark_segment_job

    detail = data.get("error")
    message = detail.get("message") if isinstance(detail, dict) else str(detail or state)
    job = await _load_video_job(video_job_id)
    await vp._update_job(
        video_job_id, status=state, error=str(message)[:1000], completed_at=_now()
    )
    if job is not None:
        await vp._mark_asset(job.output_asset_id, status="failed")
        if job.segment_id:
            await mark_segment_job(job.segment_id, video_job_id, status=state)
    if state == "cancelled":
        return Cancelled(result={"video_job_id": video_job_id})
    return Failed(error_code="provider_failed", message=str(message)[:500])


async def _cancel_video_job(ctx, job, target):
    from tool import video_production as vp
    from tool.video_workflow import mark_segment_job

    if job.provider_task_id and job.status != "transfer_failed":
        try:
            await vp._provider_cancel(target, job.provider_task_id)
        except Exception as exc:
            log.warning(f"provider cancel for {job.id} failed: {exc}")
    await vp._update_job(job.id, status="cancelled", completed_at=_now(), error="cancelled")
    await vp._mark_asset(job.output_asset_id, status="failed")
    if job.segment_id:
        await mark_segment_job(job.segment_id, job.id, status="cancelled")
    return Cancelled(result={"video_job_id": job.id})


async def _success(job, *, cancel_race: bool = False):
    result = {
        "video_job_id": job.id,
        "production_id": job.production_id,
        "segment_id": job.segment_id,
        "asset_id": job.output_asset_id,
        "provider_task_id": job.provider_task_id,
    }
    if cancel_race:
        result["cancel_race"] = True
    artifacts = [job.output_asset_id] if job.output_asset_id else []
    return Succeeded(result=result, artifacts=artifacts)


async def _owned_video_job(video_job_id: str, user_id: str):
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return (
            await db.execute(
                select(VideoJob).where(VideoJob.id == video_job_id, VideoJob.user_id == user_id)
            )
        ).scalar_one_or_none()


async def _load_video_job(video_job_id: str):
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return await db.get(VideoJob, video_job_id)


register_builtin(SKILL_KEY, run, handler_version=2)
