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
    if operation == "segment.transcribe":
        return await _segment_transcribe(ctx, payload, checkpoint)
    if operation == "production.render":
        return await _production_render(ctx, payload, checkpoint)
    return Failed(
        error_code="unknown_operation",
        message=f"video-production has no operation {operation!r}",
    )


async def _sandbox_client(ctx):
    """The user's WUYING sandbox — the media queue's execution node. Acquires
    (cold-starts) the desktop when needed; media dispatch is idempotent per
    (owner, idempotency_key) so re-dispatch after a crash is safe."""
    from sandbox.manager import sandbox_manager

    if ctx.session_id:
        return await sandbox_manager.get_client(ctx.session_id, user_id=ctx.user_id)
    client = await sandbox_manager.get_client_any(user_id=ctx.user_id)
    if client is None:
        raise RuntimeError("no WUYING sandbox available for this user")
    return client


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
            input_schema={},
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
        if job is None:
            return Failed(error_code="video_job_missing", message=f"video job {video_job_id} vanished")

    if not job.provider_task_id:
        if cancel_requested:
            # Ambiguous submit + explicit user cancel: same policy as the
            # legacy tool — settle cancelled, keep the do-not-resubmit trail.
            await vp._update_job(
                job.id, status="cancelled", completed_at=_now(),
                error="cancelled during operator review (ambiguous submit; do not resubmit)",
            )
            await vp._mark_asset(job.output_asset_id, status="failed")
            if job.segment_id:
                from tool.video_workflow import mark_segment_job

                await mark_segment_job(job.segment_id, job.id, status="cancelled")
            return Cancelled(result={"video_job_id": job.id, "ambiguous_submit": True})
        return WaitUser(
            checkpoint=checkpoint,
            prompt=OPERATOR_REVIEW_PROMPT,
            input_schema={},
        )

    # Even under a cancel request, settle against provider FACTS (§7.4): a
    # task that already succeeded keeps its paid output; only a still-running
    # task gets a provider-side cancel.
    data = await vp._provider_status(target, job.provider_task_id)
    state = vp._provider_state(data)

    if state == "completed":
        return await _finalize(
            ctx, job.id, data, settings,
            cancel_race=cancel_requested,
            transfer_retries=int(checkpoint.get("transfer_retries") or 0),
        )
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
            input_schema={},
        )
    await ctx.progress({"adopted_video_job": job.id}, phase="provider_generate")
    return WaitExternal(
        checkpoint={"video_job_id": job.id},
        wake_at=_now(),
        external_handle=job.provider_task_id,
    )


#: Provider success outranks a cancel (§7.4) — but not forever: a permanently
#: broken OSS leg must not make the cancel button inert. After this many
#: transfer attempts under a pending cancel, the skill job honors the cancel
#: and the video_job stays `transfer_failed` for the recovery sweep to finish.
TRANSFER_RETRY_CANCEL_GRACE = 3


