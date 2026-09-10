"""Compose an OpenBox timeline into a finished MP4 on Aliyun IMS.

The agent describes the cut in the schema from ``video/timeline.py`` — shots
it owns, captions, banners — and this tool does what only the backend can:
prove every asset belongs to the caller, compile the cut with the renderer's
quirks closed (``video/ims_compiler.py``), reserve a durable job so a retry
can never bill twice, submit, and turn the result into an owned asset the
chat can attach. Nothing here knows how to make a *good* cut; that stays in
the video-production skill.

Shape follows ``video_generate``: idempotency_key on submit, ``status``/``wait``
with bounded in-turn waits and ``polling_paused`` when a job outlives the turn,
``video/job_recovery.py`` re-driving whatever a crashed process left behind.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, model_validator

from core.log import create_logger
from tool.tool import ToolContext, ToolResult, define_tool
from video import ims_client
from video.ims_compiler import CompiledJob, TimelineCompileError, compile_ims
from video.timeline import Shot, Timeline

log = create_logger("tool.video_compose")

KIND = "compose"
_TERMINAL = {"completed", "failed", "cancelled"}
_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/webm", "video/x-m4v"}
_MAX_INLINE_WAITS = 13          # same in-turn budget as video_generate
_DEFERRED_RECHECK_SECONDS = 60


class VideoComposeArgs(BaseModel):
    action: Literal["schema", "validate", "submit", "status", "wait", "cancel"]
    #: An OpenBox timeline (action="schema" returns the exact format). Shots
    #: reference assets you own by asset_id; oss:// and OSS https URLs are
    #: accepted only when they point at your own objects.
    timeline: dict[str, Any] | None = None
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=180)
    #: Name for the finished file; sanitised, ``.mp4`` enforced.
    filename: str | None = Field(default=None, max_length=200)
    job_id: str | None = Field(default=None, max_length=96)
    wait_seconds: float = Field(default=25.0, ge=0.0, le=25.0)
    after_version: int = Field(default=0, ge=0)
    #: Increment on every wait call. On polling_paused=true end the run and
    #: resume this job_id in a later turn; never resubmit.
    wait_iteration: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _required_by_action(self) -> "VideoComposeArgs":
        if self.action == "schema":
            return self
        if self.action in ("validate", "submit"):
            if self.timeline is None:
                raise ValueError(f"{self.action} requires timeline")
            if self.action == "submit" and not self.idempotency_key:
                raise ValueError("submit requires idempotency_key to prevent duplicate billing")
            return self
        if not self.job_id:
            raise ValueError(f"{self.action} requires job_id")
        return self


# ── timeline → compiled job ──────────────────────────────────────────────────

class ComposeRefusal(Exception):
    """A reason the caller's own request cannot be composed; safe to show."""

    public_message = True


def _parse_timeline(raw: dict[str, Any]) -> Timeline:
    try:
        return Timeline.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'timeline'}: {err['msg']}" for err in exc.errors()[:8]
        )
        raise ComposeRefusal(f"timeline is not valid: {problems}") from exc


def _owned_oss_url(ref: str, ctx: ToolContext, oss) -> str | None:
    """Return the https form of an oss:// or https OSS reference if it is the
    caller's own object in the asset bucket; None if the reference is not an
    OSS URL at all (so the caller may be naming an asset id instead)."""
    if ref.startswith("oss://"):
        bucket, _, key = ref[6:].partition("/")
    else:
        parts = urlsplit(ref)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return None
        bucket = parts.netloc.split(".", 1)[0]
        key = parts.path.lstrip("/")
    if bucket != oss.bucket:
        raise ComposeRefusal(f"asset {ref!r} is outside the asset bucket; only your own uploaded or generated assets can be composed")
    if not key.startswith(f"assets/{ctx.user_id}/"):
        raise ComposeRefusal(f"asset {ref!r} is not owned by you")
    return f"https://{oss.host}/{key}"


