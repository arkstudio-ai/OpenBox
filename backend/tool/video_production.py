"""Skill-only Seedance generation and WUYING media-render orchestration.

Provider calls and ownership checks live on the backend.  FFmpeg and
HyperFrames live behind the sandbox action server's durable per-desktop queue.
The two tools in this module are registered globally but hidden until the
``video-production`` skill explicitly activates them for an agent run.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.log import create_logger
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.video_production")

_SEGMENT_TERMINAL = {"completed", "failed", "cancelled"}
_RENDER_TERMINAL = {"completed", "failed", "cancelled"}
_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/webm", "video/x-m4v"}
_IMAGE_PREFIX = "image/"
_RATIOS = {"16:9", "9:16", "3:4", "1:1", "4:3", "21:9", "adaptive"}
_RESOLUTIONS = {"480p", "720p", "1080p"}


class VideoGenerateArgs(BaseModel):
    action: Literal["submit", "status", "wait", "cancel"]
    job_id: str | None = Field(default=None, max_length=96)
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=180)
    prompt: str | None = Field(default=None, min_length=1, max_length=32_000)
    input_assets: list[str] = Field(default_factory=list, max_length=8)
    model: str | None = Field(default=None, max_length=160)
    resolution: Literal["480p", "720p", "1080p"] | None = None
    ratio: Literal["16:9", "9:16", "3:4", "1:1", "4:3", "21:9", "adaptive"] | None = None
    duration: int | None = Field(default=None, ge=-1, le=30)
    generate_audio: bool | None = None
    watermark: bool | None = None
    seed: int | None = Field(default=None, ge=0, le=2_147_483_647)
    filename: str | None = Field(default=None, max_length=180)
    wait_seconds: float = Field(default=25.0, ge=0.0, le=25.0)

    @model_validator(mode="after")
    def _required_by_action(self):
        if self.action == "submit":
            if not self.prompt:
                raise ValueError("submit requires prompt")
            if not self.idempotency_key:
                raise ValueError("submit requires idempotency_key to prevent duplicate billing")
        elif not self.job_id:
            raise ValueError(f"{self.action} requires job_id")
        return self


class VideoRenderArgs(BaseModel):
    action: Literal["submit", "status", "wait", "cancel", "retry"]
    job_id: str | None = Field(default=None, max_length=96)
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=180)
    segment_assets: list[str] = Field(default_factory=list, max_length=100)
    captions: list[Annotated[str, StringConstraints(max_length=2000)]] = Field(
        default_factory=list, max_length=100
    )
    subtitles: bool = True
    channel_name: str = Field(default="", max_length=100)
    render_engine: Literal["auto", "ffmpeg", "hyperframes"] = "auto"
    width: int = Field(default=720, ge=320, le=3840)
    height: int = Field(default=1280, ge=320, le=3840)
    filename: str | None = Field(default=None, max_length=180)
    wait_seconds: float = Field(default=25.0, ge=0.0, le=25.0)
    after_version: int = Field(default=0, ge=0)
    wait_iteration: int = Field(
        default=0,
        ge=0,
        description=(
            "Increment on repeated wait calls when the returned version is unchanged; "
            "it prevents an intentional long poll from being mistaken for a tool loop."
        ),
    )

    @model_validator(mode="after")
    def _required_by_action(self):
        if self.action == "submit":
            if not self.idempotency_key:
                raise ValueError("submit requires idempotency_key")
            if not self.segment_assets:
                raise ValueError("submit requires at least one segment asset")
            if self.captions and len(self.captions) != len(self.segment_assets):
                raise ValueError("captions must be empty or match segment_assets length")
        elif not self.job_id:
            raise ValueError(f"{self.action} requires job_id")
        return self


@dataclass(frozen=True)
class VideoProviderTarget:
    provider: str
    model: str
    api_key: str
    base_url: str
    submit_timeout_seconds: int
    status_timeout_seconds: int


def _configured_target(model_override: str | None = None) -> tuple[VideoProviderTarget, object]:
    import os

    from core.config import get_config

    config = get_config()
    settings = config.video_generation
    provider = config.provider.get(settings.provider)
    api_key = (provider.api_key if provider else None) or (
        os.environ.get("DOUBAO_API_KEY", "") if settings.provider == "doubao" else ""
    )
    configured_base = (provider.base_url if provider else None) or (
        os.environ.get("DOUBAO_BASE_URL", "") if settings.provider == "doubao" else ""
    )
    if not api_key:
        raise RuntimeError("DOUBAO_API_KEY is empty")
    base_url = configured_base.rstrip("/")
    if not base_url.startswith("https://") or base_url.endswith(".html"):
        raise RuntimeError(
            "DOUBAO_BASE_URL must be the API origin (for example https://api.tokenspace.net.cn), not the documentation page"
        )
    model = (model_override or settings.model).strip()
    if not model:
        raise RuntimeError("video_generation.model is empty")
    return (
        VideoProviderTarget(
            provider=settings.provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            submit_timeout_seconds=settings.submit_timeout_seconds,
            status_timeout_seconds=settings.status_timeout_seconds,
        ),
        settings,
    )


def _auth_header(key: str) -> str:
    return key if key.lower().startswith("bearer ") else f"Bearer {key}"


def _safe_filename(requested: str | None, job_id: str, *, rendered: bool = False) -> str:
    fallback = f"{'final' if rendered else 'segment'}-{job_id}.mp4"
    raw = PurePosixPath(requested or fallback).name
    stem = re.sub(r"\.(mp4|mov|webm|m4v)$", "", raw, flags=re.IGNORECASE)
    stem = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", stem).strip("._") or fallback[:-4]
    return f"{stem[:180]}.mp4"


async def _find_owned_asset(ref: str, ctx: ToolContext, *, ready: bool = True):
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    value = ref.strip()
    if value.startswith("asset:"):
        value = value[6:]
    conditions = [FileAsset.user_id == ctx.user_id, FileAsset.is_deleted.is_(False)]
    if ready:
        conditions.append(FileAsset.status == "ready")
    async with get_db_session() as db:
        row = (
            await db.execute(select(FileAsset).where(FileAsset.id == value, *conditions))
        ).scalar_one_or_none()
        if row:
            return row
        name = PurePosixPath(value).name
        if not name:
            return None
        if ctx.session_id:
            row = (
                await db.execute(
                    select(FileAsset)
                    .where(FileAsset.name == name, FileAsset.session_id == ctx.session_id, *conditions)
                    .order_by(FileAsset.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row:
                return row
        return (
            await db.execute(
                select(FileAsset)
                .where(FileAsset.name == name, *conditions)
                .order_by(FileAsset.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _resolve_inputs(refs: list[str], ctx: ToolContext) -> list[Any]:
    rows = []
    for ref in refs:
        row = await _find_owned_asset(ref, ctx)
        if not row:
            raise RuntimeError(f"asset '{ref}' is not a ready OSS resource owned by this user")
        if not (row.mime.startswith(_IMAGE_PREFIX) or row.mime in _VIDEO_MIMES):
            raise RuntimeError(f"asset '{ref}' is {row.mime}; video generation inputs must be image or video")
        rows.append(row)
    return rows


def _validate_generation(model: str, resolution: str, duration: int, generate_audio: bool) -> None:
    if resolution not in _RESOLUTIONS:
        raise RuntimeError(f"unsupported resolution: {resolution}")
    lowered = model.lower()
    if resolution == "1080p" and model != "doubao-seedance-2-0-260128":
        raise RuntimeError("1080p is supported only by doubao-seedance-2-0-260128")
    if "2-5" in lowered:
        if duration == -1 or not 4 <= duration <= 30:
            raise RuntimeError("Seedance 2.5 duration must be 4-30 seconds")
    elif duration != -1 and not 4 <= duration <= 15:
        raise RuntimeError("Seedance 2.0 duration must be -1 or 4-15 seconds")
    if "fast" in lowered and generate_audio:
        raise RuntimeError("Seedance Fast does not support generated audio; use the standard model for spoken video")


async def _create_pending_job(
    *,
    ctx: ToolContext,
    kind: str,
    idempotency_key: str,
    model: str | None,
    prompt: str | None,
    request_data: dict[str, Any],
    filename: str | None,
) -> tuple[Any, Any, bool]:
    """Reserve job+asset before a paid/remote call; return existing on a race."""
    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob
    from sandbox.assets import _session_project

    async with get_db_session() as db:
        existing = (
            await db.execute(
                select(VideoJob).where(
                    VideoJob.user_id == ctx.user_id,
                    VideoJob.kind == kind,
                    VideoJob.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing:
            asset = await db.get(FileAsset, existing.output_asset_id) if existing.output_asset_id else None
            return existing, asset, False

    job_id = ascending("video")
    asset_id = ascending("asset")
    name = _safe_filename(filename, job_id, rendered=kind == "render")
    key = f"assets/{ctx.user_id}/{asset_id}/{name}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        project_id = ctx.project_id or await _session_project(db, ctx.session_id, ctx.user_id)
        asset = FileAsset(
            id=asset_id,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            project_id=project_id or None,
            name=name,
            oss_key=key,
            mime="video/mp4",
            size=0,
            status="pending",
            source="agent",
            transient=False,
            created_at=now,
        )
        job = VideoJob(
            id=job_id,
            user_id=ctx.user_id,
            session_id=ctx.session_id or None,
            project_id=project_id or None,
            kind=kind,
            idempotency_key=idempotency_key,
            status="submitting" if kind == "segment" else "dispatching",
            model=model,
            prompt=prompt,
            request_data=request_data,
            result_data={},
            output_asset_id=asset_id,
            attempt=0,
            created_at=now,
            updated_at=now,
        )
        db.add(asset)
        db.add(job)
        try:
            await db.commit()
            return job, asset, True
        except IntegrityError:
            await db.rollback()

    async with get_db_session() as db:
        existing = (
            await db.execute(
                select(VideoJob).where(
                    VideoJob.user_id == ctx.user_id,
                    VideoJob.kind == kind,
                    VideoJob.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one()
        asset = await db.get(FileAsset, existing.output_asset_id) if existing.output_asset_id else None
        return existing, asset, False


async def _owned_job(job_id: str, ctx: ToolContext, kind: str):
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return (
            await db.execute(
                select(VideoJob).where(
                    VideoJob.id == job_id,
                    VideoJob.user_id == ctx.user_id,
                    VideoJob.kind == kind,
                )
            )
        ).scalar_one_or_none()


async def _update_job(job_id: str, **values) -> None:
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    values["updated_at"] = datetime.now(timezone.utc)
    async with get_db_session() as db:
        await db.execute(update(VideoJob).where(VideoJob.id == job_id).values(**values))


async def _mark_asset(asset_id: str | None, *, status: str, size: int | None = None) -> None:
    if not asset_id:
        return
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    values: dict[str, Any] = {"status": status}
    if size is not None:
        values["size"] = size
    async with get_db_session() as db:
        await db.execute(update(FileAsset).where(FileAsset.id == asset_id).values(**values))


async def _attach_completed(job, ctx: ToolContext) -> bool:
    if not job.output_asset_id or not ctx.message_id or job.attached_message_id:
        return bool(job.attached_message_id)
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob
    from models.message import FilePart
    from session.session import save_part

    claimed = False
    async with get_db_session() as db:
        result = await db.execute(
            update(VideoJob)
            .where(VideoJob.id == job.id, VideoJob.attached_message_id.is_(None))
            .values(attached_message_id=ctx.message_id, updated_at=datetime.now(timezone.utc))
        )
        claimed = result.rowcount == 1
        asset = await db.get(FileAsset, job.output_asset_id)
    if not claimed:
        return True
    if not asset:
        await _update_job(job.id, attached_message_id=None)
        return False
    try:
        await save_part(
            FilePart(
                path=f"/workspace/generated_videos/{asset.name}",
                mime_type=asset.mime,
                asset_id=asset.id,
                oss_key=asset.oss_key,
                size=asset.size,
                transient=False,
                session_id=ctx.session_id,
                message_id=ctx.message_id,
            ),
            is_new=True,
            user_id=ctx.user_id,
        )
        return True
    except Exception:
        await _update_job(job.id, attached_message_id=None)
        log.warning("video asset saved but chat attachment failed", exc_info=True)
        return False


def _public_error(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            detail = response.text[:800]
        except Exception:
            detail = ""
        return f"HTTP {response.status_code}: {detail or response.reason_phrase}"[:1000]
    return (str(exc) or exc.__class__.__name__)[:1000]


async def _provider_submit(target: VideoProviderTarget, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=target.submit_timeout_seconds, follow_redirects=True) as client:
        response = await client.post(
            f"{target.base_url}/api/v3/contents/generations/tasks",
            headers={"Authorization": _auth_header(target.api_key), "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code not in (200, 201, 202):
        response.raise_for_status()
    data = response.json()
    if not data.get("id"):
        raise RuntimeError("video provider response did not include a task id")
    return data


async def _provider_status(target: VideoProviderTarget, task_id: str) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=target.status_timeout_seconds, follow_redirects=True) as client:
        response = await client.get(
            f"{target.base_url}/api/v3/contents/generations/tasks/{task_id}",
            headers={"Authorization": _auth_header(target.api_key)},
        )
    response.raise_for_status()
    return response.json()


async def _provider_cancel(target: VideoProviderTarget, task_id: str) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=target.status_timeout_seconds) as client:
        response = await client.delete(
            f"{target.base_url}/api/v3/contents/generations/tasks/{task_id}",
            headers={"Authorization": _auth_header(target.api_key)},
        )
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


def _provider_state(data: dict[str, Any]) -> str:
    value = str(data.get("status") or "").lower()
    return {
        "queued": "queued",
        "pending": "queued",
        "running": "in_progress",
        "in_progress": "in_progress",
        "succeeded": "completed",
        "success": "completed",
        "completed": "completed",
        "failed": "failed",
        "failure": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }.get(value, "in_progress")


def _provider_video_url(data: dict[str, Any]) -> str:
    content = data.get("content") or {}
    if not isinstance(content, dict):
        return ""
    return str(content.get("video_url") or content.get("url") or "")


async def _copy_provider_video_to_oss(url: str, oss, key: str, max_bytes: int) -> int:
    import httpx

    put_url = oss.presign_put(key, "video/mp4", expires_sec=3600)
    total = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, read=1800.0), follow_redirects=True) as source:
        async with source.stream("GET", url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise RuntimeError("provider video exceeds configured output size limit")

            async def chunks():
                nonlocal total
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError("provider video exceeds configured output size limit")
                    yield chunk

            headers = {"Content-Type": "video/mp4"}
            if declared:
                headers["Content-Length"] = str(declared)
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, write=1800.0)) as sink:
                uploaded = await sink.put(put_url, content=chunks(), headers=headers)
            if uploaded.status_code not in (200, 201, 204):
                raise RuntimeError(f"OSS upload returned HTTP {uploaded.status_code}")
    if total <= 0:
        raise RuntimeError("provider returned an empty video")
    head = await oss.head(key)
    if not head:
        raise RuntimeError("video is missing from OSS after upload")
    return head["size"] or total


async def _finalize_segment(job, data: dict[str, Any], ctx: ToolContext, settings) -> Any:
    source_url = _provider_video_url(data)
    if not source_url.startswith("https://"):
        raise RuntimeError("provider marked the task completed without a video URL")
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob

    claimed = False
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        result = await db.execute(
            update(VideoJob)
            .where(
                VideoJob.id == job.id,
                VideoJob.status.in_(["queued", "in_progress", "transfer_failed"]),
            )
            .values(
                status="finalizing",
                result_data={"provider_status": data.get("status")},
                updated_at=now,
            )
        )
        claimed = result.rowcount == 1
        asset = await db.get(FileAsset, job.output_asset_id)
    if not claimed:
        return await _owned_job(job.id, ctx, "segment")
    if not asset:
        raise RuntimeError("reserved output asset is missing")
    try:
        size = await _copy_provider_video_to_oss(
            source_url, get_oss(), asset.oss_key, settings.max_provider_output_bytes
        )
    except Exception as exc:
        # The paid provider task already succeeded. Keep this recoverable so a
        # later wait can fetch a fresh result URL and retry only OSS transfer.
        await _update_job(
            job.id,
            status="transfer_failed",
            error=_public_error(exc),
            result_data={"provider_status": data.get("status")},
        )
        await _mark_asset(job.output_asset_id, status="pending")
        return await _owned_job(job.id, ctx, "segment")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    await _mark_asset(job.output_asset_id, status="ready", size=size)
    await _update_job(
        job.id,
        status="completed",
        result_data={"usage": usage, "provider_status": data.get("status"), "bytes": size},
        error=None,
        completed_at=datetime.now(timezone.utc),
    )
    return await _owned_job(job.id, ctx, "segment")


def _job_lines(job, asset=None, *, queue_position: int | None = None, retry_after: int = 5) -> list[str]:
    lines = [f"job_id={job.id}", f"status={job.status}"]
    if job.provider_task_id:
        lines.append(f"provider_task_id={job.provider_task_id}")
    if job.sandbox_job_id:
        lines.append(f"sandbox_job_id={job.sandbox_job_id}")
    if queue_position is not None:
        lines.append(f"queue_position={queue_position}")
    if job.status not in _SEGMENT_TERMINAL | _RENDER_TERMINAL:
        lines.append(f"retry_after_seconds={retry_after}")
    if asset and asset.status == "ready":
        lines.extend(
            [
                f"asset_id={asset.id}",
                f"name={asset.name}",
                f"path=/workspace/generated_videos/{asset.name}",
                f"bytes={asset.size}",
            ]
        )
    if job.error:
        lines.append(f"error={job.error}")
    return lines


async def _job_asset(job):
    if not job or not job.output_asset_id:
        return None
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    async with get_db_session() as db:
        return await db.get(FileAsset, job.output_asset_id)


async def execute_generate(args: VideoGenerateArgs, ctx: ToolContext) -> ToolResult:
    try:
        target, settings = _configured_target(args.model)
        from core.oss import get_oss

        oss = get_oss()
    except Exception as exc:
        return ToolResult(title="Video generation is not configured", output=_public_error(exc))

    if args.action == "submit":
        resolution = args.resolution or settings.default_resolution
        ratio = args.ratio or settings.default_ratio
        duration = args.duration if args.duration is not None else settings.default_duration
        generate_audio = (
            args.generate_audio if args.generate_audio is not None else settings.default_generate_audio
        )
        watermark = args.watermark if args.watermark is not None else settings.default_watermark
        try:
            _validate_generation(target.model, resolution, duration, generate_audio)
            if ratio not in _RATIOS:
                raise RuntimeError(f"unsupported ratio: {ratio}")
            inputs = await _resolve_inputs(args.input_assets, ctx)
            request_data = {
                "input_asset_ids": [row.id for row in inputs],
                "resolution": resolution,
                "ratio": ratio,
                "duration": duration,
                "generate_audio": generate_audio,
                "watermark": watermark,
                "seed": args.seed,
            }
            job, asset, created = await _create_pending_job(
                ctx=ctx,
                kind="segment",
                idempotency_key=args.idempotency_key or "",
                model=target.model,
                prompt=args.prompt,
                request_data=request_data,
                filename=args.filename,
            )
            if not created:
                if job.status == "completed":
                    await _attach_completed(job, ctx)
                return ToolResult(
                    title="Existing video generation job",
                    output="\n".join(_job_lines(job, asset)),
                    metadata={"job_id": job.id, "status": job.status, "idempotent_reuse": True},
                )

            content: list[dict[str, Any]] = [{"type": "text", "text": args.prompt}]
            for row in inputs:
                url = oss.presign_get(row.oss_key, expires_sec=settings.provider_input_url_ttl_seconds)
                if row.mime.startswith(_IMAGE_PREFIX):
                    content.append(
                        {"type": "image_url", "image_url": {"url": url}, "role": "reference_image"}
                    )
                else:
                    content.append(
                        {"type": "video_url", "video_url": {"url": url}, "role": "reference_video"}
                    )
            payload: dict[str, Any] = {
                "model": target.model,
                "content": content,
                "resolution": resolution,
                "ratio": ratio,
                "duration": duration,
                "generate_audio": generate_audio,
                "watermark": watermark,
            }
            if args.seed is not None:
                payload["seed"] = args.seed
            await ctx.update_output("Submitting the asynchronous Seedance video task…")
            response = await _provider_submit(target, payload)
            state = _provider_state(response)
            # A provider may return a terminal state from the initial POST.  In
            # our state machine, "completed" means the output is already safe
            # in OSS, so retain an in-progress state until finalization wins
            # the database claim and completes that transfer.
            stored_state = "in_progress" if state == "completed" else state
            await _update_job(
                job.id,
                provider_task_id=response["id"],
                status=stored_state,
                attempt=1,
                started_at=datetime.now(timezone.utc),
                error=None,
            )
            job = await _owned_job(job.id, ctx, "segment")
            if state == "completed":
                await ctx.update_output("Provider completed; copying the video to OSS…")
                job = await _finalize_segment(job, response, ctx, settings)
                asset = await _job_asset(job)
                await _attach_completed(job, ctx)
            elif state in {"failed", "cancelled"}:
                detail = response.get("error")
                message = detail.get("message") if isinstance(detail, dict) else str(detail or state)
                await _update_job(
                    job.id,
                    error=message[:1000],
                    completed_at=datetime.now(timezone.utc),
                )
                await _mark_asset(job.output_asset_id, status="failed")
                job = await _owned_job(job.id, ctx, "segment")
            return ToolResult(
                title=("Video segment ready" if job.status == "completed" else "Video generation submitted"),
                output="\n".join(_job_lines(job, asset)),
                metadata={
                    "job_id": job.id,
                    "status": job.status,
                    "asset_id": asset.id if asset and asset.status == "ready" else None,
                    "retry_after_seconds": 5,
                },
            )
        except Exception as exc:
            if "job" in locals() and created:
                provider_task_id = (
                    str(response.get("id") or "")
                    if isinstance(locals().get("response"), dict)
                    else ""
                )
                if provider_task_id:
                    # The POST definitely returned a provider identity.  Keep
                    # the paid task reconcilable instead of converting a local
                    # persistence/transfer failure into an unrecoverable bill.
                    current = await _owned_job(job.id, ctx, "segment")
                    if current and current.status != "completed":
                        await _update_job(
                            job.id,
                            provider_task_id=provider_task_id,
                            status="in_progress",
                            attempt=max(1, current.attempt or 0),
                            error=("Provider task accepted; local reconciliation pending. " + _public_error(exc)),
                        )
                        current = await _owned_job(job.id, ctx, "segment")
                    if current:
                        current_asset = await _job_asset(current)
                        return ToolResult(
                            title=(
                                "Video segment ready"
                                if current.status == "completed"
                                else "Video generation accepted; reconciliation pending"
                            ),
                            output="\n".join(_job_lines(current, current_asset)),
                            metadata={
                                "job_id": current.id,
                                "status": current.status,
                                "retry_after_seconds": 5,
                            },
                        )
                await _update_job(
                    job.id,
                    status="failed",
                    error=(
                        "Submission outcome may be ambiguous; do not automatically submit a second paid task. "
                        + _public_error(exc)
                    ),
                    completed_at=datetime.now(timezone.utc),
                )
                await _mark_asset(job.output_asset_id, status="failed")
            return ToolResult(title="Video generation submit failed", output=_public_error(exc))

    job = await _owned_job(args.job_id or "", ctx, "segment")
    if not job:
        return ToolResult(title="Video job not found", output="No owned segment job has that job_id.")
    if args.action == "cancel":
        if job.status in _SEGMENT_TERMINAL:
            asset = await _job_asset(job)
            return ToolResult(
                title="Video generation already finished",
                output="\n".join(_job_lines(job, asset)),
                metadata={"job_id": job.id, "status": job.status},
            )
        if job.status == "finalizing":
            return ToolResult(
                title="Video finalization in progress",
                output="The provider task has completed and its output is being secured in OSS; it can no longer be cancelled.",
                metadata={"job_id": job.id, "status": job.status},
            )
        if job.provider_task_id and job.status != "transfer_failed":
            try:
                await _provider_cancel(target, job.provider_task_id)
            except Exception as exc:
                return ToolResult(title="Video cancellation failed", output=_public_error(exc))
        await _update_job(
            job.id, status="cancelled", completed_at=datetime.now(timezone.utc), error="cancelled"
        )
        await _mark_asset(job.output_asset_id, status="failed")
        job = await _owned_job(job.id, ctx, "segment")
        return ToolResult(title="Video generation cancelled", output="\n".join(_job_lines(job)))

    deadline = asyncio.get_running_loop().time() + (args.wait_seconds if args.action == "wait" else 0)
    while job.status not in _SEGMENT_TERMINAL:
        if job.status == "finalizing":
            updated = job.updated_at
            if updated and updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated and (datetime.now(timezone.utc) - updated).total_seconds() > 300:
                await _update_job(
                    job.id,
                    status="transfer_failed",
                    error="recovering a stale OSS finalization",
                )
                job = await _owned_job(job.id, ctx, "segment")
                continue
            break
        if not job.provider_task_id:
            break
        try:
            data = await _provider_status(target, job.provider_task_id)
            state = _provider_state(data)
            if state == "completed":
                await ctx.update_output("Provider completed; copying the video to OSS…")
                job = await _finalize_segment(job, data, ctx, settings)
            elif state in {"failed", "cancelled"}:
                detail = data.get("error")
                message = detail.get("message") if isinstance(detail, dict) else str(detail or state)
                await _update_job(
                    job.id,
                    status=state,
                    error=message[:1000],
                    completed_at=datetime.now(timezone.utc),
                )
                await _mark_asset(job.output_asset_id, status="failed")
                job = await _owned_job(job.id, ctx, "segment")
            else:
                await _update_job(job.id, status=state, error=None)
                job = await _owned_job(job.id, ctx, "segment")
        except Exception as exc:
            return ToolResult(title="Video status check failed", output=_public_error(exc))
        if args.action != "wait" or job.status in _SEGMENT_TERMINAL:
            break
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        await ctx.update_output(f"Video is {job.status}; waiting for the provider…")
        await asyncio.sleep(min(settings.poll_interval_seconds, remaining))

    asset = await _job_asset(job)
    if job.status == "completed":
        attached = await _attach_completed(job, ctx)
        job = await _owned_job(job.id, ctx, "segment")
        title = "Video segment ready"
    else:
        attached = False
        title = "Video generation status"
    return ToolResult(
        title=title,
        output="\n".join(_job_lines(job, asset, retry_after=round(settings.poll_interval_seconds))),
        metadata={
            "job_id": job.id,
            "status": job.status,
            "asset_id": asset.id if asset and asset.status == "ready" else None,
            "attached": attached,
            "retry_after_seconds": round(settings.poll_interval_seconds),
        },
    )


async def _render_payload(job, ctx: ToolContext, settings, oss) -> dict[str, Any]:
    from sandbox.assets import _use_internal_oss

    data = job.request_data or {}
    refs = list(data.get("segment_asset_ids") or [])
    rows = await _resolve_inputs(refs, ctx)
    for row in rows:
        if row.mime not in _VIDEO_MIMES:
            raise RuntimeError(f"render input {row.id} is {row.mime}, not a supported video")
    ttl = settings.render_url_ttl_seconds
    asset = await _job_asset(job)
    if not asset:
        raise RuntimeError("reserved render output asset is missing")
    internal = _use_internal_oss(oss)
    return {
        "job_id": job.id,
        "owner": ctx.user_id,
        "session_id": ctx.session_id,
        "idempotency_key": job.idempotency_key,
        "inputs": [
            {
                "name": row.name,
                "mime": row.mime,
                "size": row.size,
                "cache_key": f"{oss.bucket}:{row.oss_key}:{row.size}",
                "url": oss.presign_get(row.oss_key, expires_sec=ttl, internal=internal),
            }
            for row in rows
        ],
        "output": {
            "name": asset.name,
            "mime": "video/mp4",
            "put_url": oss.presign_put(
                asset.oss_key, "video/mp4", expires_sec=ttl, internal=internal
            ),
        },
        "captions": list(data.get("captions") or []),
        "subtitles": bool(data.get("subtitles", True)),
        "channel_name": str(data.get("channel_name") or ""),
        "render_engine": str(data.get("render_engine") or "auto"),
        "width": int(data.get("width") or 720),
        "height": int(data.get("height") or 1280),
    }


async def _dispatch_render(job, ctx: ToolContext, settings, oss) -> tuple[Any, dict[str, Any]]:
    payload = await _render_payload(job, ctx, settings, oss)
    remote = await ctx.sandbox.submit_media_job(payload)
    await _update_job(
        job.id,
        sandbox_job_id=remote["job_id"],
        status=remote["status"],
        error=None,
        started_at=datetime.now(timezone.utc) if remote["status"] == "in_progress" else None,
    )
    return await _owned_job(job.id, ctx, "render"), remote


async def _sync_render(job, remote: dict[str, Any], ctx: ToolContext, oss) -> Any:
    status = remote.get("status") or "failed"
    result = remote.get("result") if isinstance(remote.get("result"), dict) else {}
    if status == "completed":
        asset = await _job_asset(job)
        head = await oss.head(asset.oss_key) if asset else None
        if not head:
            status = "failed"
            remote["error"] = "sandbox reported completion but the OSS output is missing"
        else:
            await _mark_asset(job.output_asset_id, status="ready", size=head["size"] or 0)
            await _update_job(
                job.id,
                status="completed",
                result_data=result,
                error=None,
                completed_at=datetime.now(timezone.utc),
            )
            return await _owned_job(job.id, ctx, "render")
    if status in {"failed", "cancelled"}:
        await _mark_asset(job.output_asset_id, status="failed")
        await _update_job(
            job.id,
            status=status,
            result_data=result,
            error=str(remote.get("error") or status)[:1200],
            completed_at=datetime.now(timezone.utc),
        )
    else:
        await _update_job(job.id, status=status, result_data={"progress": remote.get("progress") or {}})
    return await _owned_job(job.id, ctx, "render")


async def execute_render(args: VideoRenderArgs, ctx: ToolContext) -> ToolResult:
    if not ctx.sandbox:
        return ToolResult(title="Sandbox unavailable", output="Video rendering requires the user's WUYING sandbox.")
    try:
        _, settings = _configured_target()
        from core.oss import get_oss

        oss = get_oss()
    except Exception as exc:
        return ToolResult(title="Video rendering is not configured", output=_public_error(exc))

    if args.action == "submit":
        try:
            rows = await _resolve_inputs(args.segment_assets, ctx)
            for row in rows:
                if row.mime not in _VIDEO_MIMES:
                    raise RuntimeError(f"asset {row.id} is {row.mime}; render inputs must be videos")
            request_data = {
                "segment_asset_ids": [row.id for row in rows],
                "captions": args.captions,
                "subtitles": args.subtitles,
                "channel_name": args.channel_name,
                "render_engine": args.render_engine,
                "width": args.width,
                "height": args.height,
            }
            job, asset, created = await _create_pending_job(
                ctx=ctx,
                kind="render",
                idempotency_key=args.idempotency_key or "",
                model="wuying-media@1",
                prompt=None,
                request_data=request_data,
                filename=args.filename,
            )
            if not created:
                if job.status == "completed":
                    await _attach_completed(job, ctx)
                return ToolResult(
                    title="Existing render job",
                    output="\n".join(_job_lines(job, asset)),
                    metadata={"job_id": job.id, "status": job.status, "idempotent_reuse": True},
                )
            await ctx.update_output("Queueing the HyperFrames/FFmpeg render on WUYING…")
            job, remote = await _dispatch_render(job, ctx, settings, oss)
            return ToolResult(
                title="Video render queued",
                output="\n".join(
                    _job_lines(
                        job,
                        asset,
                        queue_position=int(remote.get("queue_position") or 0),
                        retry_after=int(remote.get("retry_after_seconds") or 5),
                    )
                ),
                metadata={
                    "job_id": job.id,
                    "status": job.status,
                    "queue_position": remote.get("queue_position", 0),
                    "retry_after_seconds": remote.get("retry_after_seconds", 5),
                },
            )
        except Exception as exc:
            if "job" in locals() and created:
                await _update_job(job.id, status="dispatch_unknown", error=_public_error(exc))
            return ToolResult(title="Video render dispatch failed", output=_public_error(exc))

    job = await _owned_job(args.job_id or "", ctx, "render")
    if not job:
        return ToolResult(title="Render job not found", output="No owned render job has that job_id.")

    try:
        if args.action == "retry":
            replacement = await _render_payload(job, ctx, settings, oss)
            remote = await ctx.sandbox.retry_media_job(
                job.sandbox_job_id or job.id,
                ctx.user_id,
                replacement_payload=replacement,
            )
            await _mark_asset(job.output_asset_id, status="pending", size=0)
            await _update_job(job.id, status=remote["status"], error=None, completed_at=None)
        elif args.action == "cancel":
            remote = await ctx.sandbox.cancel_media_job(job.sandbox_job_id or job.id, ctx.user_id)
        else:
            if job.status in {"dispatch_unknown", "dispatching"} and not job.sandbox_job_id:
                try:
                    job, remote = await _dispatch_render(job, ctx, settings, oss)
                except Exception:
                    remote = await ctx.sandbox.get_media_job(job.id, ctx.user_id)
            elif args.action == "wait":
                await ctx.update_output("Waiting on the WUYING render queue…")
                remote = await ctx.sandbox.wait_media_job(
                    job.sandbox_job_id or job.id,
                    ctx.user_id,
                    after_version=args.after_version,
                    timeout=args.wait_seconds,
                )
            else:
                remote = await ctx.sandbox.get_media_job(job.sandbox_job_id or job.id, ctx.user_id)
        job = await _sync_render(job, remote, ctx, oss)
    except Exception as exc:
        return ToolResult(title="Render status check failed", output=_public_error(exc))

    asset = await _job_asset(job)
    attached = await _attach_completed(job, ctx) if job.status == "completed" else False
    if attached:
        job = await _owned_job(job.id, ctx, "render")
    queue_position = int(remote.get("queue_position") or 0)
    retry_after = int(remote.get("retry_after_seconds") or 0)
    lines = _job_lines(
        job,
        asset,
        queue_position=queue_position,
        retry_after=retry_after or 5,
    )
    progress = remote.get("progress") or {}
    version = int(remote.get("version") or 0)
    lines.append(f"version={version}")
    if job.status not in _RENDER_TERMINAL:
        lines.append(
            f"next_wait_after_version={version} next_wait_iteration={args.wait_iteration + 1}"
        )
    if progress:
        lines.append(f"progress={progress}")
    resource_check = (remote.get("result") or {}).get("resource_check")
    if resource_check:
        lines.append(f"resource_check={resource_check}")
    return ToolResult(
        title="Rendered video ready" if job.status == "completed" else "Video render status",
        output="\n".join(lines),
        metadata={
            "job_id": job.id,
            "status": job.status,
            "asset_id": asset.id if asset and asset.status == "ready" else None,
            "queue_position": queue_position,
            "retry_after_seconds": retry_after,
            "version": version,
            "attached": attached,
            "resource_check": resource_check,
        },
    )


VIDEO_GENERATE_DESCRIPTION = """\
Submit, inspect, wait for, or cancel an asynchronous Seedance video generation. \
Inputs are owned OSS image/video asset IDs. Completed output is copied to OSS, \
indexed in the resource centre, and attached to chat. Billable submit requires \
an idempotency_key; never automatically create a replacement task after an \
ambiguous provider error. Load the video-production skill before use."""

VIDEO_RENDER_DESCRIPTION = """\
Queue, inspect, wait for, cancel, or retry a durable WUYING HyperFrames/FFmpeg \
render. Inputs and output move over OSS; the desktop queue enforces its configured \
concurrency and returns queue_position/retry_after_seconds for bounded waiting. \
Terminal paths clean per-job temp files and report memory/process cleanup."""


video_generate_tool = define_tool(
    "video_generate",
    description=VIDEO_GENERATE_DESCRIPTION,
    parameters=VideoGenerateArgs,
    execute=execute_generate,
    sandbox_required=False,
    parallel_safe=False,
    skill_only=True,
)


video_render_tool = define_tool(
    "video_render",
    description=VIDEO_RENDER_DESCRIPTION,
    parameters=VideoRenderArgs,
    execute=execute_render,
    sandbox_required=True,
    parallel_safe=False,
    skill_only=True,
)