async def _finalize(ctx, video_job_id: str, data: dict, settings, *,
                    cancel_race: bool = False, transfer_retries: int = 0):
    from tool import video_production as vp

    await ctx.progress(phase="asset_publish")
    job = await _owned_video_job(video_job_id, ctx.user_id)
    if job is None:
        return Failed(error_code="video_job_missing", message=f"video job {video_job_id} vanished")
    refreshed = await vp._finalize_segment(job, data, _tool_ctx(ctx), settings)
    if refreshed is not None and refreshed.status == "completed":
        return await _success(refreshed, cancel_race=cancel_race)
    retries = transfer_retries + 1
    poll = _poll_seconds(settings)
    retry_delay = max(poll, 30.0) if poll else 0.0
    return WaitExternal(
        checkpoint={"video_job_id": video_job_id, "transfer_retries": retries},
        wake_at=_now() + timedelta(seconds=retry_delay),
        external_handle=job.provider_task_id,
        progress={"provider_state": "succeeded", "finalize": "transfer_failed",
                  "transfer_retries": retries},
        acknowledges_cancel=retries < TRANSFER_RETRY_CANCEL_GRACE,
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


# ---------------------------------------------------------------------------
# segment.transcribe — WUYING audio extraction + STT QA (PR#16)
# ---------------------------------------------------------------------------

async def _segment_transcribe(ctx, payload: dict, checkpoint: dict):
    from core.oss import get_oss
    from tool import video_production as vp
    from tool import video_workflow as vw

    target = vp._configured_transcription_target()
    _, video_settings = vp._configured_target()
    oss = get_oss()

    if checkpoint.get("video_job_id"):
        return await _advance_transcription(ctx, checkpoint, target, video_settings, oss)

    if await ctx.is_cancel_requested():
        return Cancelled()

    production_id = str(payload.get("production_id") or "")
    segment_id = str(payload.get("segment_id") or "")
    tctx = _tool_ctx(ctx)
    await ctx.progress(phase="preparing")
    approved = await vw.prepare_transcription(tctx, production_id, segment_id)
    domain_key = f"{production_id}:{segment_id}:stt"
    source = approved["asset"]
    request_data = {
        "production_id": production_id,
        "segment_id": segment_id,
        "source_asset_id": source.id,
        "source_bytes": source.size,
        "model": target.model,
        "skill_job_id": ctx.job_id,
    }
    request_hash = vw.content_hash(
        {"kind": "stt", "request_data": {k: v for k, v in request_data.items() if k != "skill_job_id"}}
    )
    job, audio, created = await vp._create_pending_job(
        ctx=tctx,
        kind="stt",
        idempotency_key=domain_key,
        model=target.model,
        prompt=None,
        request_data=request_data,
        filename=f"segment-{approved['segment'].ordinal}-speech.mp3",
        request_hash=request_hash,
        production_id=production_id,
        segment_id=segment_id,
        output_mime="audio/mpeg",
        transient=True,
    )
    checkpoint = {"video_job_id": job.id}
    if not created:
        if job.status == "completed":
            return _transcription_success(job)
        if job.status in ("failed", "cancelled"):
            return Failed(
                error_code=f"existing_{job.status}",
                message=job.error or f"the segment's STT job already ended {job.status}",
            )
        return await _advance_transcription(ctx, checkpoint, target, video_settings, oss)

    return await _dispatch_stt(ctx, job, checkpoint, video_settings, oss)


async def _dispatch_stt(ctx, job, checkpoint: dict, video_settings, oss):
    from tool import video_production as vp

    tctx = _tool_ctx(ctx)
    try:
        tctx.sandbox = await _sandbox_client(ctx)
        job, remote = await vp._dispatch_transcription(job, tctx, video_settings, oss)
    except Exception as exc:
        # Media dispatch is idempotent per (owner, idempotency_key) on the
        # node's queue, so unlike a paid provider submit this can retry.
        await vp._update_job(job.id, status="dispatch_unknown", error=str(exc)[:500])
        return WaitExternal(
            checkpoint=checkpoint,
            wake_at=_now() + timedelta(seconds=_poll_seconds(video_settings)),
            progress={"dispatch": "retrying"},
        )
    await ctx.progress(
        {"sandbox_job_id": remote.get("job_id"), "queue_position": remote.get("queue_position")},
        phase="extract_audio",
    )
    return WaitExternal(
        checkpoint=checkpoint,
        wake_at=_now() + timedelta(seconds=_poll_seconds(video_settings)),
        external_handle=remote.get("job_id"),
    )


async def _advance_transcription(ctx, checkpoint: dict, target, video_settings, oss):
    from tool import video_production as vp

    job = await _owned_video_job(checkpoint["video_job_id"], ctx.user_id, kind="stt")
    if job is None:
        return Failed(error_code="video_job_missing", message="STT job vanished")
    if job.status == "completed":
        return _transcription_success(job)
    if job.status in ("failed", "cancelled"):
        return Failed(
            error_code="stt_failed" if job.status == "failed" else "stt_cancelled",
            message=job.error or f"STT job ended {job.status}",
        )

    tctx = _tool_ctx(ctx)

    if job.status == "transcribing":
        # STT finalization runs inside one invocation; seeing it from outside
        # means a crash mid-finalize. Reclaim past the staleness window.
        from video.job_recovery import _age_seconds

        if _age_seconds(job.updated_at) < 300:
            return WaitExternal(
                checkpoint=checkpoint,
                wake_at=_now() + timedelta(seconds=_poll_seconds(video_settings)),
                acknowledges_cancel=True,
            )
        await vp._update_job(job.id, status="extraction_completed", error="recovering stale STT finalization")
        job = await _owned_video_job(job.id, ctx.user_id, kind="stt")
        if job is None:
            return Failed(error_code="video_job_missing", message="STT job vanished")

    if job.status == "extraction_completed":
        await ctx.progress(phase="speech_to_text")
        job = await vp._finalize_transcription(
            job, tctx, target, oss, (job.result_data or {}).get("extraction") or {}
        )
        return _map_transcription_finalize(job, checkpoint, video_settings)

    cancel_requested = await ctx.is_cancel_requested()
    client = await _sandbox_client(ctx)

    if cancel_requested:
        try:
            await client.cancel_media_job(job.sandbox_job_id or job.id, ctx.user_id)
        except Exception as exc:
            log.warning(f"media cancel for {job.id} failed: {exc}")
        await vp._update_job(job.id, status="cancelled", error="cancelled", completed_at=_now())
        await vp._mark_asset(job.output_asset_id, status="failed")
        return Cancelled(result={"video_job_id": job.id})

    if job.status in ("dispatch_unknown", "dispatching") and not job.sandbox_job_id:
        tctx.sandbox = client
        return await _dispatch_stt(ctx, job, checkpoint, video_settings, oss)

    remote = await client.get_media_job(job.sandbox_job_id or job.id, ctx.user_id)
    state = str(remote.get("status") or "failed")
    if state == "completed":
        await vp._update_job(
            job.id,
            status="extraction_completed",
            result_data={"extraction": remote.get("result") or {}},
            error=None,
        )
        job = await _owned_video_job(job.id, ctx.user_id, kind="stt")
        if job is None:
            return Failed(error_code="video_job_missing", message="STT job vanished")
        await ctx.progress(phase="speech_to_text")
        job = await vp._finalize_transcription(job, tctx, target, oss, remote.get("result") or {})
        return _map_transcription_finalize(job, checkpoint, video_settings)
    if state in ("failed", "cancelled"):
        await vp._mark_asset(job.output_asset_id, status="failed")
        await vp._update_job(
            job.id, status=state, error=str(remote.get("error") or state)[:1200], completed_at=_now()
        )
        if state == "cancelled":
            return Cancelled(result={"video_job_id": job.id})
        return Failed(error_code="extraction_failed", message=str(remote.get("error") or state)[:500])

    await vp._update_job(job.id, status=state, error=None)
    await ctx.progress(
        {"queue_position": remote.get("queue_position"), "sandbox_state": state},
        phase="extract_audio",
    )
    return WaitExternal(
        checkpoint=checkpoint,
        wake_at=_now() + timedelta(seconds=_poll_seconds(video_settings)),
        external_handle=job.sandbox_job_id,
    )


def _map_transcription_finalize(job, checkpoint: dict, video_settings):
    """`_finalize_transcription` returns the job unchanged (still
    `transcribing`) when it loses the finalize claim to a concurrent owner —
    that is a wait, not a failure."""
    if job is None:
        return Failed(error_code="video_job_missing", message="STT job vanished")
    if job.status == "completed":
        return _transcription_success(job)
    if job.status == "transcribing":
        return WaitExternal(
            checkpoint=checkpoint,
            wake_at=_now() + timedelta(seconds=_poll_seconds(video_settings)),
            acknowledges_cancel=True,
        )
    return Failed(error_code="stt_failed", message=job.error or "STT finalization failed")


def _transcription_success(job):
    result_data = job.result_data or {}
    transcript = result_data.get("transcript") or {}
    comparison = result_data.get("comparison") or {}
    return Succeeded(
        result={
            "video_job_id": job.id,
            "segment_id": job.segment_id,
            "transcript": transcript.get("text"),
            "similarity": comparison.get("similarity"),
            "verdict": comparison.get("verdict"),
        }
    )


# ---------------------------------------------------------------------------
# production.render — WUYING media queue render + OSS publish (PR#17)
# ---------------------------------------------------------------------------

async def _production_render(ctx, payload: dict, checkpoint: dict):
    from core.oss import get_oss
    from tool import video_production as vp
    from tool import video_workflow as vw

    _, settings = vp._configured_target()
    oss = get_oss()

    if checkpoint.get("video_job_id"):
        return await _advance_render(ctx, checkpoint, settings, oss)

    if await ctx.is_cancel_requested():
        return Cancelled()

    production_id = str(payload.get("production_id") or "")
    tctx = _tool_ctx(ctx)
    await ctx.progress(phase="preparing")
    approved = await vw.prepare_render_submission(tctx, production_id)
    domain_key = f"{approved['production_id']}:render:{approved['scope_hash'][:16]}"
    rows = await vp._resolve_inputs(approved["segment_assets"], tctx)
    request_data = {
        "production_id": approved["production_id"],
        "render_scope_hash": approved["scope_hash"],
        "segment_asset_ids": [row.id for row in rows],
        "captions": approved["captions"],
        "subtitles": approved["subtitles"],
        "channel_name": approved["channel_name"],
        "render_engine": str(payload.get("render_engine") or "auto"),
        "width": approved["width"],
        "height": approved["height"],
        "skill_job_id": ctx.job_id,
    }
    request_hash = vw.content_hash(
        {"kind": "render", "request_data": {k: v for k, v in request_data.items() if k != "skill_job_id"}}
    )
    job, asset, created = await vp._create_pending_job(
        ctx=tctx,
        kind="render",
        idempotency_key=domain_key,
        model="wuying-media@1",
        prompt=None,
        request_data=request_data,
        filename=str(payload.get("filename") or "") or None,
        request_hash=request_hash,
        production_id=approved["production_id"],
    )
    checkpoint = {"video_job_id": job.id}
    if not created:
        if job.status == "completed":
            return _render_success(job)
        if job.status in ("failed", "cancelled"):
            return Failed(
                error_code=f"existing_{job.status}",
                message=job.error or f"the production's render already ended {job.status}",
            )
        return await _advance_render(ctx, checkpoint, settings, oss)

    return await _dispatch_render_step(ctx, job, checkpoint, settings, oss)


async def _dispatch_render_step(ctx, job, checkpoint: dict, settings, oss):
    from tool import video_production as vp

    tctx = _tool_ctx(ctx)
    try:
        tctx.sandbox = await _sandbox_client(ctx)
        job, remote = await vp._dispatch_render(job, tctx, settings, oss)
    except Exception as exc:
        await vp._update_job(job.id, status="dispatch_unknown", error=str(exc)[:500])
        return WaitExternal(
            checkpoint=checkpoint,
            wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
            progress={"dispatch": "retrying"},
        )
    await ctx.progress(
        {"sandbox_job_id": remote.get("job_id"), "queue_position": remote.get("queue_position")},
        phase="rendering",
    )
    return WaitExternal(
        checkpoint=checkpoint,
        wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
        external_handle=remote.get("job_id"),
    )


async def _advance_render(ctx, checkpoint: dict, settings, oss):
    from tool import video_production as vp

    job = await _owned_video_job(checkpoint["video_job_id"], ctx.user_id, kind="render")
    if job is None:
        return Failed(error_code="video_job_missing", message="render job vanished")
    if job.status == "completed":
        return _render_success(job)
    if job.status in ("failed", "cancelled"):
        if job.status == "cancelled":
            return Cancelled(result={"video_job_id": job.id})
        return Failed(error_code="render_failed", message=job.error or "render failed")

    tctx = _tool_ctx(ctx)
    cancel_requested = await ctx.is_cancel_requested()
    client = await _sandbox_client(ctx)

    if job.status in ("dispatch_unknown", "dispatching") and not job.sandbox_job_id:
        if cancel_requested:
            await vp._update_job(job.id, status="cancelled", error="cancelled", completed_at=_now())
            await vp._mark_asset(job.output_asset_id, status="failed")
            return Cancelled(result={"video_job_id": job.id})
        return await _dispatch_render_step(ctx, job, checkpoint, settings, oss)

    if cancel_requested:
        try:
            remote = await client.cancel_media_job(job.sandbox_job_id or job.id, ctx.user_id)
        except Exception as exc:
            log.warning(f"render cancel for {job.id} failed: {exc}")
            remote = {"status": "cancelled", "error": "cancelled"}
        job = await vp._sync_render(job, remote, tctx, oss)
        if job.status == "completed":
            # The node finished before the cancel landed; keep the output.
            return _render_success(job, cancel_race=True)
        return Cancelled(result={"video_job_id": job.id})

    remote = await client.get_media_job(job.sandbox_job_id or job.id, ctx.user_id)
    await ctx.progress(
        {"queue_position": remote.get("queue_position"), "sandbox_state": remote.get("status")},
        phase="rendering",
    )
    job = await vp._sync_render(job, remote, tctx, oss)
    if job.status == "completed":
        return _render_success(job)
    if job.status == "failed":
        return Failed(error_code="render_failed", message=job.error or "render failed")
    if job.status == "cancelled":
        return Cancelled(result={"video_job_id": job.id})
    return WaitExternal(
        checkpoint=checkpoint,
        wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
        external_handle=job.sandbox_job_id,
    )


def _render_success(job, *, cancel_race: bool = False):
    result = {
        "video_job_id": job.id,
        "production_id": job.production_id,
        "asset_id": job.output_asset_id,
    }
    if cancel_race:
        result["cancel_race"] = True
    artifacts = [job.output_asset_id] if job.output_asset_id else []
    return Succeeded(result=result, artifacts=artifacts)


async def _owned_video_job(video_job_id: str, user_id: str, kind: str = "segment"):
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return (
            await db.execute(
                select(VideoJob).where(
                    VideoJob.id == video_job_id,
                    VideoJob.user_id == user_id,
                    VideoJob.kind == kind,
                )
            )
        ).scalar_one_or_none()


async def _load_video_job(video_job_id: str):
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return await db.get(VideoJob, video_job_id)


register_builtin(SKILL_KEY, run, handler_version=2)