async def _resolve_shot_assets(timeline: Timeline, ctx: ToolContext, oss) -> Timeline:
    from tool.video_production import _find_owned_asset

    shots: list[Shot] = []
    for index, shot in enumerate(timeline.shots):
        url = _owned_oss_url(shot.asset, ctx, oss)
        if url is None:
            row = await _find_owned_asset(shot.asset, ctx)
            if not row:
                raise ComposeRefusal(f"shots[{index}].asset {shot.asset!r} is not a ready asset owned by you")
            if row.mime not in _VIDEO_MIMES and not row.mime.startswith("image/"):
                raise ComposeRefusal(f"shots[{index}].asset is {row.mime}; shots must be video or image")
            url = f"https://{oss.host}/{row.oss_key}"
        shots.append(shot.model_copy(update={"asset": url}))
    return timeline.model_copy(update={"shots": shots})


def _compile(timeline: Timeline, *, output_url: str) -> CompiledJob:
    from core.config import get_config

    try:
        return compile_ims(timeline, output_url=output_url, bitrate_kbps=get_config().video_compose.bitrate_kbps)
    except TimelineCompileError as exc:
        raise ComposeRefusal("timeline cannot be compiled: " + "; ".join(exc.problems[:8])) from exc


# ── actions ──────────────────────────────────────────────────────────────────

async def _execute_schema() -> ToolResult:
    from video import ims_catalog as cat

    schema = Timeline.model_json_schema()
    rules = [
        "units: seconds and canvas fractions (0..1); never pixels",
        "shots[].asset: an asset_id you own (from video_generate or share_file), or your own oss:// / OSS https object",
        "shots[].fit: cover (default, crops to fill) | contain (black bars) | fill (stretch)",
        "transition_out sits on the shot it leads out of and overlaps the next shot",
        "texts[].align=center ignores x; y is the top edge of the text box",
        "captions render bottom-centred at caption_style.bottom_ratio, one track, non-overlapping",
        "output is 25fps regardless of source fps",
        f"verified transitions: {sorted(cat.VERIFIED_TRANSITIONS)}; motion-in: {sorted(cat.VERIFIED_MOTION_IN)}; "
        f"motion-out: {sorted(cat.VERIFIED_MOTION_OUT)}; fonts: {sorted(cat.VERIFIED_FONTS)}",
        "other documented values compile with a warning; unknown values are refused before submission",
        "use action=validate (free) before action=submit; it returns estimated_credits — show that number to the person and get a yes before submit",
        "billing: per output minute by tier (720p 0.03 / 1080p 0.06 credits), 不足 1 分钟按 1 分钟计, failed jobs are free",
    ]
    return ToolResult(
        title="OpenBox timeline schema",
        output="\n".join(["rules:"] + [f"- {r}" for r in rules] + ["", "json_schema=" + json.dumps(schema, ensure_ascii=False)]),
        metadata={"schema": schema},
    )


def _oss_or_refuse():
    """The asset bucket, or a refusal that names the missing setting."""
    from core.oss import OssNotConfigured, get_oss

    try:
        return get_oss()
    except OssNotConfigured as exc:
        raise ComposeRefusal(
            "video_compose needs the OSS asset bucket and Alibaba Cloud credentials: set OSS_BUCKET and "
            f"OSS_REGION (and ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET or an aliyun CLI profile) — {exc}"
        ) from exc


async def _execute_validate(args: VideoComposeArgs, ctx: ToolContext) -> ToolResult:
    try:
        oss = _oss_or_refuse()
        timeline = await _resolve_shot_assets(_parse_timeline(args.timeline or {}), ctx, oss)
        compiled = _compile(timeline, output_url=f"https://{oss.host}/assets/{ctx.user_id}/validate/preview.mp4")
    except Exception as exc:
        return ToolResult(title="Timeline rejected", output=_public(exc), metadata={"valid": False})
    from billing.media import quote_compose

    price = quote_compose(timeline.canvas.width, timeline.canvas.height, compiled.duration_sec)
    lines = ["valid=true", f"duration_sec={compiled.duration_sec}", f"shots={len(timeline.shots)}",
             f"captions={len(timeline.captions)}", f"texts={len(timeline.texts)}",
             f"output={timeline.canvas.width}x{timeline.canvas.height}", f"tier={price.tier}"]
    if price.credits is not None:
        lines += [f"minutes_billed={price.minutes_billed}", f"estimated_credits={_credits(price.credits)}",
                  "billing_note=不足 1 分钟按 1 分钟计；合成失败不计费；以成片实际时长结算"]
    else:
        lines.append("estimated_credits=unavailable (" + str(price.snapshot.get("reason")) + ")")
    lines += [f"warning={w}" for w in compiled.warnings]
    return ToolResult(title="Timeline valid", output="\n".join(lines),
                      metadata={"valid": True, "duration_sec": compiled.duration_sec, "warnings": compiled.warnings,
                                "estimated_credits": _credits(price.credits) if price.credits is not None else None,
                                "minutes_billed": price.minutes_billed, "tier": price.tier})


