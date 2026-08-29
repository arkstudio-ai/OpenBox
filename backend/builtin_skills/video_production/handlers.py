"""Video production as a builtin skill (rebuild plan §11, PR#14/15).

The generic runtime owns admission, scheduling, waits, retries and recovery;
this package owns the domain: approvals, per-revision paid idempotency, the
provider protocol and OSS finalization. During the migration window the
handlers reuse the tool module's proven helpers (`tool.video_production`,
`tool.video_workflow`) — the platform core still imports neither.

segment.generate runs one segment through bounded, checkpointed steps:

  invocation 1 (no checkpoint): validate approvals → reserve the idempotent
    video_job row → consume the spend approval → pass the atomic external
    start gate → submit to the provider → persist the provider task id →
    WaitExternal.
  invocation 2+: one status pass — finalize on success (idempotent OSS copy +
    business-table commit), settle provider failure, otherwise WaitExternal
    with the provider's own pace. An ambiguous submit (no provider task id)
    goes to operator review and is never auto-resubmitted (§10.1).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.log import create_logger
from skill_runtime.registry import register_builtin, register_startup_validator
from skill_runtime.repository import StaleLeaseError
from skill_runtime.types import (
    Cancelled,
    Failed,
    Retry,
    Succeeded,
    WaitExternal,
    WaitUser,
)

log = create_logger("builtin.video_production")

SKILL_KEY = "builtin:video-production"

OPERATOR_REVIEW_PROMPT = (
    "视频提交结果不明确（没有拿到供应商任务 ID），已转交平台管理员核实供应商侧"
    "是否产生付费任务。管理员若查到任务，将通过受限通道填写 provider_task_id；若"
    "确认没有产生任务，将设置 confirmed_no_remote_task=true。核实前平台不会重复提交。"
)

OPERATOR_REVIEW_SCHEMA = {
    "type": "object",
    # Non-standard presentation hint enforced again by the REST endpoint. A
    # provider task handle is a shared-account capability, never user input.
    "x-operator-only": True,
    "properties": {
        "provider_task_id": {
            "type": "string",
            "minLength": 1,
            "description": "人工查到的供应商任务 ID",
        },
        "confirmed_no_remote_task": {
            "type": "boolean",
            "const": True,
            "description": "仅在供应商侧确认没有产生任务时设为 true",
        },
    },
    "oneOf": [
        {"required": ["provider_task_id"]},
        {"required": ["confirmed_no_remote_task"]},
    ],
    "additionalProperties": False,
}

STT_OPERATOR_REVIEW_PROMPT = (
    "语音转写调用在远端结果落库前中断，平台无法证明供应商是否已经创建或完成任务。"
    "为避免重复提交，请平台管理员先核实供应商侧；仅在确认没有远端任务后，"
    "通过受限通道设置 confirmed_no_remote_task=true，平台才会重新转写。"
)

STT_OPERATOR_REVIEW_SCHEMA = {
    "type": "object",
    "x-operator-only": True,
    "properties": {
        "confirmed_no_remote_task": {
            "type": "boolean",
            "const": True,
            "description": "仅在供应商侧确认没有产生转写任务时设为 true",
        },
    },
    "required": ["confirmed_no_remote_task"],
    "additionalProperties": False,
}


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


def validate_runtime_dependencies(config) -> None:
    """Domain-owned fail-fast check, invoked through the generic registry."""
    if not config.skill_jobs_video_write:
        return
    if config.sandbox_provider == "wuying" and not config.wuying_api_key:
        raise RuntimeError("video skill runtime requires WUYING_API_KEY")
    from core.oss import get_oss
    from tool.video_production import _configured_target, _configured_transcription_target

    get_oss()
    _configured_target(None)
    _configured_transcription_target()


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

    if ctx.cancel_requested:
        linked = await _linked_video_job(
            ctx,
            kind="segment",
            production_id=str(payload.get("production_id") or ""),
            segment_id=str(payload.get("segment_id") or ""),
        )
        if linked is not None:
            return await _advance_existing(ctx, {"video_job_id": linked.id})
        return Cancelled()

    production_id = str(payload.get("production_id") or "")
    segment_id = str(payload.get("segment_id") or "")
    tctx = _tool_ctx(ctx)

    from tool import video_workflow as vw

    await ctx.progress(phase="preparing")
    approved = await vw.prepare_segment_submission(tctx, production_id, segment_id)
    # Route from the model frozen onto the segment, not the deployment default:
    # the durable path is the only write path once the rollout gate is on, so
    # resolving `None` here silently billed every pick against the default
    # model and made the composer's video picker decorative.
    target, settings = vp._configured_target(approved.get("model"))
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

    # The gateway channels (sd2/task) speak a different wire shape than ark and
    # take their references as presigned URLs rather than provider asset ids.
    # Building the ark payload unconditionally would post a body the gateway
    # ignores — and it bills for what it substitutes instead of erroring.
    submit_path: str | None = None
    if getattr(target, "channel", "ark") == "ark":
        provider_payload: dict[str, Any] = {
            "model": target.model,
            "content": [{"type": "text", "text": prompt}, *provider_content],
            "resolution": resolution,
            "ratio": ratio,
            "duration": duration,
            "generate_audio": generate_audio,
            "watermark": watermark,
        }
    else:
        from core.oss import get_oss
        from tool import video_providers

        oss = get_oss()
        refs = [
            {
                "kind": "image" if row.mime.startswith(vp._IMAGE_PREFIX) else "video",
                "url": oss.presign_get(
                    row.oss_key, expires_sec=settings.provider_input_url_ttl_seconds
                ),
                "role": (
                    "reference_image"
                    if row.mime.startswith(vp._IMAGE_PREFIX)
                    else "reference_video"
                ),
            }
            for row in inputs
        ]
        submit_path, provider_payload = video_providers.build_payload(
            target,
            prompt=prompt,
            refs=refs,
            resolution=resolution,
            ratio=ratio,
            duration=duration,
            generate_audio=generate_audio,
            watermark=watermark,
        )

    # The row-lock gate orders cancellation against the *start* of the paid
    # side effect. If cancel committed first, no approval is consumed and no
    # provider request is made.
    if not await ctx.may_start_external():
        await vp._update_job(
            job.id,
            status="cancelled",
            error="cancelled before provider submission",
            completed_at=_now(),
        )
        await vp._mark_asset(job.output_asset_id, status="failed")
        await vw.mark_segment_job(
            approved["segment_id"],
            job.id,
            user_id=ctx.user_id,
            status="cancelled",
        )
        return Cancelled(result={"video_job_id": job.id})

    try:
        await vw.consume_spend_approval(approved["spend_approval_id"])
    except Exception as exc:
        await ctx.assert_lease()
        await vp._update_job(
            job.id,
            status="failed",
            error="approved generation call limit is unavailable",
            completed_at=_now(),
        )
        await vp._mark_asset(job.output_asset_id, status="failed")
        return Failed(
            error_code="spend_approval_unavailable",
            message=vp._public_error(exc),
        )

    await vw.mark_segment_job(
        approved["segment_id"],
        job.id,
        user_id=ctx.user_id,
        status="submitting",
    )
    try:
        if submit_path is None:
            submitted = await vp._provider_submit(target, provider_payload)
        else:
            from tool import video_providers as _vpx

            submitted = await _vpx.submit(target, submit_path, provider_payload)
            if getattr(target, "channel", "ark") == "task":
                submitted = {
                    **submitted,
                    **(submitted.get("data") if isinstance(submitted.get("data"), dict) else {}),
                }
    except Exception as exc:
        # Unknown outcome: the POST may or may not have landed. Never resubmit
        # blindly — park for operator review (§10.1 submit_unknown).
        log.warning(
            f"video submit outcome unknown for job {job.id}: {type(exc).__name__}"
        )
        return WaitUser(
            checkpoint={"video_job_id": job.id},
            prompt=OPERATOR_REVIEW_PROMPT,
            input_schema=OPERATOR_REVIEW_SCHEMA,
        )

    # The provider call can outlive this invocation's lease. Fence the domain
    # identity write; a replacement worker must reconcile an ambiguous submit
    # instead of having a stale worker overwrite its decision.
    await ctx.assert_lease()
    from tool import video_providers as _vps

    state = vp._provider_state(submitted, target)
    # Never `submitted["id"]` directly: on sd2 the upstream overwrites
    # `task_id`, and polling that value returns task_not_exist.
    provider_task_id = _vps.extract_task_id(target, submitted)
    stored_state = "in_progress" if state == "completed" else state
    await vp._update_job(
        job.id,
        provider_task_id=provider_task_id,
        status=stored_state,
        attempt=1,
        started_at=_now(),
        error=None,
    )

    await ctx.progress(
        {"provider_state": state, "provider_task_id": provider_task_id},
        phase="provider_generate",
    )

    if state == "completed":
        return await _finalize(ctx, job.id, submitted, settings, route=target)
    if state in ("failed", "cancelled"):
        return await _settle_provider_terminal(ctx, job.id, state, submitted)
    return WaitExternal(
        checkpoint={"video_job_id": job.id},
        wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
        external_handle=provider_task_id,
        progress={"provider_state": state, "provider_task_id": provider_task_id},
    )


async def _advance_existing(ctx, checkpoint: dict):
    from tool import video_production as vp

    video_job_id = checkpoint["video_job_id"]
    job = await _owned_video_job(video_job_id, ctx.user_id)
    if job is None:
        return Failed(error_code="video_job_missing", message=f"video job {video_job_id} vanished")
    # Poll on the channel that was actually submitted to. `job.model` is the
    # durable record of that; re-resolving the default would poll the wrong
    # endpoint for anything not on the default model.
    target, settings = vp._configured_target(job.model or None)

    if job.status == "completed":
        return await _success(ctx, job)
    if job.status == "cancelled":
        return Cancelled(result={"video_job_id": job.id})
    if job.status == "failed":
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
        await ctx.assert_lease()
        await _reclaim_stale_finalizing(job.id)
        job = await _owned_video_job(video_job_id, ctx.user_id)
        if job is None:
            return Failed(error_code="video_job_missing", message=f"video job {video_job_id} vanished")

    if not job.provider_task_id:
        operator_inputs = [item for item in ctx.inputs if item.kind == "operator_resume"]
        if operator_inputs:
            decision = operator_inputs[-1].payload or {}
            provider_task_id = str(decision.get("provider_task_id") or "").strip()
            confirmed_absent = decision.get("confirmed_no_remote_task") is True
            if provider_task_id:
                await ctx.assert_lease()
                await vp._update_job(
                    job.id,
                    provider_task_id=provider_task_id,
                    status="in_progress",
                    error=None,
                )
                job = await _owned_video_job(video_job_id, ctx.user_id)
                if job is None:
                    return Failed(
                        error_code="video_job_missing",
                        message=f"video job {video_job_id} vanished",
                    )
                # The latest valid answer supersedes older attempts. Acknowledge
                # only after its provider identity is durably applied.
                ctx.consume_inputs(operator_inputs)
            elif confirmed_absent:
                await ctx.assert_lease()
                terminal = "cancelled" if cancel_requested else "failed"
                message = "operator confirmed provider created no remote task"
                await vp._update_job(
                    job.id,
                    status=terminal,
                    completed_at=_now(),
                    error=message,
                )
                await vp._mark_asset(job.output_asset_id, status="failed")
                if job.segment_id:
                    from tool.video_workflow import mark_segment_job

                    await mark_segment_job(
                        job.segment_id,
                        job.id,
                        user_id=ctx.user_id,
                        status=terminal,
                    )
                ctx.consume_inputs(operator_inputs)
                if cancel_requested:
                    return Cancelled(
                        result={"video_job_id": job.id, "operator_confirmed_absent": True}
                    )
                return Failed(error_code="provider_submit_not_created", message=message)
            else:
                return WaitUser(
                    checkpoint=checkpoint,
                    prompt=(
                        "人工核实输入无效：必须填写 provider_task_id，或在确认供应商侧"
                        "没有任务后设置 confirmed_no_remote_task=true。"
                    ),
                    input_schema=OPERATOR_REVIEW_SCHEMA,
                    acknowledges_cancel=cancel_requested,
                )

    if not job.provider_task_id:
        if cancel_requested:
            # There is no remote identity to cancel or query. Marking this
            # cancelled would be a lie: the paid POST may have landed. Keep the
            # durable operator-review state and the do-not-resubmit evidence.
            return WaitUser(
                checkpoint=checkpoint,
                prompt=(
                    "取消请求已收到，但提交结果不明确且没有供应商任务 ID，"
                    "平台无法证明远端任务已停止。请人工核实供应商侧；在确认"
                    "未产生任务前不要重新提交。"
                ),
                input_schema=OPERATOR_REVIEW_SCHEMA,
                acknowledges_cancel=True,
            )
        return WaitUser(
            checkpoint=checkpoint,
            prompt=OPERATOR_REVIEW_PROMPT,
            input_schema=OPERATOR_REVIEW_SCHEMA,
        )

    # Even under a cancel request, settle against provider FACTS (§7.4): a
    # task that already succeeded keeps its paid output; only a still-running
    # task gets a provider-side cancel.
    try:
        data = await vp._provider_status(target, job.provider_task_id)
    except Exception as exc:
        response = getattr(exc, "response", None)
        if (
            cancel_requested
            and checkpoint.get("cancel_pending")
            and getattr(response, "status_code", None) == 404
        ):
            # A previously accepted DELETE followed by not-found is the
            # provider's only terminal cancellation fact for this API.
            return await _settle_provider_terminal(
                ctx,
                job.id,
                "cancelled",
                {"error": {"message": "provider task removed after cancellation"}},
            )
        raise
    await ctx.assert_lease()
    state = vp._provider_state(data, target)

    if state == "completed":
        return await _finalize(
            ctx, job.id, data, settings,
            route=target,
            cancel_race=cancel_requested,
            transfer_retries=int(checkpoint.get("transfer_retries") or 0),
        )
    if state in ("failed", "cancelled"):
        return await _settle_provider_terminal(ctx, job.id, state, data)

    if cancel_requested:
        return await _cancel_video_job(ctx, job, target, settings)

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
        return await _success(ctx, job)
    if job.status in ("failed", "cancelled"):
        return Failed(
            error_code=f"existing_{job.status}",
            message=job.error or f"the segment's paid job already ended {job.status}",
        )
    if job.status == "submitting" and not job.provider_task_id:
        return WaitUser(
            checkpoint={"video_job_id": job.id},
            prompt=OPERATOR_REVIEW_PROMPT,
            input_schema=OPERATOR_REVIEW_SCHEMA,
        )
    await ctx.progress({"adopted_video_job": job.id}, phase="provider_generate")
    return WaitExternal(
        checkpoint={"video_job_id": job.id},
        wake_at=_now(),
        external_handle=job.provider_task_id,
    )


#: Provider success outranks a cancel (§7.4), so a produced asset must still be
#: reconciled. A persistently broken publish leg may acknowledge only this many
#: cancel-time retries; afterwards the generic runtime parks the job for
#: operator reconciliation instead of falsely claiming the remote work stopped.
TRANSFER_RETRY_CANCEL_GRACE = 3


async def _finalize(ctx, video_job_id: str, data: dict, settings, *,
                    route=None, cancel_race: bool = False, transfer_retries: int = 0):
    from tool import video_production as vp

    await ctx.progress(phase="asset_publish")
    job = await _owned_video_job(video_job_id, ctx.user_id)
    if job is None:
        return Failed(error_code="video_job_missing", message=f"video job {video_job_id} vanished")
    await ctx.assert_lease()
    # The route decides where the finished video URL lives: sd2 carries it in
    # `metadata.url`, ark under `content`. Omitting it here finalizes a paid,
    # completed task to "no video URL".
    refreshed = await vp._finalize_segment(
        job,
        data,
        _tool_ctx(ctx),
        settings,
        route,
        persist_guard=ctx.assert_lease,
    )
    if refreshed is not None and refreshed.status == "completed":
        return await _success(ctx, refreshed, cancel_race=cancel_race)
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


async def _settle_provider_terminal(ctx, video_job_id: str, state: str, data: dict):
    from tool import video_production as vp
    from tool.video_workflow import mark_segment_job

    detail = data.get("error")
    provider_code = detail.get("code") if isinstance(detail, dict) else None
    message = f"provider reported {state}"
    if isinstance(provider_code, str) and provider_code:
        message += f" ({provider_code[:80]})"
    job = await _owned_video_job(video_job_id, ctx.user_id)
    if job is None:
        # A checkpoint is durable input, not an ownership capability. Never
        # pass its raw id to the legacy unscoped helper after an ownership
        # lookup failed, or a corrupt/stale checkpoint could mutate another
        # tenant's VideoJob row.
        return Failed(
            error_code="video_job_missing",
            message=f"video job {video_job_id} vanished",
        )
    # The provider read may outlive a lease. Fence domain-table writes too, not
    # only the final SkillJob settlement, so a stale worker cannot overwrite a
    # newer reconciliation result.
    await ctx.assert_lease()
    await vp._update_job(
        job.id, status=state, error=str(message)[:1000], completed_at=_now()
    )
    await vp._mark_asset(job.output_asset_id, status="failed")
    if job.segment_id:
        await mark_segment_job(
            job.segment_id,
            job.id,
            user_id=ctx.user_id,
            status=state,
        )
    if state == "cancelled":
        return Cancelled(result={"video_job_id": job.id})
    return Failed(error_code="provider_failed", message=str(message)[:500])


async def _cancel_video_job(ctx, job, target, settings):
    from tool import video_production as vp

    if job.provider_task_id and job.status != "transfer_failed":
        try:
            await ctx.assert_lease()
            await vp._provider_cancel(target, job.provider_task_id)
        except StaleLeaseError:
            raise
        except Exception as exc:
            log.warning(
                f"provider cancel for {job.id} failed: {type(exc).__name__}"
            )
            # A transport error (and BossIP's lack of a cancel endpoint) says
            # nothing about the provider task's terminal state. Keep polling
            # under desired_state=cancel; never manufacture a local cancelled
            # fact while a paid task may still be running or succeed.
            return WaitExternal(
                checkpoint={"video_job_id": job.id, "cancel_pending": True},
                wake_at=_now() + timedelta(seconds=30),
                external_handle=job.provider_task_id,
                progress={"provider_state": job.status, "cancel": "pending"},
                acknowledges_cancel=True,
            )
    # DELETE/Cancel normally means "request accepted", not "the remote task is
    # terminal". Keep the local domain row non-terminal until the next status
    # read proves cancelled, failed, or completed (success wins the race).
    return WaitExternal(
        checkpoint={"video_job_id": job.id, "cancel_pending": True},
        wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
        external_handle=job.provider_task_id,
        progress={"provider_state": job.status, "cancel": "requested"},
        acknowledges_cancel=True,
    )


async def _success(ctx, job, *, cancel_race: bool = False):
    """Repair and verify domain postconditions before generic success.

    FileAsset, VideoJob and VideoSegment are separate legacy transactions. A
    crash after marking VideoJob completed but before updating the segment must
    not let the next invocation skip that final domain commit.
    """
    await ctx.assert_lease()
    await _require_ready_output(job, ctx.user_id)
    if job.segment_id:
        from tool.video_workflow import mark_segment_job

        await mark_segment_job(
            job.segment_id,
            job.id,
            user_id=ctx.user_id,
            status="completed",
            output_asset_id=job.output_asset_id,
        )
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
    # Read the settings directly rather than resolving a generation route for
    # them: transcription does not generate, and routing can legitimately fail
    # (an undeclared default model) for reasons that must not break QA.
    from core.config import get_config

    video_settings = get_config().video_generation
    oss = get_oss()

    if checkpoint.get("video_job_id"):
        return await _advance_transcription(ctx, checkpoint, target, video_settings, oss)

    if ctx.cancel_requested:
        linked = await _linked_video_job(
            ctx,
            kind="stt",
            production_id=str(payload.get("production_id") or ""),
            segment_id=str(payload.get("segment_id") or ""),
        )
        if linked is not None:
            return await _advance_transcription(
                ctx,
                {"video_job_id": linked.id},
                target,
                video_settings,
                oss,
            )
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

    return await _dispatch_stt(
        ctx, job, checkpoint, video_settings, oss, known_unsubmitted=True
    )


async def _dispatch_stt(
    ctx,
    job,
    checkpoint: dict,
    video_settings,
    oss,
    *,
    known_unsubmitted: bool = False,
):
    from tool import video_production as vp

    tctx = _tool_ctx(ctx)
    try:
        if not await ctx.may_start_external():
            if known_unsubmitted:
                await vp._mark_asset(job.output_asset_id, status="failed")
                await vp._update_job(
                    job.id,
                    status="cancelled",
                    error="cancelled before sandbox transcription startup",
                    completed_at=_now(),
                )
                return Cancelled(result={"video_job_id": job.id})
            return WaitExternal(
                checkpoint=checkpoint,
                wake_at=_now(),
                progress={"dispatch": "cancel_won_before_sandbox_start"},
            )
        tctx.sandbox = await _sandbox_client(ctx)
        if not await ctx.may_start_external():
            if known_unsubmitted:
                await vp._mark_asset(job.output_asset_id, status="failed")
                await vp._update_job(
                    job.id,
                    status="cancelled",
                    error="cancelled before sandbox transcription dispatch",
                    completed_at=_now(),
                )
                return Cancelled(result={"video_job_id": job.id})
            return WaitExternal(
                checkpoint=checkpoint,
                wake_at=_now(),
                progress={"dispatch": "cancel_won_before_external_start"},
            )
        job, remote = await vp._dispatch_transcription(
            job,
            tctx,
            video_settings,
            oss,
            persist_guard=ctx.assert_lease,
        )
    except StaleLeaseError:
        raise
    except Exception as exc:
        # Media dispatch is idempotent per (owner, idempotency_key) on the
        # node's queue, so unlike a paid provider submit this can retry.
        await ctx.assert_lease()
        await vp._update_job(job.id, status="dispatch_unknown", error=vp._public_error(exc))
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
    if job.status == "cancelled":
        return Cancelled(result={"video_job_id": job.id})
    if job.status == "failed":
        return Failed(
            error_code="stt_failed",
            message=job.error or f"STT job ended {job.status}",
        )

    tctx = _tool_ctx(ctx)
    cancel_requested = await ctx.is_cancel_requested()

    if job.status == "transcript_ready":
        # The provider output is already durable. Resume only the local
        # comparison/domain commit; cancellation cannot erase that success or
        # turn it into another provider submission.
        await ctx.progress(phase="speech_to_text")
        job = await vp._finalize_transcription(
            job,
            tctx,
            target,
            oss,
            (job.result_data or {}).get("extraction") or {},
            persist_guard=ctx.assert_lease,
            durable_recovery=True,
        )
        return _map_transcription_finalize(job, checkpoint, video_settings)

    if job.status == "transcribing":
        # The current STT adapter submits and polls inside one invocation and
        # does not durably expose a provider task id. Observing `transcribing`
        # therefore means the prior process died in an ambiguous external-call
        # window. Time passing does not prove absence: never auto-resubmit.
        operator_inputs = [item for item in ctx.inputs if item.kind == "operator_resume"]
        if not operator_inputs:
            return WaitUser(
                checkpoint=checkpoint,
                prompt=STT_OPERATOR_REVIEW_PROMPT,
                input_schema=STT_OPERATOR_REVIEW_SCHEMA,
                acknowledges_cancel=cancel_requested,
            )

        decision = operator_inputs[-1].payload or {}
        if decision.get("confirmed_no_remote_task") is not True:
            return WaitUser(
                checkpoint=checkpoint,
                prompt=(
                    "人工核实输入无效：只有在确认供应商侧没有产生转写任务后，"
                    "才能设置 confirmed_no_remote_task=true。"
                ),
                input_schema=STT_OPERATOR_REVIEW_SCHEMA,
                acknowledges_cancel=cancel_requested,
            )

        await ctx.assert_lease()
        if cancel_requested:
            await vp._mark_asset(job.output_asset_id, status="failed")
            await vp._update_job(
                job.id,
                status="cancelled",
                error="operator confirmed no remote transcription task was created",
                completed_at=_now(),
            )
            ctx.consume_inputs(operator_inputs)
            return Cancelled(
                result={"video_job_id": job.id, "operator_confirmed_absent": True}
            )

        # An administrator has established the missing external fact. Return to
        # the durable pre-submit checkpoint; the retry below is now explicit and
        # auditable rather than time-based guesswork.
        await vp._update_job(
            job.id,
            status="extraction_completed",
            error="operator confirmed no remote transcription task was created",
        )
        ctx.consume_inputs(operator_inputs)
        job = await _owned_video_job(job.id, ctx.user_id, kind="stt")
        if job is None:
            return Failed(error_code="video_job_missing", message="STT job vanished")

    if job.status == "extraction_completed":
        if cancel_requested:
            await ctx.assert_lease()
            await vp._mark_asset(job.output_asset_id, status="failed")
            await vp._update_job(
                job.id,
                status="cancelled",
                error="cancelled after audio extraction and before transcription",
                completed_at=_now(),
            )
            return Cancelled(result={"video_job_id": job.id})
        await ctx.progress(phase="speech_to_text")
        if not await ctx.may_start_external():
            await vp._mark_asset(job.output_asset_id, status="failed")
            await vp._update_job(
                job.id,
                status="cancelled",
                error="cancelled before transcription provider submission",
                completed_at=_now(),
            )
            return Cancelled(result={"video_job_id": job.id})
        job = await vp._finalize_transcription(
            job,
            tctx,
            target,
            oss,
            (job.result_data or {}).get("extraction") or {},
            persist_guard=ctx.assert_lease,
            durable_recovery=True,
        )
        return _map_transcription_finalize(job, checkpoint, video_settings)

    client = await _sandbox_client(ctx)

    if cancel_requested:
        try:
            await ctx.assert_lease()
            remote = await client.cancel_media_job(job.sandbox_job_id or job.id, ctx.user_id)
        except StaleLeaseError:
            raise
        except Exception as exc:
            log.warning(
                f"media cancel for {job.id} failed: {type(exc).__name__}"
            )
            return WaitExternal(
                checkpoint=checkpoint,
                wake_at=_now() + timedelta(seconds=_poll_seconds(video_settings)),
                external_handle=job.sandbox_job_id or job.id,
                progress={"sandbox_state": job.status, "cancel": "pending"},
                acknowledges_cancel=True,
            )
        await ctx.assert_lease()
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
            await ctx.assert_lease()
            await vp._mark_asset(job.output_asset_id, status="failed")
            await vp._update_job(
                job.id,
                status="cancelled",
                error="cancelled after audio extraction and before transcription",
                completed_at=_now(),
            )
            return Cancelled(result={"video_job_id": job.id})
        if state in ("failed", "cancelled"):
            await vp._mark_asset(job.output_asset_id, status="failed")
            await vp._update_job(
                job.id,
                status=state,
                error=str(remote.get("error") or state)[:1200],
                completed_at=_now(),
            )
            if state == "cancelled":
                return Cancelled(result={"video_job_id": job.id})
            return Failed(
                error_code="extraction_failed",
                message=str(remote.get("error") or state)[:500],
            )
        await vp._update_job(job.id, status=state, error=None)
        return WaitExternal(
            checkpoint=checkpoint,
            wake_at=_now() + timedelta(seconds=_poll_seconds(video_settings)),
            external_handle=job.sandbox_job_id or job.id,
            progress={"sandbox_state": state, "cancel": "pending"},
            acknowledges_cancel=True,
        )

    if job.status in ("dispatch_unknown", "dispatching") and not job.sandbox_job_id:
        tctx.sandbox = client
        return await _dispatch_stt(ctx, job, checkpoint, video_settings, oss)

    remote = await client.get_media_job(job.sandbox_job_id or job.id, ctx.user_id)
    await ctx.assert_lease()
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
        if not await ctx.may_start_external():
            await vp._mark_asset(job.output_asset_id, status="failed")
            await vp._update_job(
                job.id,
                status="cancelled",
                error="cancelled before transcription provider submission",
                completed_at=_now(),
            )
            return Cancelled(result={"video_job_id": job.id})
        job = await vp._finalize_transcription(
            job,
            tctx,
            target,
            oss,
            remote.get("result") or {},
            persist_guard=ctx.assert_lease,
            durable_recovery=True,
        )
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
    """Map a durable STT checkpoint without repeating provider work.

    ``transcribing`` means the external outcome is still ambiguous and gets one
    short wake before the next invocation enters operator review.
    ``transcript_ready`` means only the idempotent local domain commit remains;
    that is an ordinary retryable local fault, not external waiting.
    """
    if job is None:
        return Failed(error_code="video_job_missing", message="STT job vanished")
    if job.status == "completed":
        return _transcription_success(job)
    if job.status == "transcript_ready":
        return Retry(
            checkpoint=checkpoint,
            error_code="stt_domain_commit_pending",
            error_message=job.error or "transcript is durable; local QA commit is pending",
            retry_at=_now() + timedelta(seconds=_poll_seconds(video_settings)),
        )
    if job.status == "transcribing":
        return WaitExternal(
            checkpoint=checkpoint,
            wake_at=_now() + timedelta(seconds=_poll_seconds(video_settings)),
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

    # Same as transcription: render needs the settings, not a generation route.
    from core.config import get_config

    settings = get_config().video_generation
    oss = get_oss()

    if checkpoint.get("video_job_id"):
        return await _advance_render(ctx, checkpoint, settings, oss)

    if ctx.cancel_requested:
        linked = await _linked_video_job(
            ctx,
            kind="render",
            production_id=str(payload.get("production_id") or ""),
        )
        if linked is not None:
            return await _advance_render(
                ctx,
                {"video_job_id": linked.id},
                settings,
                oss,
            )
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
            return await _render_success(ctx, job)
        if job.status in ("failed", "cancelled"):
            return Failed(
                error_code=f"existing_{job.status}",
                message=job.error or f"the production's render already ended {job.status}",
            )
        return await _advance_render(ctx, checkpoint, settings, oss)

    return await _dispatch_render_step(
        ctx, job, checkpoint, settings, oss, known_unsubmitted=True
    )


async def _dispatch_render_step(
    ctx,
    job,
    checkpoint: dict,
    settings,
    oss,
    *,
    known_unsubmitted: bool = False,
):
    from tool import video_production as vp

    tctx = _tool_ctx(ctx)
    try:
        if not await ctx.may_start_external():
            if known_unsubmitted:
                await vp._mark_asset(job.output_asset_id, status="failed")
                await vp._update_job(
                    job.id,
                    status="cancelled",
                    error="cancelled before sandbox render startup",
                    completed_at=_now(),
                )
                return Cancelled(result={"video_job_id": job.id})
            return WaitExternal(
                checkpoint=checkpoint,
                wake_at=_now(),
                progress={"dispatch": "cancel_won_before_sandbox_start"},
            )
        tctx.sandbox = await _sandbox_client(ctx)
        if not await ctx.may_start_external():
            if known_unsubmitted:
                await vp._mark_asset(job.output_asset_id, status="failed")
                await vp._update_job(
                    job.id,
                    status="cancelled",
                    error="cancelled before sandbox render dispatch",
                    completed_at=_now(),
                )
                return Cancelled(result={"video_job_id": job.id})
            return WaitExternal(
                checkpoint=checkpoint,
                wake_at=_now(),
                progress={"dispatch": "cancel_won_before_external_start"},
            )
        job, remote = await vp._dispatch_render(
            job,
            tctx,
            settings,
            oss,
            persist_guard=ctx.assert_lease,
        )
    except StaleLeaseError:
        raise
    except Exception as exc:
        await ctx.assert_lease()
        await vp._update_job(job.id, status="dispatch_unknown", error=vp._public_error(exc))
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
        return await _render_success(ctx, job)
    if job.status in ("failed", "cancelled"):
        if job.status == "cancelled":
            return Cancelled(result={"video_job_id": job.id})
        return Failed(error_code="render_failed", message=job.error or "render failed")

    tctx = _tool_ctx(ctx)
    cancel_requested = await ctx.is_cancel_requested()
    client = await _sandbox_client(ctx)

    if (
        job.status in ("dispatch_unknown", "dispatching")
        and not job.sandbox_job_id
        and not cancel_requested
    ):
        return await _dispatch_render_step(ctx, job, checkpoint, settings, oss)

    if cancel_requested:
        try:
            await ctx.assert_lease()
            remote = await client.cancel_media_job(job.sandbox_job_id or job.id, ctx.user_id)
        except StaleLeaseError:
            raise
        except Exception as exc:
            log.warning(
                f"render cancel for {job.id} failed: {type(exc).__name__}"
            )
            return WaitExternal(
                checkpoint=checkpoint,
                wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
                external_handle=job.sandbox_job_id or job.id,
                progress={"sandbox_state": job.status, "cancel": "pending"},
                acknowledges_cancel=True,
            )
        await ctx.assert_lease()
        job = await vp._sync_render(
            job, remote, tctx, oss, persist_guard=ctx.assert_lease
        )
        if job.status == "completed":
            # The node finished before the cancel landed; keep the output.
            return await _render_success(ctx, job, cancel_race=True)
        if job.status == "cancelled":
            return Cancelled(result={"video_job_id": job.id})
        if job.status == "failed":
            return Failed(error_code="render_failed", message=job.error or "render failed")
        return WaitExternal(
            checkpoint=checkpoint,
            wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
            external_handle=job.sandbox_job_id or job.id,
            progress={"sandbox_state": job.status, "cancel": "pending"},
            acknowledges_cancel=True,
        )

    remote = await client.get_media_job(job.sandbox_job_id or job.id, ctx.user_id)
    await ctx.progress(
        {"queue_position": remote.get("queue_position"), "sandbox_state": remote.get("status")},
        phase="rendering",
    )
    await ctx.assert_lease()
    job = await vp._sync_render(
        job, remote, tctx, oss, persist_guard=ctx.assert_lease
    )
    if job.status == "completed":
        return await _render_success(ctx, job)
    if job.status == "failed":
        return Failed(error_code="render_failed", message=job.error or "render failed")
    if job.status == "cancelled":
        return Cancelled(result={"video_job_id": job.id})
    return WaitExternal(
        checkpoint=checkpoint,
        wake_at=_now() + timedelta(seconds=_poll_seconds(settings)),
        external_handle=job.sandbox_job_id,
    )


async def _render_success(ctx, job, *, cancel_race: bool = False):
    """Make the legacy Production commit a recoverable success postcondition."""
    await ctx.assert_lease()
    await _require_ready_output(job, ctx.user_id)
    if job.production_id and job.output_asset_id:
        from tool.video_workflow import mark_render_complete

        await mark_render_complete(
            job.production_id,
            job.output_asset_id,
            user_id=ctx.user_id,
        )
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


async def _require_ready_output(job, user_id: str) -> None:
    """Fail closed until the declared output is a ready, owned FileAsset."""
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    async with get_db_session() as db:
        asset_id = (
            await db.execute(
                select(FileAsset.id).where(
                    FileAsset.id == job.output_asset_id,
                    FileAsset.user_id == user_id,
                    FileAsset.status == "ready",
                    FileAsset.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
    if asset_id is None:
        raise RuntimeError("completed video job output asset is not ready and owned")


async def _linked_video_job(
    ctx,
    *,
    kind: str,
    production_id: str,
    segment_id: str | None = None,
):
    """Recover the domain row if a process died before checkpoint settlement.

    The domain row is written before every external dispatch and carries the
    generic job id. Looking it up lets a cancel invocation with an empty
    checkpoint adopt and unwind the already-created remote work.
    """
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        stmt = select(VideoJob).where(
            VideoJob.user_id == ctx.user_id,
            VideoJob.kind == kind,
            VideoJob.production_id == production_id,
        )
        if segment_id is not None:
            stmt = stmt.where(VideoJob.segment_id == segment_id)
        rows = (
            await db.execute(stmt.order_by(VideoJob.created_at.desc()))
        ).scalars().all()
    for row in rows:
        if str((row.request_data or {}).get("skill_job_id") or "") == ctx.job_id:
            return row
    return None


# v2 only changed the runtime contract around this handler; its persisted
# checkpoint shape remains compatible with jobs admitted by the v1 rollout.
register_startup_validator(SKILL_KEY, validate_runtime_dependencies)
register_builtin(SKILL_KEY, run, handler_version=2, compatible_versions=(1,))