async def _execute_submit(args: VideoComposeArgs, ctx: ToolContext) -> ToolResult:
    from tool import video_production as vp

    try:
        oss = _oss_or_refuse()
        timeline = await _resolve_shot_assets(_parse_timeline(args.timeline or {}), ctx, oss)
        await _check_budget(ctx)
        from billing.media import precheck_compose

        await precheck_compose(ctx.session_id)
    except Exception as exc:
        return ToolResult(title="Composition refused", output=_public(exc))

    # The output key is only known once the asset row exists, and the compiled
    # request (hence its hash and token) includes that key. So reserve first
    # with a hash of the *resolved timeline*, which is what the person asked
    # for, then compile against the reserved key.
    resolved = timeline.model_dump(mode="json")
    request_hash = vp.content_hash(resolved)
    try:
        job, asset, created = await vp._create_pending_job(
            ctx=ctx, kind=KIND, idempotency_key=args.idempotency_key or "", model="ims",
            prompt=None, request_data={"timeline": resolved}, filename=args.filename or None,
            request_hash=request_hash,
        )
    except RuntimeError as exc:
        return ToolResult(title="Composition refused", output=str(exc))
    if not created:
        return await _status_result(job, asset, ctx, is_wait=False, args=args,
                                    title="Composition already submitted with this idempotency_key")

    try:
        compiled = _compile(timeline, output_url=f"https://{oss.host}/{asset.oss_key}")
    except Exception as exc:
        await vp._update_job(job.id, status="failed", error=_public(exc), completed_at=_now())
        await vp._mark_asset(asset.id, status="failed")
        return ToolResult(title="Composition refused", output=_public(exc))

    try:
        from autopilot import ledger as _autopilot
        from billing.media import quote_compose as _quote_compose

        _price = _quote_compose(timeline.canvas.width, timeline.canvas.height, compiled.duration_sec)
        _autopilot.guard_paid_step(ctx.session_id, kind="compose", credits=_price.credits if _price.credits is not None else 0,
                                   note=f"{compiled.duration_sec}s {_price.tier}")
    except Exception as exc:
        await vp._update_job(job.id, status="failed", error=_public(exc), completed_at=_now())
        await vp._mark_asset(asset.id, status="failed")
        return ToolResult(title="Composition refused", output=_public(exc), metadata={"job_id": job.id, "budget_stop": True})

    await vp._update_job(job.id, request_data={
        "timeline": resolved,
        "ims_timeline": compiled.timeline,
        "output_media_config": compiled.output_media_config,
        "client_token": compiled.client_token,
        "compile_warnings": compiled.warnings,
        "duration_sec": compiled.duration_sec,
    })
    try:
        provider_job_id = await ims_client.submit_media_producing_job(
            timeline=json.dumps(compiled.timeline, ensure_ascii=False, separators=(",", ":")),
            output_media_config=json.dumps(compiled.output_media_config, separators=(",", ":")),
            client_token=compiled.client_token,
            user_data=json.dumps({"openbox_job": job.id})[:512],
        )
    except Exception as exc:
        await vp._update_job(job.id, status="failed", error=_public(exc), completed_at=_now())
        await vp._mark_asset(asset.id, status="failed")
        return ToolResult(title="Composition submit failed", output=_public(exc), metadata={"job_id": job.id})
    await vp._update_job(job.id, status="in_progress", provider_task_id=provider_job_id, started_at=_now(), attempt=1)
    job = await vp._owned_job(job.id, ctx, KIND)
    return await _status_result(job, asset, ctx, is_wait=False, args=args, title="Composition submitted",
                                extra=[f"warning={w}" for w in compiled.warnings])


async def _execute_job_action(args: VideoComposeArgs, ctx: ToolContext) -> ToolResult:
    from tool import video_production as vp

    job = await vp._owned_job(args.job_id or "", ctx, KIND)
    if not job:
        return ToolResult(title="Composition job not found", output="No owned composition job has that job_id.")
    asset = await vp._job_asset(job)

    if args.action == "cancel":
        if job.status in _TERMINAL:
            return ToolResult(title="Composition already finished", output="\n".join(vp._job_lines(job, asset)),
                              metadata={"job_id": job.id, "status": job.status})
        # IMS has no cancel for producing jobs; the render may still complete
        # and land in OSS, but the job is closed and the asset stays hidden.
        await vp._update_job(job.id, status="cancelled", completed_at=_now(),
                             error="cancelled locally; IMS cannot cancel a running composition")
        await vp._mark_asset(job.output_asset_id, status="failed")
        job = await vp._owned_job(job.id, ctx, KIND)
        return ToolResult(title="Composition cancelled", output="\n".join(vp._job_lines(job)))

    is_wait = args.action == "wait"
    from core.config import get_config

    interval = float(get_config().video_compose.poll_interval_seconds)
    deadline = asyncio.get_running_loop().time() + (args.wait_seconds if is_wait else 0)
    version = vp._job_snapshot_version(job)
    while job.status not in _TERMINAL:
        if is_wait and args.after_version and version > args.after_version:
            break
        try:
            job = await poll_compose_job(job)
        except Exception as exc:
            return ToolResult(title="Composition status unavailable", output=_public(exc),
                              metadata={"job_id": job.id, "status": job.status})
        version = vp._job_snapshot_version(job)
        if job.status in _TERMINAL or not is_wait:
            break
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        await ctx.update_output(f"Composition is {job.status}; waiting for IMS…")
        await asyncio.sleep(min(interval, remaining))
    asset = await vp._job_asset(job)
    return await _status_result(job, asset, ctx, is_wait=is_wait, args=args)


async def poll_compose_job(job):
    """Ask IMS once and persist what changed. Shared with job recovery."""
    from core.oss import get_oss
    from tool import video_production as vp

    if not job.provider_task_id:
        return job
    state = await ims_client.get_media_producing_job(job.provider_task_id)
    if state.status == "completed":
        asset = await vp._job_asset(job)
        size = None
        if asset:
            try:
                head = await get_oss().head(asset.oss_key)
                size = int(head.get("size") or head.get("content_length") or 0) if head else None
            except Exception as exc:  # the object is there per IMS; size is a nicety
                log.info(f"compose {job.id}: head after IMS success failed: {type(exc).__name__}")
            await vp._mark_asset(asset.id, status="ready", size=size)
        planned = (job.request_data or {}).get("duration_sec")
        duration = state.duration_sec if state.duration_sec is not None else planned
        credits = None
        try:
            from billing.media import settle_compose
            canvas = ((job.request_data or {}).get("timeline") or {}).get("canvas") or {}
            credits = await settle_compose(job, asset, width=int(canvas.get("width") or 720),
                                           height=int(canvas.get("height") or 1280), duration_sec=duration)
        except Exception as exc:  # billing must never strand a finished video
            log.warning(f"compose {job.id}: settlement failed: {type(exc).__name__}: {exc}")
        await vp._update_job(job.id, status="completed", completed_at=_now(),
                             result_data={"media_url": state.media_url, "duration_sec": state.duration_sec,
                                          "ims_status": state.raw_status,
                                          "credits": _credits(credits) if credits is not None else None})
    elif state.status == "failed":
        await vp._update_job(job.id, status="failed", completed_at=_now(),
                             error=f"IMS {state.code or 'Failed'}: {(state.message or '')[:300]}",
                             result_data={"ims_status": state.raw_status})
        await vp._mark_asset(job.output_asset_id, status="failed")
    else:
        await vp._update_job(job.id, status="in_progress", result_data={"ims_status": state.raw_status})
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return await db.get(VideoJob, job.id)


async def _status_result(job, asset, ctx: ToolContext, *, is_wait: bool, args: VideoComposeArgs,
                         title: str | None = None, extra: list[str] | None = None) -> ToolResult:
    from tool import video_production as vp

    workspace_path = None
    attached = False
    if job.status == "completed":
        attached = await vp._attach_completed(job, ctx)
        job = await vp._owned_job(job.id, ctx, KIND) or job
        workspace_path = await vp._try_materialize(job, ctx)
        title = title or "Composition ready"
    else:
        title = title or "Composition status"
    from core.config import get_config

    interval = round(float(get_config().video_compose.poll_interval_seconds))
    lines = vp._job_lines(job, asset, retry_after=interval)
    duration = (job.request_data or {}).get("duration_sec")
    if duration is not None:
        lines.append(f"duration_sec={duration}")
    if workspace_path:
        lines.append(f"workspace_path={workspace_path}")
    if job.status == "completed":
        credits = (job.result_data or {}).get("credits")
        if credits is not None:
            lines.append(f"credits={credits}")
        lines.append("handoff_instruction=deliver with the attached final-video card or the exact download_url")
    lines.extend(extra or [])
    version = vp._job_snapshot_version(job)
    still_running = job.status not in _TERMINAL
    lines.append(f"version={version}")
    polling_paused = bool(is_wait and still_running and args.wait_iteration >= _MAX_INLINE_WAITS)
    if polling_paused:
        title = "Composition still processing"
        lines.extend([
            "still_running=true", "polling_paused=true",
            f"next_check_after_seconds={_DEFERRED_RECHECK_SECONDS}",
            "instruction=stop this assistant run now and report that the composition is still processing; "
            "do not call video_compose again in this run, do not cancel, do not resubmit; resume this job_id later",
        ])
    elif still_running:
        lines.extend(["still_running=true", f"next_wait_after_version={version} next_wait_iteration={args.wait_iteration + 1}"])
    metadata: dict[str, Any] = {
        "job_id": job.id, "status": job.status,
        "asset_id": asset.id if asset and asset.status == "ready" else None,
        "attached": attached, "still_running": still_running, "version": version,
        "retry_after_seconds": interval,
    }
    if polling_paused:
        metadata.update({"polling_paused": True, "next_check_after_seconds": _DEFERRED_RECHECK_SECONDS, "do_not_resubmit": True})
    return ToolResult(title=title, output="\n".join(lines), metadata=metadata)


# ── helpers ──────────────────────────────────────────────────────────────────

async def _check_budget(ctx: ToolContext) -> None:
    from sqlalchemy import func, select

    from core.config import get_config
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    limit = int(get_config().video_compose.daily_job_limit or 0)
    if limit <= 0:
        return
    from datetime import timedelta

    since = _now() - timedelta(days=1)
    async with get_db_session() as db:
        used = int((await db.execute(
            select(func.count()).select_from(VideoJob).where(
                VideoJob.user_id == ctx.user_id, VideoJob.kind == KIND, VideoJob.created_at >= since)
        )).scalar_one() or 0)
    if used >= limit:
        raise ComposeRefusal(f"daily composition limit reached ({used}/{limit} in the last 24h); tell the user rather than retrying")


def _credits(value) -> str:
    """Credits for humans: 0.03, not 0.030000000000."""
    from decimal import Decimal

    return format(Decimal(value).normalize(), "f")


def _public(exc: Exception) -> str:
    from billing.service import BillingError
    from tool.video_production import _public_error

    if isinstance(exc, BillingError):
        return f"{exc.code}: {exc}"
    return _public_error(exc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def execute_compose(args: VideoComposeArgs, ctx: ToolContext) -> ToolResult:
    if args.action == "schema":
        return await _execute_schema()
    if args.action == "validate":
        return await _execute_validate(args, ctx)
    if args.action == "submit":
        return await _execute_submit(args, ctx)
    return await _execute_job_action(args, ctx)


VIDEO_COMPOSE_DESCRIPTION = """\
Compose a finished MP4 from shots you already own — transitions, styled \
captions, animated banners — rendered in the cloud (Aliyun IMS), not with \
ffmpeg in the sandbox. Pass an OpenBox timeline (action="schema" gives the \
exact format and the verified effect names) plus an idempotency_key. Shots \
reference asset_ids from video_generate or share_file; assets never leave the \
account. Use action="validate" first: it is free and reports duration and \
warnings. Then action="submit", and "wait"/"status" on the job_id until \
completed; the result attaches to the chat and lands in the workspace. On \
polling_paused=true end the run and resume the same job_id later. Plain \
concatenation with burnt ASS subtitles can still be done with ffmpeg in the \
sandbox; use this when the cut needs effects."""

video_compose_tool = define_tool(
    "video_compose",
    description=VIDEO_COMPOSE_DESCRIPTION,
    parameters=VideoComposeArgs,
    execute=execute_compose,
    sandbox_required=False,
    parallel_safe=True,
)
