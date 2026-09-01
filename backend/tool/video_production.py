"""Seedance generation and WUYING media-render orchestration.

Provider calls and ownership checks live on the backend.  FFmpeg and
HyperFrames live behind the sandbox action server's durable per-desktop queue.
The build agent exposes these tools directly; skills provide workflow guidance
but never change tool availability.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

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
_BOSSIP_RELAY_HOST = "openapi.bossipai.com.cn"
_BOSSIP_RELAY_MODELS = {
    "480p": "seedance-2.0-480-fastⅠ",
    "720p": "video-sd-720p-proⅠ",
    "1080p": "video-sd-1080p-pro",
}
_SEGMENT_FINALIZATION_TASKS: dict[str, asyncio.Task[Any]] = {}
_SEGMENT_FINALIZATION_STALE_SECONDS = 300
_FINALIZATION_HEARTBEAT_SECONDS = 20
_EXPECTED_PROVIDER_UNSET = object()


def _ctx_persist_guard(ctx: ToolContext):
    guard = getattr(ctx, "assert_run_current", None)
    return guard if callable(guard) else None


async def _assert_ctx_current(ctx: ToolContext) -> None:
    guard = _ctx_persist_guard(ctx)
    if guard is not None:
        await guard()


class VideoGenerateArgs(BaseModel):
    action: Literal["submit", "status", "wait", "cancel"]
    job_id: str | None = Field(default=None, max_length=96)
    production_id: str | None = Field(default=None, max_length=96)
    segment_id: str | None = Field(default=None, max_length=96)
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=180)
    wait_seconds: float = Field(default=25.0, ge=0.0, le=25.0)
    after_version: int = Field(default=0, ge=0)
    wait_iteration: int = Field(
        default=0,
        ge=0,
        description=(
            "Supply and increment this on every wait call; together with after_version "
            "it makes repeated bounded waits explicit rather than an accidental tool loop."
        ),
    )

    @model_validator(mode="after")
    def _required_by_action(self):
        if self.action == "submit":
            if not self.idempotency_key:
                raise ValueError("submit requires idempotency_key to prevent duplicate billing")
            if not self.production_id or not self.segment_id:
                raise ValueError("submit requires production_id and segment_id from video_project")
        elif not self.job_id:
            raise ValueError(f"{self.action} requires job_id")
        return self


class VideoRenderArgs(BaseModel):
    action: Literal["submit", "status", "wait", "cancel", "retry"]
    job_id: str | None = Field(default=None, max_length=96)
    production_id: str | None = Field(default=None, max_length=96)
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
            if not self.production_id:
                raise ValueError("submit requires production_id from video_project")
            if self.captions and len(self.captions) != len(self.segment_assets):
                raise ValueError("captions must be empty or match segment_assets length")
        elif not self.job_id:
            raise ValueError(f"{self.action} requires job_id")
        return self


class VideoTranscribeArgs(BaseModel):
    action: Literal["submit", "status", "wait", "cancel", "retry"]
    job_id: str | None = Field(default=None, max_length=96)
    production_id: str | None = Field(default=None, max_length=96)
    segment_id: str | None = Field(default=None, max_length=96)
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=180)
    wait_seconds: float = Field(default=25.0, ge=0.0, le=25.0)
    after_version: int = Field(default=0, ge=0)
    wait_iteration: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _required_by_action(self):
        if self.action == "submit":
            if not self.production_id or not self.segment_id:
                raise ValueError("submit requires production_id and segment_id")
            if not self.idempotency_key:
                raise ValueError("submit requires idempotency_key")
        elif not self.job_id:
            raise ValueError(f"{self.action} requires job_id")
        return self


# The provider target is now the multi-channel route object; the old name is
# kept for the existing call sites and tests. The ark channel additionally
# carries a wire_format (TokenSpace contents vs the BossIP public relay).
from tool.video_providers import VideoRoute as VideoProviderTarget  # noqa: E402


@dataclass(frozen=True)
class VideoTranscriptionTarget:
    engine: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int
    poll_interval_seconds: float
    similarity_threshold: float


def _configured_target(model_override: str | None = None) -> tuple[VideoProviderTarget, object]:
    """Resolve the submission route (channel + credentials) for a model.

    Routing lives in tool/video_providers.py; the legacy doubao/ark
    configuration resolves exactly as before.
    """
    from core.config import get_config
    from tool.video_providers import resolve_route

    config = get_config()
    return resolve_route(model_override, config), config.video_generation


def _configured_transcription_target() -> VideoTranscriptionTarget:
    from core.config import get_config

    settings = get_config().video_transcription
    engine = settings.engine.strip().lower()
    if engine not in {"dashscope", "openai_url"}:
        raise RuntimeError("video_transcription.engine must be dashscope or openai_url")
    base_url = settings.base_url.rstrip("/")
    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        raise RuntimeError("video_transcription.base_url is not configured")
    if not settings.api_key:
        raise RuntimeError("video_transcription.api_key is not configured")
    if not settings.model:
        raise RuntimeError("video_transcription.model is not configured")
    return VideoTranscriptionTarget(
        engine=engine,
        model=settings.model,
        api_key=settings.api_key,
        base_url=base_url,
        timeout_seconds=settings.timeout_seconds,
        poll_interval_seconds=settings.poll_interval_seconds,
        similarity_threshold=settings.similarity_threshold,
    )


def _auth_header(key: str) -> str:
    return key if key.lower().startswith("bearer ") else f"Bearer {key}"


def _content_url(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("url") or "").strip()
    return ""


def _bossip_video_payload(target: VideoProviderTarget, payload: dict[str, Any]) -> dict[str, Any]:
    """Translate TokenSpace contents format to BossIP's public `/v1/videos` shape.

    The relay deliberately owns material-library upload under its own upstream
    account. OpenBox therefore gives it short-lived OSS URLs instead of
    account-scoped TokenSpace ``asset://`` identifiers.
    """
    resolution = str(payload.get("resolution") or "").lower()
    if resolution not in _BOSSIP_RELAY_MODELS:
        raise RuntimeError(f"BossIP relay does not support resolution: {resolution}")

    configured_model = target.model.strip()
    model = (
        configured_model
        if configured_model in _BOSSIP_RELAY_MODELS.values()
        else _BOSSIP_RELAY_MODELS[resolution]
    )
    native_resolution = next(
        key for key, candidate in _BOSSIP_RELAY_MODELS.items() if candidate == model
    )
    if native_resolution != resolution:
        raise RuntimeError(
            f"BossIP relay model {model} requires {native_resolution}, not {resolution}"
        )
    if resolution == "480p" and payload.get("generate_audio") is True:
        raise RuntimeError(
            "BossIP relay 480p compatibility model does not support generated audio; use 720p or 1080p"
        )

    prompt = ""
    images: list[str] = []
    videos: list[str] = []
    for item in payload.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind == "text" and not prompt:
            prompt = str(item.get("text") or "").strip()
        elif kind == "image_url":
            url = _content_url(item.get("image_url"))
            if url:
                images.append(url)
        elif kind == "video_url":
            url = _content_url(item.get("video_url"))
            if url:
                videos.append(url)
    if not prompt:
        raise RuntimeError("BossIP relay video request requires a prompt")

    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "resolution": resolution,
    }
    duration = payload.get("duration")
    if isinstance(duration, int) and 4 <= duration <= 15:
        body["duration"] = duration
    ratio = str(payload.get("ratio") or "").strip()
    if ratio and ratio != "adaptive":
        body["ratio"] = ratio
    for option in ("generate_audio", "watermark", "return_last_frame", "seed"):
        if option in payload:
            body[option] = payload[option]
    if images:
        body["image_url"] = images[0]
        if len(images) > 1:
            body["extra_images"] = images[1:]
    if videos:
        body["extra_videos"] = videos
    return body


def _safe_filename(requested: str | None, job_id: str, *, rendered: bool = False) -> str:
    fallback = f"{'final' if rendered else 'segment'}-{job_id}.mp4"
    raw = PurePosixPath(requested or fallback).name
    stem = re.sub(r"\.(mp4|mov|webm|m4v)$", "", raw, flags=re.IGNORECASE)
    stem = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", stem).strip("._") or fallback[:-4]
    return f"{stem[:180]}.mp4"


def _safe_audio_filename(requested: str | None, job_id: str) -> str:
    raw = PurePosixPath(requested or f"speech-{job_id}.mp3").name
    stem = re.sub(r"\.(mp3|wav|m4a|aac|ogg)$", "", raw, flags=re.IGNORECASE)
    stem = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", stem).strip("._") or f"speech-{job_id}"
    return f"{stem[:180]}.mp3"


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


async def _resolve_generation_inputs(
    character_reference: str | None,
    refs: list[str],
    ctx: ToolContext,
) -> tuple[list[Any], Any | None]:
    """Resolve provider inputs with one explicit, auditable character image first."""
    rows = await _resolve_inputs(refs, ctx)
    if not character_reference:
        return rows, None

    character = await _find_owned_asset(character_reference, ctx)
    if not character:
        raise RuntimeError(
            f"character reference '{character_reference}' is not a ready OSS resource owned by this user"
        )
    if not character.mime.startswith(_IMAGE_PREFIX):
        raise RuntimeError(
            f"character reference '{character_reference}' is {character.mime}; "
            "character_reference_asset must be an image"
        )

    # The explicit reference is always first in the provider content and is
    # recorded separately in request_data. Deduplicate it if an agent also put
    # the same asset in the generic list, so provider input limits stay stable.
    ordered = [character, *(row for row in rows if row.id != character.id)]
    if len(ordered) > 8:
        raise RuntimeError("video generation accepts at most 8 distinct input assets")
    return ordered, character


async def _materialize_provider_inputs(
    rows: list[Any],
    character: Any | None,
    *,
    character_reference_type: str,
    character_identity_id: str | None,
    ctx: ToolContext,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert owned OSS rows into stable provider ``asset://`` references.

    An actual person's explicit character image is constrained to their active
    LivenessFace group. Every other reference goes through the user's AIGC
    material group, which removes expiring/cross-account URLs from paid tasks.
    """
    from video.materials import materialize_generation_asset

    content: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for row in rows:
        real_character = bool(
            character
            and row.id == character.id
            and character_reference_type == "real_person"
        )
        if real_character and not character_identity_id:
            raise RuntimeError(
                "real-person generation requires an active character identity before submission"
            )
        uri, binding = await materialize_generation_asset(
            ctx.user_id,
            row.id,
            identity_id=character_identity_id if real_character else None,
        )
        if row.mime.startswith(_IMAGE_PREFIX):
            content.append(
                {"type": "image_url", "image_url": {"url": uri}, "role": "reference_image"}
            )
        else:
            content.append(
                {"type": "video_url", "video_url": {"url": uri}, "role": "reference_video"}
            )
        bindings.append(
            {
                "source_asset_id": row.id,
                "material_asset_id": binding.get("material_asset_id"),
                "provider_asset_id": binding.get("provider_asset_id"),
                "group_id": binding.get("identity_id"),
                "group_type": "LivenessFace" if real_character else "AIGC",
            }
        )
    return content, bindings


def _relay_provider_inputs(
    rows: list[Any],
    character: Any | None,
    *,
    character_reference_type: str,
    input_url_ttl_seconds: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give BossIP public source URLs and let its adapter own materialization.

    TokenSpace asset ids are scoped to the upstream credential that created
    them. Reusing OpenBox's direct-account ids through the relay would fail as
    cross-account assets, so the relay receives signed OSS URLs and records its
    own stable material bindings instead.
    """
    from core.oss import get_oss

    oss = get_oss()
    content: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for row in rows:
        url = oss.presign_get(row.oss_key, expires_sec=input_url_ttl_seconds)
        if row.mime.startswith(_IMAGE_PREFIX):
            content.append(
                {"type": "image_url", "image_url": {"url": url}, "role": "reference_image"}
            )
        else:
            content.append(
                {"type": "video_url", "video_url": {"url": url}, "role": "reference_video"}
            )
        requested_reference_type = (
            "real_person"
            if (
                character
                and row.id == character.id
                and character_reference_type == "real_person"
            )
            else "virtual"
        )
        bindings.append(
            {
                "source_asset_id": row.id,
                "managed_by": "bossip_relay",
                # The current BossIP adapter owns an AIGC material group. A
                # prior LivenessFace approval on another TokenSpace account
                # cannot be claimed or transferred by changing gateways.
                "group_type": "AIGC",
                "requested_reference_type": requested_reference_type,
            }
        )
    return content, bindings


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
    request_hash: str,
    production_id: str | None = None,
    segment_id: str | None = None,
    output_mime: str = "video/mp4",
    transient: bool = False,
    prompt_hash: str | None = None,
) -> tuple[Any, Any, bool]:
    """Reserve job+asset before a paid/remote call; return existing on a race."""
    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob
    from sandbox.assets import _session_project

    async with get_db_session() as db:
        await _assert_effect_fence_locked(db, ctx)
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
            if existing.request_hash and existing.request_hash != request_hash:
                raise RuntimeError(
                    "idempotency key conflict: the existing job has a different request hash"
                )
            asset = await db.get(FileAsset, existing.output_asset_id) if existing.output_asset_id else None
            return existing, asset, False

    job_id = ascending("video")
    asset_id = ascending("asset")
    name = (
        _safe_audio_filename(filename, job_id)
        if output_mime == "audio/mpeg"
        else _safe_filename(filename, job_id, rendered=kind == "render")
    )
    key = f"assets/{ctx.user_id}/{asset_id}/{name}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        await _assert_effect_fence_locked(db, ctx)
        project_id = ctx.project_id or await _session_project(db, ctx.session_id, ctx.user_id)
        asset = FileAsset(
            id=asset_id,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            project_id=project_id or None,
            name=name,
            oss_key=key,
            mime=output_mime,
            size=0,
            status="pending",
            source="agent",
            transient=transient,
            created_at=now,
        )
        job = VideoJob(
            id=job_id,
            user_id=ctx.user_id,
            session_id=ctx.session_id or None,
            project_id=project_id or None,
            kind=kind,
            production_id=production_id,
            segment_id=segment_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            prompt_hash=prompt_hash,
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
        await _assert_effect_fence_locked(db, ctx)
        existing = (
            await db.execute(
                select(VideoJob).where(
                    VideoJob.user_id == ctx.user_id,
                    VideoJob.kind == kind,
                    VideoJob.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one()
        if existing.request_hash and existing.request_hash != request_hash:
            raise RuntimeError(
                "idempotency key conflict: the existing job has a different request hash"
            )
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


async def _assert_effect_fence_locked(db, ctx: ToolContext | None) -> None:
    """Fence a media read-model write in the same transaction as the write.

    Provider receipts use their own job CAS because they must remain durable
    after a paid request has been accepted.  All ordinary Agent-owned status
    and asset projections use this fence so a replaced generation cannot
    publish a late success/cancellation.
    """
    run_fence = getattr(ctx, "run_fence", None) if ctx is not None else None
    if run_fence is None:
        return
    from agent.driver import assert_run_fence_locked

    session_id, run_id, generation = run_fence
    await assert_run_fence_locked(
        db,
        session_id=session_id,
        user_id=ctx.user_id,
        run_id=run_id,
        generation=generation,
    )


async def _update_job(
    job_id: str,
    *,
    ctx: ToolContext | None = None,
    expected_statuses: set[str] | tuple[str, ...] | list[str] | None = None,
    expected_provider_task_id: str | None | object = _EXPECTED_PROVIDER_UNSET,
    **values,
) -> bool:
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    values["updated_at"] = datetime.now(timezone.utc)
    async with get_db_session() as db:
        await _assert_effect_fence_locked(db, ctx)
        conditions = [VideoJob.id == job_id]
        if ctx is not None and ctx.user_id:
            conditions.append(VideoJob.user_id == ctx.user_id)
        if expected_statuses is not None:
            conditions.append(VideoJob.status.in_(tuple(expected_statuses)))
        if expected_provider_task_id is not _EXPECTED_PROVIDER_UNSET:
            conditions.append(
                VideoJob.provider_task_id.is_(None)
                if expected_provider_task_id is None
                else VideoJob.provider_task_id == expected_provider_task_id
            )
        result = await db.execute(
            update(VideoJob)
            .where(*conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1


async def _mark_asset(
    asset_id: str | None,
    *,
    status: str,
    size: int | None = None,
    ctx: ToolContext | None = None,
) -> bool:
    if not asset_id:
        return False
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    values: dict[str, Any] = {"status": status}
    if size is not None:
        values["size"] = size
    async with get_db_session() as db:
        await _assert_effect_fence_locked(db, ctx)
        conditions = [FileAsset.id == asset_id]
        if ctx is not None and ctx.user_id:
            conditions.append(FileAsset.user_id == ctx.user_id)
        result = await db.execute(
            update(FileAsset)
            .where(*conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1


async def _persist_video_submit_receipt(
    job,
    *,
    user_id: str,
    task_id: str,
    stored_state: str,
) -> None:
    """Durably bind a paid provider task without overwriting a newer owner.

    The ordinary path advances ``submitting`` to the provider state. If a
    cancellation/takeover changed the job after the POST was sent, only the
    immutable provider receipt is filled in; the newer state is preserved.
    """
    from sqlalchemy import or_

    from db.base import get_db_session
    from db.models.video_job import VideoJob

    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        advanced = await db.execute(
            update(VideoJob)
            .where(
                VideoJob.id == job.id,
                VideoJob.user_id == user_id,
                VideoJob.status == "submitting",
                or_(
                    VideoJob.provider_task_id.is_(None),
                    VideoJob.provider_task_id == task_id,
                ),
            )
            .values(
                provider_task_id=task_id,
                status=stored_state,
                attempt=1,
                started_at=now,
                error=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if advanced.rowcount == 1:
            return
        receipt_only = await db.execute(
            update(VideoJob)
            .where(
                VideoJob.id == job.id,
                VideoJob.user_id == user_id,
                VideoJob.provider_task_id.is_(None),
            )
            .values(provider_task_id=task_id, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if receipt_only.rowcount == 1:
            return
        current = await db.get(VideoJob, job.id)
        if not current or current.user_id != user_id or current.provider_task_id != task_id:
            raise RuntimeError("provider receipt conflicts with the durable video job")


async def _settle_job_and_asset(
    job,
    ctx: ToolContext,
    *,
    job_status: str,
    asset_status: str,
    error: str | None,
    expected_statuses: set[str] | tuple[str, ...] | list[str],
    result_data: dict[str, Any] | None = None,
    size: int | None = None,
) -> bool:
    """Atomically settle the job and its reserved asset under one run fence."""
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob

    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        await _assert_effect_fence_locked(db, ctx)
        changed = await db.execute(
            update(VideoJob)
            .where(
                VideoJob.id == job.id,
                VideoJob.user_id == ctx.user_id,
                VideoJob.status.in_(tuple(expected_statuses)),
            )
            .values(
                status=job_status,
                error=error,
                result_data=result_data if result_data is not None else job.result_data,
                completed_at=(now if job_status in _SEGMENT_TERMINAL | _RENDER_TERMINAL else None),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            return False
        if job.output_asset_id:
            asset_values: dict[str, Any] = {"status": asset_status}
            if size is not None:
                asset_values["size"] = size
            asset_changed = await db.execute(
                update(FileAsset)
                .where(
                    FileAsset.id == job.output_asset_id,
                    FileAsset.user_id == ctx.user_id,
                )
                .values(**asset_values)
                .execution_options(synchronize_session=False)
            )
            if asset_changed.rowcount != 1:
                raise RuntimeError("reserved media asset disappeared during settlement")
        return True


async def _attach_completed(job, ctx: ToolContext) -> bool:
    if not job.output_asset_id or not ctx.message_id or job.attached_message_id:
        return bool(job.attached_message_id)
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_production import VideoProduction, VideoSegment
    from db.models.video_job import VideoJob
    from models.message import FilePart, FileRelation
    from project.workspace import asset_sandbox_path
    from session.session import save_part

    claimed = False
    async with get_db_session() as db:
        await _assert_effect_fence_locked(db, ctx)
        result = await db.execute(
            update(VideoJob)
            .where(
                VideoJob.id == job.id,
                VideoJob.user_id == ctx.user_id,
                VideoJob.status == "completed",
                VideoJob.attached_message_id.is_(None),
            )
            .values(attached_message_id=ctx.message_id, updated_at=datetime.now(timezone.utc))
        )
        claimed = result.rowcount == 1
        asset = await db.get(FileAsset, job.output_asset_id)
        production = (
            await db.get(VideoProduction, job.production_id)
            if job.production_id
            else None
        )
        segment = (
            await db.get(VideoSegment, job.segment_id)
            if job.segment_id
            else None
        )
    if not claimed:
        current = await _owned_job(job.id, ctx, job.kind)
        return bool(current and current.attached_message_id)
    if not asset:
        await _update_job(job.id, ctx=ctx, attached_message_id=None)
        return False
    try:
        await save_part(
            FilePart(
                path=asset_sandbox_path(
                    asset.user_id,
                    asset.project_id,
                    asset.name,
                    asset_id=asset.id,
                ),
                mime_type=asset.mime,
                asset_id=asset.id,
                oss_key=asset.oss_key,
                size=asset.size,
                transient=False,
                relation=FileRelation(
                    source_part_id=ctx.part_id or None,
                    group_id=(
                        f"video:{job.production_id}:segment:{job.segment_id}"
                        if job.kind == "segment" and job.segment_id
                        else f"video:{job.production_id or job.id}:final"
                    ),
                    role="intermediate" if job.kind == "segment" else "final",
                    kind="video_segment" if job.kind == "segment" else "video_final",
                    label=(production.title if production else asset.name),
                    caption=(
                        segment.script_text
                        if segment
                        else ((production.brief or production.title) if production else None)
                    ),
                    ordinal=segment.ordinal if segment else None,
                    revision=segment.revision if segment else None,
                    metadata={
                        "production_id": job.production_id,
                        "segment_id": job.segment_id,
                        "job_id": job.id,
                        "transcript": segment.transcript_text if segment else None,
                        "stt_verdict": segment.stt_verdict if segment else None,
                        "stt_similarity": segment.stt_similarity if segment else None,
                        "subtitles": (
                            bool((job.request_data or {}).get("subtitles"))
                            if job.kind == "render"
                            else None
                        ),
                        "render_engine": (
                            (job.request_data or {}).get("render_engine")
                            if job.kind == "render"
                            else None
                        ),
                    },
                ),
                session_id=ctx.session_id,
                message_id=ctx.message_id,
            ),
            is_new=True,
            user_id=ctx.user_id,
            run_fence=ctx.run_fence,
        )
        return True
    except Exception as exc:
        from agent.driver import LeaseLostError

        if isinstance(exc, LeaseLostError):
            # Never turn a failed fenced projection into an apparent success.
            # The attachment marker is intentionally left for operator/local
            # reconciliation because VideoJob has no generation claim column.
            raise
        await _update_job(job.id, ctx=ctx, attached_message_id=None)
        log.warning("video asset saved but chat attachment failed", exc_info=True)
        return False


def _public_error(exc: Exception) -> str:
    """Return a stable user-visible diagnostic without provider bodies/URLs.

    HTTP response bodies can echo prompts, signed object URLs or credentials;
    request exception strings commonly include the full URL. Detailed failures
    stay in provider-side correlation logs, while persisted job state carries
    only the class/status needed for recovery and support.
    """
    if getattr(exc, "public_message", False):
        return str(exc)[:500]
    response = getattr(exc, "response", None)
    if response is not None:
        reason = str(getattr(response, "reason_phrase", "") or "request failed")
        return f"HTTP {response.status_code}: {reason}"[:200]
    return f"{exc.__class__.__name__}: operation failed"


def _failure_result(
    title: str,
    error: Exception | str,
    *,
    failure_code: str,
    **metadata: Any,
) -> ToolResult:
    output = _public_error(error) if isinstance(error, Exception) else str(error)[:800]
    return ToolResult(
        title=title,
        output=output,
        metadata={
            "error": True,
            "failure_code": failure_code,
            **metadata,
        },
    )


async def _provider_submit(target: VideoProviderTarget, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    relay = target.wire_format == "bossip_videos"
    path = "/v1/videos" if relay else "/api/v3/contents/generations/tasks"
    request_payload = _bossip_video_payload(target, payload) if relay else payload
    async with httpx.AsyncClient(timeout=target.submit_timeout_seconds, follow_redirects=True) as client:
        response = await client.post(
            f"{target.base_url}{path}",
            headers={"Authorization": _auth_header(target.api_key), "Content-Type": "application/json"},
            json=request_payload,
        )
    if response.status_code not in (200, 201, 202):
        response.raise_for_status()
    data = response.json()
    if not data.get("id"):
        raise RuntimeError("video provider response did not include a task id")
    return data


async def _provider_status(target: VideoProviderTarget, task_id: str) -> dict[str, Any]:
    import httpx

    if getattr(target, "channel", "ark") != "ark":
        from tool import video_providers

        return await video_providers.status(target, task_id)
    encoded_task_id = quote(task_id, safe="")
    path = (
        f"/v1/videos/{encoded_task_id}"
        if target.wire_format == "bossip_videos"
        else f"/api/v3/contents/generations/tasks/{encoded_task_id}"
    )
    async with httpx.AsyncClient(timeout=target.status_timeout_seconds, follow_redirects=True) as client:
        response = await client.get(
            f"{target.base_url}{path}",
            headers={"Authorization": _auth_header(target.api_key)},
        )
    response.raise_for_status()
    return response.json()


async def _provider_cancel(target: VideoProviderTarget, task_id: str) -> None:
    import httpx

    if getattr(target, "channel", "ark") != "ark":
        # The gateway channels expose no upstream cancel API; the caller marks
        # the local job cancelled and the provider task may still complete.
        return
    if target.wire_format == "bossip_videos":
        raise RuntimeError(
            "BossIP public relay does not expose remote task cancellation; the provider task may continue"
        )
    async with httpx.AsyncClient(timeout=target.status_timeout_seconds) as client:
        response = await client.delete(
            f"{target.base_url}/api/v3/contents/generations/tasks/{task_id}",
            headers={"Authorization": _auth_header(target.api_key)},
        )
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


async def _dashscope_transcribe(
    target: VideoTranscriptionTarget,
    audio_url: str,
    *,
    task_id: str | None = None,
    operation_key: str | None = None,
    receipt_callback=None,
) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=target.timeout_seconds, follow_redirects=True) as client:
        if not task_id:
            submit_headers = {
                "Authorization": _auth_header(target.api_key),
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            }
            if operation_key:
                # Correlation only. DashScope does not document this header as
                # an idempotency guarantee, so unknown POST outcomes still stop.
                submit_headers["X-Client-Request-Id"] = operation_key
            submitted = await client.post(
                f"{target.base_url}/api/v1/services/audio/asr/transcription",
                headers=submit_headers,
                json={
                    "model": target.model,
                    "input": {"file_urls": [audio_url]},
                    "parameters": {"channel_id": [0], "language_hints": ["zh"]},
                },
            )
            submitted.raise_for_status()
            submitted_data = submitted.json()
            task_id = str((submitted_data.get("output") or {}).get("task_id") or "")
            if not task_id:
                raise RuntimeError("DashScope transcription response did not include a task_id")
            if receipt_callback is not None:
                await receipt_callback(task_id)

        deadline = asyncio.get_running_loop().time() + target.timeout_seconds
        finished: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            response = await client.get(
                f"{target.base_url}/api/v1/tasks/{task_id}",
                headers={"Authorization": _auth_header(target.api_key)},
            )
            response.raise_for_status()
            data = response.json()
            state = str((data.get("output") or {}).get("task_status") or "").upper()
            if state in {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}:
                finished = data
                break
            await asyncio.sleep(target.poll_interval_seconds)
        if finished is None:
            raise RuntimeError("DashScope transcription timed out")

        output = finished.get("output") or {}
        results = output.get("results") or []
        result = results[0] if results and isinstance(results[0], dict) else {}
        state = str(output.get("task_status") or "").upper()
        subtask_state = str(result.get("subtask_status") or "").upper()
        code = str(result.get("code") or output.get("code") or "")
        if state != "SUCCEEDED" or subtask_state not in {"", "SUCCEEDED"}:
            if "HAVE_NO_WORDS" in code.upper():
                return {
                    "text": "",
                    "duration_ms": None,
                    "model": target.model,
                    "provider": "dashscope",
                }
            detail = str(result.get("message") or output.get("message") or code or state)
            raise RuntimeError(f"DashScope transcription failed: {detail[:300]}")
        result_url = str(result.get("transcription_url") or "")
        if not result_url.startswith("https://"):
            raise RuntimeError("DashScope transcription completed without a result URL")
        downloaded = await client.get(result_url)
        downloaded.raise_for_status()
        transcript_data = downloaded.json()

    transcripts = transcript_data.get("transcripts") or []
    text = "\n".join(
        str(item.get("text") or "").strip()
        for item in transcripts
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ).strip()
    duration_ms = (transcript_data.get("properties") or {}).get(
        "original_duration_in_milliseconds"
    )
    return {
        "text": text,
        "duration_ms": duration_ms,
        "model": target.model,
        "provider": "dashscope",
    }


async def _provider_transcribe(
    target: VideoTranscriptionTarget,
    audio_url: str,
    *,
    task_id: str | None = None,
    operation_key: str | None = None,
    receipt_callback=None,
) -> dict[str, Any]:
    if target.engine == "dashscope":
        return await _dashscope_transcribe(
            target,
            audio_url,
            task_id=task_id,
            operation_key=operation_key,
            receipt_callback=receipt_callback,
        )

    import httpx

    async with httpx.AsyncClient(timeout=target.timeout_seconds, follow_redirects=True) as client:
        headers = {
            "Authorization": _auth_header(target.api_key),
            "Content-Type": "application/json",
        }
        if operation_key:
            headers["X-Client-Request-Id"] = operation_key
        response = await client.post(
            f"{target.base_url}/v1/audio/transcriptions",
            headers=headers,
            json={
                "model": target.model,
                "audio_url": audio_url,
                "response_format": "json",
            },
        )
    response.raise_for_status()
    data = response.json()
    text = str(data.get("text") or "").strip()
    if not text:
        raise RuntimeError("transcription returned no spoken text")
    return {
        "text": text,
        "duration_ms": data.get("duration_ms") or data.get("durationMs"),
        "model": target.model,
        "provider": "openai_url",
    }


def _provider_state(data: dict[str, Any], route: Any = None) -> str:
    if route is not None and getattr(route, "channel", "ark") != "ark":
        from tool import video_providers

        return video_providers.normalize_state(route, data)
    value = str(data.get("status") or "").lower()
    return {
        "queued": "queued",
        "pending": "queued",
        "running": "in_progress",
        "processing": "in_progress",
        "in_progress": "in_progress",
        "succeeded": "completed",
        "success": "completed",
        "completed": "completed",
        "failed": "failed",
        "failure": "failed",
        "error": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }.get(value, "in_progress")


def _provider_video_url(data: dict[str, Any], route: Any = None) -> str:
    if route is not None and getattr(route, "channel", "ark") != "ark":
        from tool import video_providers

        return video_providers.result_video_url(route, data)
    containers = [data]
    for key in ("result", "data", "content"):
        value = data.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for key in ("video_url", "url", "download_url", "result_url"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return ""


async def _copy_provider_video_to_oss(
    url: str,
    oss,
    key: str,
    max_bytes: int,
    *,
    heartbeat=None,
) -> int:
    import hashlib
    import httpx

    put_url = oss.presign_put(key, "video/mp4", expires_sec=3600)
    total = 0
    source_complete = False
    digest = hashlib.md5(usedforsecurity=False)
    last_heartbeat = 0.0
    if heartbeat is not None:
        await heartbeat()
        last_heartbeat = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, read=1800.0), follow_redirects=True) as source:
        async with source.stream("GET", url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise RuntimeError("provider video exceeds configured output size limit")

            async def chunks():
                nonlocal total, last_heartbeat, source_complete
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    digest.update(chunk)
                    if total > max_bytes:
                        raise RuntimeError("provider video exceeds configured output size limit")
                    if (
                        heartbeat is not None
                        and asyncio.get_running_loop().time() - last_heartbeat
                        >= _FINALIZATION_HEARTBEAT_SECONDS
                    ):
                        await heartbeat()
                        last_heartbeat = asyncio.get_running_loop().time()
                    yield chunk
                source_complete = True

            headers = {"Content-Type": "video/mp4"}
            if declared:
                headers["Content-Length"] = str(declared)
            upload_error: Exception | None = None
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, write=1800.0)) as sink:
                    uploaded = await sink.put(put_url, content=chunks(), headers=headers)
                if uploaded.status_code not in (200, 201, 204):
                    upload_error = RuntimeError(
                        f"OSS upload returned HTTP {uploaded.status_code}"
                    )
            except Exception as exc:
                # A lost PUT response is not proof the object was not committed.
                upload_error = exc
            if heartbeat is not None:
                await heartbeat()
            if upload_error is not None:
                head = await oss.head(key)
                expected = declared or total
                etag = str((head or {}).get("etag") or "").strip('"').casefold()
                if (
                    not source_complete
                    or not head
                    or expected <= 0
                    or int(head.get("size") or 0) != expected
                    or not re.fullmatch(r"[0-9a-f]{32}", etag)
                    or etag != digest.hexdigest()
                ):
                    raise upload_error
    if total <= 0:
        raise RuntimeError("provider returned an empty video")
    head = await oss.head(key)
    if not head:
        raise RuntimeError("video is missing from OSS after upload")
    etag = str(head.get("etag") or "").strip('"').casefold()
    if re.fullmatch(r"[0-9a-f]{32}", etag) and etag != digest.hexdigest():
        raise RuntimeError("video OSS digest does not match the provider output")
    return head["size"] or total


async def _copy_provider_video_with_heartbeat(
    url: str,
    oss,
    key: str,
    max_bytes: int,
    heartbeat,
) -> int:
    import inspect

    kwargs = {}
    if "heartbeat" in inspect.signature(_copy_provider_video_to_oss).parameters:
        kwargs["heartbeat"] = heartbeat

    # A provider or OSS socket may stop yielding bytes for longer than the
    # stale-finalization window. Keep ownership alive independently of stream
    # progress, and abort the old transfer immediately if its lease/CAS is
    # rejected so recovery can never overlap it after 300 seconds.
    transfer = asyncio.create_task(
        _copy_provider_video_to_oss(url, oss, key, max_bytes, **kwargs),
        name=f"video-oss-transfer:{key}",
    )

    async def keep_alive() -> None:
        while True:
            await asyncio.sleep(_FINALIZATION_HEARTBEAT_SECONDS)
            await heartbeat()

    watchdog = asyncio.create_task(
        keep_alive(),
        name=f"video-oss-heartbeat:{key}",
    )
    try:
        done, _pending = await asyncio.wait(
            {transfer, watchdog}, return_when=asyncio.FIRST_COMPLETED
        )
        if watchdog in done:
            # The watchdog is intentionally infinite, so completion means its
            # heartbeat failed (including lease loss or a lost job CAS).
            error = watchdog.exception()
            transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)
            if error is not None:
                raise error
            raise RuntimeError("video finalization heartbeat stopped unexpectedly")
        return transfer.result()
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)


async def _finalize_segment(
    job,
    data: dict[str, Any],
    ctx: ToolContext,
    settings,
    # Positional: the routing callers pass `target` as the 5th argument.
    route: Any = None,
    *,
    persist_guard=None,
) -> Any:
    source_url = _provider_video_url(data, route)
    if not source_url.startswith("https://"):
        raise RuntimeError("provider marked the task completed without a video URL")
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob

    if persist_guard is not None:
        await persist_guard()
    claimed = False
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        await _assert_effect_fence_locked(
            db, ctx if persist_guard is not None else None
        )
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

    async def heartbeat() -> None:
        if persist_guard is not None:
            await persist_guard()
        touched = await _update_job(
            job.id,
            ctx=ctx if persist_guard is not None else None,
            expected_statuses={"finalizing"},
            status="finalizing",
        )
        if not touched:
            raise RuntimeError("video finalization ownership was lost")

    try:
        size = await _copy_provider_video_with_heartbeat(
            source_url,
            get_oss(),
            asset.oss_key,
            settings.max_provider_output_bytes,
            heartbeat,
        )
    except Exception as exc:
        # The paid provider task already succeeded. Keep this recoverable so a
        # later wait can fetch a fresh result URL and retry only OSS transfer.
        if persist_guard is not None:
            await persist_guard()
        moved_to_transfer_failed = await _update_job(
            job.id,
            ctx=ctx if persist_guard is not None else None,
            expected_statuses={"finalizing"},
            status="transfer_failed",
            error=_public_error(exc),
            result_data={"provider_status": data.get("status")},
        )
        if moved_to_transfer_failed:
            await _mark_asset(
                job.output_asset_id,
                status="pending",
                ctx=ctx if persist_guard is not None else None,
            )
        return await _owned_job(job.id, ctx, "segment")
    if persist_guard is not None:
        await persist_guard()
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    settled = await _settle_job_and_asset(
        job,
        ctx,
        job_status="completed",
        asset_status="ready",
        error=None,
        expected_statuses={"finalizing"},
        result_data={
            "usage": usage,
            "provider_status": data.get("status"),
            "bytes": size,
        },
        size=size,
    )
    if not settled:
        return await _owned_job(job.id, ctx, "segment")
    if getattr(job, "segment_id", None):
        from tool.video_workflow import mark_segment_job

        await _assert_ctx_current(ctx)
        await mark_segment_job(
            job.segment_id,
            job.id,
            user_id=ctx.user_id,
            status="completed",
            output_asset_id=job.output_asset_id,
        )
    return await _owned_job(job.id, ctx, "segment")


def _job_lines(
    job,
    asset=None,
    *,
    queue_position: int | None = None,
    retry_after: int | None = 5,
) -> list[str]:
    lines = [f"job_id={job.id}", f"status={job.status}"]
    production_id = getattr(job, "production_id", None)
    segment_id = getattr(job, "segment_id", None)
    if production_id:
        lines.append(f"production_id={production_id}")
    if segment_id:
        lines.append(f"segment_id={segment_id}")
    character_reference = (job.request_data or {}).get("character_reference_asset_id")
    if character_reference:
        lines.append(f"character_reference_asset_id={character_reference}")
    if job.provider_task_id:
        lines.append(f"provider_task_id={job.provider_task_id}")
    if job.sandbox_job_id:
        lines.append(f"sandbox_job_id={job.sandbox_job_id}")
    if queue_position is not None:
        lines.append(f"queue_position={queue_position}")
    if retry_after is not None and job.status not in _SEGMENT_TERMINAL | _RENDER_TERMINAL:
        lines.append(f"retry_after_seconds={retry_after}")
    if asset and asset.status == "ready":
        from project.workspace import asset_sandbox_path

        lines.extend(
            [
                f"asset_id={asset.id}",
                f"name={asset.name}",
                "path=" + asset_sandbox_path(
                    asset.user_id,
                    asset.project_id,
                    asset.name,
                    asset_id=asset.id,
                ),
                f"bytes={asset.size}",
            ]
        )
    if job.error:
        lines.append(f"error={job.error}")
    if getattr(job, "kind", None) == "segment" and job.status == "completed" and production_id and segment_id:
        lines.extend(
            [
                f"transcription_idempotency_key={production_id}:{segment_id}:stt",
                "next_action=video_transcribe.submit",
                (
                    "instruction=generation is terminal; do not call video_generate again; "
                    "submit video_transcribe for this segment, then wait on the returned "
                    "transcription job_id"
                ),
            ]
        )
    return lines


def _job_snapshot_version(job) -> int:
    """Return an opaque monotonic-enough version for a persisted job snapshot."""
    updated_at = getattr(job, "updated_at", None)
    if not updated_at:
        return 0
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return max(0, int(updated_at.timestamp() * 1_000_000))


def _stale_finalization_needs_provider(job) -> bool:
    """Whether local OSS recovery has to fall back to another provider probe."""
    if job.status != "finalizing" or _SEGMENT_FINALIZATION_TASKS.get(job.id) is not None:
        return False
    updated_at = getattr(job, "updated_at", None)
    if not updated_at:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (
        datetime.now(timezone.utc) - updated_at
    ).total_seconds() > _SEGMENT_FINALIZATION_STALE_SECONDS


async def _provider_route_blocked_result(
    job,
    *,
    reason: str = "provider_route_changed_since_submission",
) -> ToolResult:
    """Return a non-pollable snapshot when a task's owning route is unavailable."""
    asset = await _job_asset(job)
    version = _job_snapshot_version(job)
    lines = _job_lines(job, asset, retry_after=None)
    lines.extend(
        [
            f"version={version}",
            "recovery_blocked=true",
            "provider_state_unknown=true",
            "still_running=false",
            f"recovery_reason={reason}",
            (
                "instruction=do_not_resubmit; do not repeat status, wait, or cancel; "
                "restore the original provider route or obtain operator review"
            ),
        ]
    )
    return ToolResult(
        title="Video generation recovery blocked",
        output="\n".join(lines),
        metadata={
            "job_id": job.id,
            "status": job.status,
            "asset_id": asset.id if asset and asset.status == "ready" else None,
            "attached": False,
            "ambiguous_submit": False,
            "recovery_blocked": True,
            "provider_state_unknown": True,
            "do_not_resubmit": True,
            "recovery_reason": reason,
            # This is a control-plane instruction, not a claim that the
            # provider task reached a terminal state.
            "still_running": False,
            "timed_out": False,
            "version": version,
        },
    )


async def _provider_outcome_unknown_result(
    job,
    *,
    title: str = "Video provider outcome needs operator review",
    failure_code: str = "video_effect_outcome_unknown",
) -> ToolResult:
    """Expose the durable recovery identity without inviting a paid retry."""
    asset = await _job_asset(job)
    lines = _job_lines(job, asset, retry_after=None)
    lines.extend(
        [
            "recovery=outcome_unknown",
            "instruction=do_not_resubmit; retain this job_id for operator review or explicit revision",
        ]
    )
    return ToolResult(
        title=title,
        output="\n".join(lines),
        metadata={
            "error": True,
            "failure_code": failure_code,
            "job_id": job.id,
            "status": job.status,
            "outcome_unknown": True,
            "manual_review": True,
            "do_not_retry": True,
        },
    )


def _is_timeout_error(exc: Exception) -> bool:
    """Whether a provider probe exhausted time rather than failed semantically."""
    if isinstance(exc, TimeoutError):
        return True
    try:
        import httpx

        return isinstance(exc, httpx.TimeoutException)
    except ImportError:  # pragma: no cover - httpx is a runtime dependency
        return False


def _forget_segment_finalization(job_id: str, task: asyncio.Task[Any]) -> None:
    if _SEGMENT_FINALIZATION_TASKS.get(job_id) is task:
        _SEGMENT_FINALIZATION_TASKS.pop(job_id, None)
    if task.cancelled():
        log.warning("video finalization task was cancelled for %s", job_id)
        return
    exc = task.exception()
    if exc is not None:
        log.warning(
            "video finalization task failed for %s: %s",
            job_id,
            type(exc).__name__,
        )


async def _call_finalize_segment(job, data, ctx, settings, target):
    """Invoke the finalizer with fencing while preserving extension test doubles."""
    import inspect

    kwargs = {}
    if "persist_guard" in inspect.signature(_finalize_segment).parameters:
        kwargs["persist_guard"] = _ctx_persist_guard(ctx)
    return await _finalize_segment(job, data, ctx, settings, target, **kwargs)


def _start_segment_finalization(job, data, ctx, settings, target) -> asyncio.Task[Any]:
    """Keep OSS finalization alive when a bounded wait or client is cancelled."""
    existing = _SEGMENT_FINALIZATION_TASKS.get(job.id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(
        _call_finalize_segment(job, data, ctx, settings, target),
        name=f"video-finalize:{job.id}",
    )
    _SEGMENT_FINALIZATION_TASKS[job.id] = task
    task.add_done_callback(
        lambda done, job_id=job.id: _forget_segment_finalization(job_id, done)
    )
    return task


async def _job_asset(job):
    if not job or not job.output_asset_id:
        return None
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    async with get_db_session() as db:
        return (
            await db.execute(
                select(FileAsset).where(
                    FileAsset.id == job.output_asset_id,
                    FileAsset.user_id == job.user_id,
                )
            )
        ).scalar_one_or_none()


async def _input_content_digests(inputs: list[Any], oss) -> list[dict[str, Any]] | None:
    """Content identity ("etag:size") per input, or None to skip dedupe.

    Any input without an ETag disables dedupe for the whole request: a miss
    only costs a paid call we would have made anyway, a false hit ships the
    wrong video.
    """
    digests: list[dict[str, Any]] = []
    for row in inputs:
        try:
            head = await oss.head(row.oss_key)
        except Exception:
            return None
        etag = (head or {}).get("etag") or ""
        if not etag:
            return None
        digests.append(
            {
                "digest": f"{etag}:{(head or {}).get('size') or 0}",
                "kind": "image" if row.mime.startswith(_IMAGE_PREFIX) else "video",
            }
        )
    return digests


async def _find_reusable_segment(prompt_hash: str) -> tuple[Any, Any] | None:
    """Newest completed job with identical content, any user (cross-user reuse)."""
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        row = (
            await db.execute(
                select(VideoJob, FileAsset)
                .join(FileAsset, FileAsset.id == VideoJob.output_asset_id)
                .where(
                    VideoJob.prompt_hash == prompt_hash,
                    VideoJob.kind == "segment",
                    VideoJob.status == "completed",
                    VideoJob.output_asset_id.is_not(None),
                    FileAsset.status == "ready",
                    FileAsset.is_deleted.is_(False),
                )
                .order_by(VideoJob.completed_at.desc())
                .limit(1)
            )
        ).first()
    return (row[0], row[1]) if row else None


async def _complete_from_reuse(job, source_job, source_asset, ctx: ToolContext) -> Any | None:
    """Fulfil a reserved job by OSS server-side copy instead of a paid call.

    The new user gets their own FileAsset row and OSS key — asset rows and
    keys are never shared across users (a later soft-delete removes the OSS
    object). Returns the completed job, or None so the caller falls back to
    the normal paid path.
    """
    from core.oss import get_oss
    from tool.video_workflow import mark_segment_job

    await _assert_ctx_current(ctx)
    asset = await _job_asset(job)
    if not asset:
        return None
    oss = get_oss()
    copy_response_unknown = False
    try:
        head = await oss.copy(source_asset.oss_key, asset.oss_key)
    except Exception:
        # COPY may have committed even when its response was lost.
        copy_response_unknown = True
        try:
            head = await oss.head(asset.oss_key)
        except Exception:
            head = None
    if head is None:
        copy_response_unknown = True
        try:
            head = await oss.head(asset.oss_key)
        except Exception:
            head = None
    expected_size = int(getattr(source_asset, "size", 0) or 0)
    if (
        not head
        or int(head.get("size") or 0) <= 0
        or (expected_size > 0 and int(head.get("size") or 0) != expected_size)
    ):
        return None
    if copy_response_unknown:
        # Size is always available from the durable source asset. Compare ETag
        # as an additional digest when the OSS exposes one on both objects.
        try:
            source_head = await oss.head(source_asset.oss_key)
        except Exception:
            source_head = None
        source_etag = str((source_head or {}).get("etag") or "").strip('"')
        dest_etag = str(head.get("etag") or "").strip('"')
        if source_etag and dest_etag and source_etag != dest_etag:
            return None
    await _assert_ctx_current(ctx)
    now = datetime.now(timezone.utc)
    settled = await _settle_job_and_asset(
        job,
        ctx,
        job_status="completed",
        asset_status="ready",
        error=None,
        expected_statuses={job.status},
        # Audit only: the source identifiers stay in result_data and are never
        # printed in tool output (they can belong to another user).
        result_data={"reuse": True, "reused_from_job": source_job.id, "bytes": head["size"]},
        size=int(head["size"]),
    )
    if not settled:
        return None
    await _update_job(
        job.id,
        ctx=ctx,
        expected_statuses={"completed"},
        attempt=1,
        started_at=now,
        completed_at=now,
    )
    if job.segment_id:
        await _assert_ctx_current(ctx)
        await mark_segment_job(
            job.segment_id,
            job.id,
            user_id=ctx.user_id,
            status="completed",
            output_asset_id=job.output_asset_id,
        )
    return await _owned_job(job.id, ctx, "segment")


async def execute_generate(args: VideoGenerateArgs, ctx: ToolContext) -> ToolResult:
    if args.action == "submit":
        try:
            target, settings = _configured_target(None)
        except Exception as exc:
            return ToolResult(
                title="Video generation is not configured",
                output=_public_error(exc),
            )
        try:
            from tool.video_workflow import (
                consume_spend_approval,
                content_hash,
                mark_segment_job,
                prepare_segment_submission,
            )

            approved = await prepare_segment_submission(
                ctx, args.production_id or "", args.segment_id or ""
            )
            expected_key = f"{approved['production_id']}:{approved['segment_id']}:generate"
            if args.idempotency_key != expected_key:
                raise RuntimeError(
                    f"idempotency_key must be the approved segment key: {expected_key}"
                )
            prompt = approved["prompt"]
            resolution = approved["resolution"]
            ratio = approved["ratio"]
            duration = approved["duration"]
            generate_audio = approved["generate_audio"]
            watermark = approved["watermark"]
            if approved.get("model"):
                # Per-segment model override routes to its own channel.
                target, settings = _configured_target(approved["model"])
            if ratio not in _RATIOS:
                raise RuntimeError(f"unsupported ratio: {ratio}")
            inputs, character_reference = await _resolve_generation_inputs(
                approved["character_reference_asset"],
                approved["input_assets"],
                ctx,
            )
            from core.oss import get_oss
            from tool import video_providers

            from core.config import get_config as _get_config

            video_providers.validate_request(
                target,
                resolution=resolution,
                ratio=ratio,
                duration=duration,
                generate_audio=generate_audio,
                input_mimes=[row.mime for row in inputs],
                declared=video_providers.declared_model(target.model, _get_config()),
            )
            character_reference_type = (
                approved.get("character_reference_type") or "virtual"
            )
            channel = getattr(target, "channel", "ark")
            provider_content: list[dict[str, Any]] = []
            material_bindings: list[dict[str, Any]] = []
            if channel == "ark":
                # The relay/material-library flows (verified real persons, AIGC
                # groups) apply only to the ark wire; gateway channels receive
                # short-lived OSS URLs directly in build_payload below.
                if target.wire_format == "bossip_videos":
                    provider_content, material_bindings = _relay_provider_inputs(
                        inputs,
                        character_reference,
                        character_reference_type=character_reference_type,
                        input_url_ttl_seconds=settings.provider_input_url_ttl_seconds,
                    )
                else:
                    provider_content, material_bindings = await _materialize_provider_inputs(
                        inputs,
                        character_reference,
                        character_reference_type=character_reference_type,
                        character_identity_id=approved.get("character_identity_id"),
                        ctx=ctx,
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
                "provider_route_fingerprint": (
                    video_providers.provider_route_fingerprint(target)
                ),
                "resolution": resolution,
                "ratio": ratio,
                "duration": duration,
                "generate_audio": generate_audio,
                "watermark": watermark,
            }
            # The route fingerprint is recovery metadata, not a logical input.
            # Excluding it preserves the pre-fingerprint idempotency hash during
            # rolling upgrades and credential rotation; an existing job is
            # reused, then its status/cancel path applies the route guard.
            logical_request_data = {
                key: value
                for key, value in request_data.items()
                if key != "provider_route_fingerprint"
            }
            request_hash = content_hash(
                {
                    "kind": "segment",
                    "model": target.model,
                    "prompt": prompt,
                    "request_data": logical_request_data,
                }
            )
            prompt_hash = None
            if getattr(settings, "dedupe", False):
                input_digests = await _input_content_digests(inputs, get_oss())
                if input_digests is not None:
                    prompt_hash = video_providers.compute_prompt_hash(
                        prompt=prompt,
                        model_type=getattr(target, "model_type", "seedance"),
                        model_name=target.model,
                        duration=duration,
                        ratio=ratio,
                        resolution=resolution,
                        inputs=input_digests,
                        extra_params={
                            "generate_audio": generate_audio,
                            "watermark": watermark,
                        },
                        character_reference_type=character_reference_type,
                        character_identity_id=approved.get("character_identity_id"),
                    )
            job, asset, created = await _create_pending_job(
                ctx=ctx,
                kind="segment",
                idempotency_key=args.idempotency_key or "",
                model=target.model,
                prompt=prompt,
                request_data=request_data,
                filename=None,
                request_hash=request_hash,
                production_id=approved["production_id"],
                segment_id=approved["segment_id"],
                prompt_hash=prompt_hash,
            )
            if not created:
                if job.status == "completed":
                    await _assert_ctx_current(ctx)
                    await mark_segment_job(
                        approved["segment_id"],
                        job.id,
                        user_id=ctx.user_id,
                        status="completed",
                        output_asset_id=job.output_asset_id,
                    )
                    await _attach_completed(job, ctx)
                lines = _job_lines(job, asset)
                ambiguous_submit = job.status == "submitting" and not job.provider_task_id
                if ambiguous_submit:
                    lines.extend(
                        [
                            "recovery=ambiguous_submit_without_provider_task_id",
                            "instruction=do_not_resubmit; cancel or obtain operator review before creating a revision",
                        ]
                    )
                metadata = {
                    "job_id": job.id,
                    "status": job.status,
                    "idempotent_reuse": True,
                    "ambiguous_submit": ambiguous_submit,
                }
                if ambiguous_submit or job.status == "outcome_unknown":
                    metadata.update(
                        error=True,
                        failure_code="video_submit_outcome_unknown",
                        outcome_unknown=True,
                        manual_review=True,
                        do_not_retry=True,
                    )
                elif job.status in {"failed", "cancelled"}:
                    metadata.update(
                        error=True,
                        failure_code=f"video_provider_{job.status}",
                    )
                return ToolResult(
                    title=(
                        "Existing video submission needs operator review"
                        if ambiguous_submit
                        else "Existing video generation job"
                    ),
                    output="\n".join(lines),
                    metadata=metadata,
                )

            if prompt_hash:
                reusable = await _find_reusable_segment(prompt_hash)
                if reusable:
                    source_job, source_asset = reusable
                    reused_job = await _complete_from_reuse(job, source_job, source_asset, ctx)
                    if reused_job is not None:
                        # No paid call happened, so the spend approval is NOT
                        # consumed. Source identifiers stay out of the output.
                        await _attach_completed(reused_job, ctx)
                        reused_asset = await _job_asset(reused_job)
                        return ToolResult(
                            title="Video segment ready (reused identical generation)",
                            output="\n".join(_job_lines(reused_job, reused_asset)),
                            metadata={
                                "job_id": reused_job.id,
                                "status": reused_job.status,
                                "asset_id": (
                                    reused_asset.id
                                    if reused_asset and reused_asset.status == "ready"
                                    else None
                                ),
                                "reused": True,
                            },
                        )

            if channel == "ark":
                content: list[dict[str, Any]] = [
                    {"type": "text", "text": prompt},
                    *provider_content,
                ]
                payload: dict[str, Any] = {
                    "model": target.model,
                    "content": content,
                    "resolution": resolution,
                    "ratio": ratio,
                    "duration": duration,
                    "generate_audio": generate_audio,
                    "watermark": watermark,
                }
                submit_path = None
            else:
                oss = get_oss()
                refs = [
                    {
                        "kind": "image" if row.mime.startswith(_IMAGE_PREFIX) else "video",
                        "url": oss.presign_get(
                            row.oss_key, expires_sec=settings.provider_input_url_ttl_seconds
                        ),
                        "role": (
                            "reference_image"
                            if row.mime.startswith(_IMAGE_PREFIX)
                            else "reference_video"
                        ),
                    }
                    for row in inputs
                ]
                submit_path, payload = video_providers.build_payload(
                    target,
                    prompt=prompt,
                    refs=refs,
                    resolution=resolution,
                    ratio=ratio,
                    duration=duration,
                    generate_audio=generate_audio,
                    watermark=watermark,
                )
            async def submit_and_persist_provider_identity():
                await _assert_ctx_current(ctx)
                try:
                    await consume_spend_approval(approved["spend_approval_id"])
                except Exception:
                    await _update_job(
                        job.id,
                        ctx=ctx,
                        expected_statuses={"submitting"},
                        status="failed",
                        error="approved generation call limit is unavailable",
                        completed_at=datetime.now(timezone.utc),
                    )
                    await _mark_asset(job.output_asset_id, status="failed", ctx=ctx)
                    raise
                await mark_segment_job(
                    approved["segment_id"],
                    job.id,
                    user_id=ctx.user_id,
                    status="submitting",
                )
                await _assert_ctx_current(ctx)
                if submit_path is None:
                    submitted = await _provider_submit(target, payload)
                else:
                    submitted = await video_providers.submit(target, submit_path, payload)
                    if getattr(target, "channel", "ark") == "task":
                        submitted = {
                            **submitted,
                            **(submitted.get("data") if isinstance(submitted.get("data"), dict) else {}),
                        }
                submitted_state = _provider_state(submitted, target)
                # A provider may return a terminal state from the initial POST. In
                # our state machine, "completed" means the output is already safe
                # in OSS, so retain an in-progress state until finalization wins
                # the database claim and completes that transfer.
                stored_state = "in_progress" if submitted_state == "completed" else submitted_state
                # Provider receipt persistence is a job-local CAS and remains
                # allowed if the Agent lease turns over after the paid POST.
                task_id = video_providers.extract_task_id(target, submitted)
                if not task_id:
                    raise RuntimeError("video provider response did not include a durable task id")
                # Keep the ordinary transition on the long-standing update
                # seam used by callers/tests. A real update returns ``False``
                # only when another owner changed the row after the POST; in
                # that case, fill just the immutable receipt without changing
                # the newer state. (Legacy adapters may return ``None`` after
                # applying the update, so only literal ``False`` falls back.)
                advanced = await _update_job(
                    job.id,
                    expected_statuses={"submitting"},
                    expected_provider_task_id=(getattr(job, "provider_task_id", None) or None),
                    provider_task_id=task_id,
                    status=stored_state,
                    attempt=1,
                    started_at=datetime.now(timezone.utc),
                    error=None,
                )
                if advanced is False:
                    await _persist_video_submit_receipt(
                        job,
                        user_id=ctx.user_id,
                        task_id=task_id,
                        stored_state=stored_state,
                    )
                return submitted, submitted_state

            try:
                await ctx.update_output(f"Submitting the asynchronous {target.model} video task…")
            except asyncio.CancelledError:
                # The paid operation below still needs an auditable terminal or
                # provider-owned identity even when the chat turn is interrupted.
                log.info("video generation output update interrupted; securing submit state")
            critical_submit = asyncio.create_task(submit_and_persist_provider_identity())
            try:
                response, state = await asyncio.shield(critical_submit)
            except asyncio.CancelledError:
                log.info("video generation turn interrupted; waiting to persist provider task identity")
                response, state = await critical_submit
            job = await _owned_job(job.id, ctx, "segment")
            if state == "completed":
                await ctx.update_output("Provider completed; copying the video to OSS…")
                job = await _call_finalize_segment(
                    job, response, ctx, settings, target
                )
                asset = await _job_asset(job)
                await _attach_completed(job, ctx)
            elif state in {"failed", "cancelled"}:
                detail = response.get("error")
                message = detail.get("message") if isinstance(detail, dict) else str(detail or state)
                await _update_job(
                    job.id,
                    ctx=ctx,
                    expected_statuses={job.status},
                    error=message[:1000],
                    completed_at=datetime.now(timezone.utc),
                )
                await _mark_asset(job.output_asset_id, status="failed", ctx=ctx)
                await _assert_ctx_current(ctx)
                await mark_segment_job(
                    approved["segment_id"],
                    job.id,
                    user_id=ctx.user_id,
                    status=state,
                )
                job = await _owned_job(job.id, ctx, "segment")
            metadata = {
                "job_id": job.id,
                "status": job.status,
                "asset_id": asset.id if asset and asset.status == "ready" else None,
                "retry_after_seconds": 5,
            }
            if job.status == "outcome_unknown":
                metadata.update(
                    error=True,
                    failure_code="video_submit_outcome_unknown",
                    outcome_unknown=True,
                    manual_review=True,
                    do_not_retry=True,
                )
            elif job.status in {"failed", "cancelled"}:
                metadata.update(
                    error=True,
                    failure_code=f"video_provider_{job.status}",
                )
            return ToolResult(
                title=("Video segment ready" if job.status == "completed" else "Video generation submitted"),
                output="\n".join(_job_lines(job, asset)),
                metadata=metadata,
            )
        except Exception as exc:
            from video.materials import RealPersonAuthorizationRequired
            from agent.driver import LeaseLostError

            if isinstance(exc, LeaseLostError):
                raise

            if isinstance(exc, RealPersonAuthorizationRequired):
                return ToolResult(
                    title="真人授权后才能继续",
                    output=_public_error(exc),
                    metadata={
                        "error": True,
                        "failure_code": "real_person_authorization_required",
                        "authorization_required": True,
                    },
                )
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
                    if current and current.status in {"submitting", "queued", "in_progress"}:
                        await _update_job(
                            job.id,
                            expected_statuses={current.status},
                            expected_provider_task_id=(current.provider_task_id or None),
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
                current = await _owned_job(job.id, ctx, "segment")
                if current and current.status not in _SEGMENT_TERMINAL:
                    quarantined = await _update_job(
                        job.id,
                        expected_statuses={current.status},
                        status="outcome_unknown",
                        error=(
                            "Submission outcome may be ambiguous; automatic paid resubmission is disabled. "
                            + _public_error(exc)
                        ),
                    )
                    if quarantined and current.segment_id:
                        from tool.video_workflow import mark_segment_job

                        await _assert_ctx_current(ctx)
                        await mark_segment_job(
                            current.segment_id,
                            current.id,
                            user_id=ctx.user_id,
                            status="outcome_unknown",
                        )
            if "job" in locals():
                current = await _owned_job(job.id, ctx, "segment")
                if current and current.status == "outcome_unknown":
                    return await _provider_outcome_unknown_result(
                        current,
                        title="Video generation submit needs operator review",
                        failure_code="video_submit_outcome_unknown",
                    )
            return _failure_result(
                "Video generation submit failed",
                exc,
                failure_code="video_submit_outcome_unknown",
                **(
                    {
                        "job_id": job.id,
                        "outcome_unknown": True,
                        "manual_review": True,
                        "do_not_retry": True,
                    }
                    if "job" in locals()
                    else {}
                ),
            )

    job = await _owned_job(args.job_id or "", ctx, "segment")
    if not job:
        return ToolResult(title="Video job not found", output="No owned segment job has that job_id.")
    if job.status == "outcome_unknown":
        if job.segment_id:
            from tool.video_workflow import mark_segment_job

            await _assert_ctx_current(ctx)
            await mark_segment_job(
                job.segment_id,
                job.id,
                user_id=ctx.user_id,
                status="outcome_unknown",
            )
        return await _provider_outcome_unknown_result(job)
    if args.action == "cancel" and job.status in _SEGMENT_TERMINAL:
        asset = await _job_asset(job)
        return ToolResult(
            title="Video generation already finished",
            output="\n".join(_job_lines(job, asset)),
            metadata={"job_id": job.id, "status": job.status},
        )
    if args.action == "cancel" and job.status == "finalizing":
        return ToolResult(
            title="Video finalization in progress",
            output="The provider task has completed and its output is being secured in OSS; it can no longer be cancelled.",
            metadata={"job_id": job.id, "status": job.status},
        )
    if args.action == "cancel" and job.status == "submitting" and not job.provider_task_id:
        await _assert_ctx_current(ctx)
        quarantined = await _update_job(
            job.id,
            ctx=ctx,
            expected_statuses={"submitting"},
            expected_provider_task_id=None,
            status="outcome_unknown",
            error=(
                "Cancellation was requested while provider submission may be in flight and no "
                "receipt is durable; no remote DELETE was guessed. Manual review is required."
            ),
        )
        if quarantined:
            if job.segment_id:
                from tool.video_workflow import mark_segment_job

                await _assert_ctx_current(ctx)
                await mark_segment_job(
                    job.segment_id,
                    job.id,
                    user_id=ctx.user_id,
                    status="outcome_unknown",
                )
            return _failure_result(
                "Video cancellation needs operator review",
                "The provider submit/cancel outcome cannot be proven without a durable task receipt.",
                failure_code="video_cancel_without_receipt",
                job_id=job.id,
                status="outcome_unknown",
                outcome_unknown=True,
                manual_review=True,
                do_not_retry=True,
            )
        job = await _owned_job(job.id, ctx, "segment")
        if not job:
            return _failure_result(
                "Video cancellation not applied",
                "The video job changed before cancellation could be claimed.",
                failure_code="video_cancel_state_changed",
            )
        if job.status not in {"queued", "in_progress"} or not job.provider_task_id:
            return _failure_result(
                "Video cancellation not applied",
                "The video job changed while its provider receipt was being reconciled.",
                failure_code="video_cancel_state_changed",
                job_id=job.id,
                status=job.status,
            )
    if args.action == "cancel" and job.status == "outcome_unknown":
        return _failure_result(
            "Video cancellation needs operator review",
            "A previous cancellation request has an unknown provider outcome; it will not be sent again automatically.",
            failure_code="video_cancel_outcome_unknown",
            job_id=job.id,
            status=job.status,
            outcome_unknown=True,
            manual_review=True,
            do_not_retry=True,
        )

    provider_route_required = job.status not in _SEGMENT_TERMINAL | {"finalizing"}
    recover_stale_finalization = (
        args.action != "cancel" and _stale_finalization_needs_provider(job)
    )
    if recover_stale_finalization:
        provider_route_required = True
    target = None
    settings = None
    route_block_reason = None
    try:
        # Controls always resolve from the persisted model. The deployment's
        # current default is not evidence of the route that owns this task.
        target, settings = _configured_target(job.model or None)
    except Exception:
        route_block_reason = "provider_route_unavailable"
    if job.provider_task_id and target is not None:
        from tool.video_providers import provider_route_mismatch

        if provider_route_mismatch(job.request_data, target):
            route_snapshot = (
                job.request_data if isinstance(job.request_data, dict) else {}
            )
            stored_fingerprint = route_snapshot.get("provider_route_fingerprint")
            if "provider_route_fingerprint" not in route_snapshot:
                route_block_reason = "legacy_provider_route_unverifiable"
            elif not isinstance(stored_fingerprint, str) or not stored_fingerprint.strip():
                route_block_reason = "provider_route_identity_invalid"
            else:
                route_block_reason = "provider_route_changed_since_submission"
    if job.provider_task_id and provider_route_required and route_block_reason:
        # The current endpoint/account has no authority to answer for this paid
        # task. Leave the durable job untouched for its original route.
        return await _provider_route_blocked_result(job, reason=route_block_reason)
    poll_interval_seconds = float(getattr(settings, "poll_interval_seconds", 5))
    if args.action == "cancel":
        await _assert_ctx_current(ctx)
        claimed = await _update_job(
            job.id,
            ctx=ctx,
            expected_statuses={job.status},
            expected_provider_task_id=(job.provider_task_id or None),
            status="cancelling",
            error=None,
        )
        if not claimed:
            current = await _owned_job(job.id, ctx, "segment")
            return _failure_result(
                "Video cancellation not applied",
                "The video job changed before cancellation could be claimed.",
                failure_code="video_cancel_state_changed",
                job_id=job.id,
                status=getattr(current, "status", "unknown"),
            )
        if job.provider_task_id and job.status != "transfer_failed":
            try:
                # This is the last possible ownership check before the
                # irreversible third-party DELETE.
                await _assert_ctx_current(ctx)
                await _provider_cancel(target, job.provider_task_id)
            except Exception as exc:
                from agent.driver import LeaseLostError

                if isinstance(exc, LeaseLostError):
                    raise
                quarantined = await _update_job(
                    job.id,
                    ctx=ctx,
                    expected_statuses={"cancelling"},
                    expected_provider_task_id=job.provider_task_id,
                    status="outcome_unknown",
                    error="Provider cancellation outcome is unknown; operator reconciliation required.",
                )
                if quarantined and job.segment_id:
                    from tool.video_workflow import mark_segment_job

                    await _assert_ctx_current(ctx)
                    await mark_segment_job(
                        job.segment_id,
                        job.id,
                        user_id=ctx.user_id,
                        status="outcome_unknown",
                    )
                return _failure_result(
                    "Video cancellation outcome unknown",
                    exc,
                    failure_code="video_cancel_outcome_unknown",
                    job_id=job.id,
                    status="outcome_unknown",
                    outcome_unknown=True,
                    manual_review=True,
                    do_not_retry=True,
                )
        cancel_note = (
            "cancelled locally; the gateway channel has no upstream cancel and the provider task may still complete"
            if getattr(target, "channel", "ark") != "ark"
            else "cancelled"
        )
        await _assert_ctx_current(ctx)
        settled = await _settle_job_and_asset(
            job,
            ctx,
            job_status="cancelled",
            asset_status="failed",
            error=cancel_note,
            expected_statuses={"cancelling"},
        )
        if not settled:
            return _failure_result(
                "Video cancellation settlement lost",
                "The provider request returned, but the current job state no longer accepts this cancellation receipt.",
                failure_code="video_cancel_settlement_conflict",
                job_id=job.id,
                outcome_unknown=True,
                manual_review=True,
            )
        if job.segment_id:
            from tool.video_workflow import mark_segment_job

            await _assert_ctx_current(ctx)
            await mark_segment_job(
                job.segment_id,
                job.id,
                user_id=ctx.user_id,
                status="cancelled",
            )
        job = await _owned_job(job.id, ctx, "segment")
        return ToolResult(title="Video generation cancelled", output="\n".join(_job_lines(job)))

    is_wait = args.action == "wait"
    deadline = asyncio.get_running_loop().time() + (args.wait_seconds if is_wait else 0)
    version = _job_snapshot_version(job)
    timed_out = False
    while job.status not in _SEGMENT_TERMINAL:
        if is_wait and args.after_version and version > args.after_version:
            break
        if job.status == "finalizing":
            finalization = _SEGMENT_FINALIZATION_TASKS.get(job.id)
            # This decision is frozen before the route guard. Do not promote a
            # previously young/tracked finalization to provider recovery later
            # in the same call: the clock may cross the threshold, or a task's
            # done callback may remove it, after the guard has already passed.
            if recover_stale_finalization:
                await _update_job(
                    job.id,
                    ctx=ctx,
                    expected_statuses={"finalizing"},
                    status="transfer_failed",
                    error="recovering a stale OSS finalization",
                )
                job = await _owned_job(job.id, ctx, "segment")
                version = _job_snapshot_version(job)
                continue
            if not is_wait:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                timed_out = True
                break
            await ctx.update_output("Provider completed; securing the video in OSS…")
            try:
                if finalization is not None:
                    job = await asyncio.wait_for(
                        asyncio.shield(finalization), timeout=remaining
                    )
                else:
                    await asyncio.sleep(min(poll_interval_seconds, remaining))
                    job = await _owned_job(job.id, ctx, "segment")
            except Exception as exc:
                if _is_timeout_error(exc):
                    timed_out = True
                    job = await _owned_job(job.id, ctx, "segment")
                    break
                return _failure_result(
                    "Video status check failed",
                    exc,
                    failure_code="video_status_failed",
                    job_id=job.id,
                )
            version = _job_snapshot_version(job)
            continue
        if not job.provider_task_id:
            break
        if route_block_reason:
            # A locally tracked finalization may legitimately be allowed to run
            # despite a route mismatch, then return transfer_failed. Re-check
            # the frozen route verdict immediately next to the provider probe
            # so that state transition cannot bypass the initial guard.
            return await _provider_route_blocked_result(
                job,
                reason=route_block_reason,
            )
        try:
            if is_wait:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    timed_out = True
                    break
                data = await asyncio.wait_for(
                    _provider_status(target, job.provider_task_id),
                    timeout=remaining,
                )
            else:
                data = await _provider_status(target, job.provider_task_id)
            state = _provider_state(data, target)
            if state == "completed":
                await ctx.update_output("Provider completed; copying the video to OSS…")
                finalization = _start_segment_finalization(
                    job, data, ctx, settings, target
                )
                if is_wait:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        timed_out = True
                        job = await _owned_job(job.id, ctx, "segment")
                        break
                    job = await asyncio.wait_for(
                        asyncio.shield(finalization), timeout=remaining
                    )
                else:
                    job = await asyncio.shield(finalization)
            elif state in {"failed", "cancelled"}:
                from tool.video_providers import failure_detail

                detail = data.get("error")
                message = (
                    detail.get("message")
                    if isinstance(detail, dict)
                    else (failure_detail(target, data) or str(detail or state))
                )
                settled = await _settle_job_and_asset(
                    job,
                    ctx,
                    job_status=state,
                    asset_status="failed",
                    error=message[:1000],
                    expected_statuses={job.status},
                )
                if settled and job.segment_id:
                    from tool.video_workflow import mark_segment_job

                    await _assert_ctx_current(ctx)
                    await mark_segment_job(
                        job.segment_id,
                        job.id,
                        user_id=ctx.user_id,
                        status=state,
                    )
                job = await _owned_job(job.id, ctx, "segment")
            else:
                if state != job.status or getattr(job, "error", None):
                    await _update_job(
                        job.id,
                        ctx=ctx,
                        expected_statuses={job.status},
                        status=state,
                        error=None,
                    )
                    job = await _owned_job(job.id, ctx, "segment")
            version = _job_snapshot_version(job)
        except Exception as exc:
            if is_wait and _is_timeout_error(exc):
                timed_out = True
                job = await _owned_job(job.id, ctx, "segment")
                break
            return _failure_result(
                "Video status check failed",
                exc,
                failure_code="video_status_failed",
                job_id=job.id,
            )
        if not is_wait or job.status in _SEGMENT_TERMINAL:
            break
        if args.after_version and version > args.after_version:
            break
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            timed_out = True
            break
        await ctx.update_output(f"Video is {job.status}; waiting for the provider…")
        await asyncio.sleep(min(poll_interval_seconds, remaining))

    asset = await _job_asset(job)
    if job.status == "completed":
        attached = await _attach_completed(job, ctx)
        job = await _owned_job(job.id, ctx, "segment")
        title = "Video segment ready"
    else:
        attached = False
        title = (
            "Video submission needs operator review"
            if job.status == "submitting" and not job.provider_task_id
            else "Video generation status"
        )
    lines = _job_lines(job, asset, retry_after=round(poll_interval_seconds))
    version = _job_snapshot_version(job)
    still_running = job.status not in _SEGMENT_TERMINAL
    lines.append(f"version={version}")
    if still_running:
        lines.extend(
            [
                "still_running=true",
                (
                    f"next_wait_after_version={version} "
                    f"next_wait_iteration={args.wait_iteration + 1}"
                ),
            ]
        )
    ambiguous_submit = job.status == "submitting" and not job.provider_task_id
    if ambiguous_submit:
        lines.extend(
            [
                "recovery=ambiguous_submit_without_provider_task_id",
                "instruction=do_not_resubmit; cancel or obtain operator review before creating a revision",
            ]
        )
    metadata = {
        "job_id": job.id,
        "status": job.status,
        "asset_id": asset.id if asset and asset.status == "ready" else None,
        "attached": attached,
        "ambiguous_submit": ambiguous_submit,
        "still_running": still_running,
        "timed_out": timed_out,
        "version": version,
        "retry_after_seconds": round(poll_interval_seconds),
    }
    if ambiguous_submit:
        metadata.update(
            error=True,
            failure_code="video_submit_outcome_unknown",
            outcome_unknown=True,
            manual_review=True,
            do_not_retry=True,
        )
    elif job.status in {"failed", "cancelled"}:
        metadata.update(
            error=True,
            failure_code=f"video_provider_{job.status}",
        )
    return ToolResult(title=title, output="\n".join(lines), metadata=metadata)


async def _transcription_payload(job, ctx: ToolContext, video_settings, oss) -> dict[str, Any]:
    from sandbox.assets import _use_internal_oss

    source_id = str((job.request_data or {}).get("source_asset_id") or "")
    rows = await _resolve_inputs([source_id], ctx)
    source = rows[0]
    if source.mime not in _VIDEO_MIMES:
        raise RuntimeError("transcription source must be a video asset")
    output = await _job_asset(job)
    if not output:
        raise RuntimeError("reserved transcription audio asset is missing")
    ttl = video_settings.render_url_ttl_seconds
    internal = _use_internal_oss(oss)
    return {
        "operation": "extract_audio",
        "job_id": job.id,
        "owner": ctx.user_id,
        "session_id": ctx.session_id,
        "idempotency_key": job.idempotency_key,
        "inputs": [
            {
                "name": source.name,
                "mime": source.mime,
                "size": source.size,
                "cache_key": f"{oss.bucket}:{source.oss_key}:{source.size}",
                "url": oss.presign_get(source.oss_key, expires_sec=ttl, internal=internal),
            }
        ],
        "output": {
            "name": output.name,
            "mime": "audio/mpeg",
            "put_url": oss.presign_put(
                output.oss_key, "audio/mpeg", expires_sec=ttl, internal=internal
            ),
        },
    }


async def _dispatch_transcription(
    job,
    ctx: ToolContext,
    video_settings,
    oss,
    *,
    persist_guard=None,
) -> tuple[Any, dict[str, Any]]:
    payload = await _transcription_payload(job, ctx, video_settings, oss)
    remote = await ctx.sandbox.submit_media_job(payload)
    if persist_guard is not None:
        await persist_guard()
    await _update_job(
        job.id,
        ctx=ctx if persist_guard is not None else None,
        expected_statuses={"dispatching", "dispatch_unknown"},
        sandbox_job_id=remote["job_id"],
        status=remote["status"],
        error=None,
        started_at=datetime.now(timezone.utc) if remote["status"] == "in_progress" else None,
    )
    return await _owned_job(job.id, ctx, "stt"), remote


async def _persist_stt_receipt(
    job,
    *,
    task_id: str,
    operation_key: str,
    extraction_result: dict[str, Any],
) -> None:
    """Persist a provider handle with a job-local CAS, independent of run lease.

    Once DashScope returned its task id, retaining that receipt is safer than
    discarding it when the Agent generation changes.  The immutable VideoJob
    identity/idempotency key scopes this write; it cannot publish chat output
    or settle the asset.
    """
    from sqlalchemy import or_

    from db.base import get_db_session
    from db.models.video_job import VideoJob

    effect = {
        "operation_key": operation_key,
        "provider": "dashscope",
        "state": "receipt_persisted",
        "task_id": task_id,
    }
    async with get_db_session() as db:
        result = await db.execute(
            update(VideoJob)
            .where(
                VideoJob.id == job.id,
                VideoJob.user_id == job.user_id,
                VideoJob.kind == "stt",
                or_(
                    VideoJob.provider_task_id.is_(None),
                    VideoJob.provider_task_id == task_id,
                ),
                VideoJob.status.in_(["transcribing", "extraction_completed"]),
            )
            .values(
                provider_task_id=task_id,
                status="transcribing",
                result_data={"extraction": extraction_result, "stt_effect": effect},
                error=None,
                updated_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            current = (
                await db.execute(
                    select(VideoJob)
                    .where(
                        VideoJob.id == job.id,
                        VideoJob.user_id == job.user_id,
                        VideoJob.kind == "stt",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if not current or current.provider_task_id not in {None, task_id}:
                raise RuntimeError("STT provider receipt conflicts with the durable job")
            if current.provider_task_id == task_id:
                return

            # A recovery worker can quarantine a receipt-less ``transcribing``
            # row while the original POST is returning. The late task id is an
            # immutable provider fact, so retain it and make that specific
            # no-receipt quarantine pollable again without publishing success.
            current_result = (
                dict(current.result_data)
                if isinstance(current.result_data, dict)
                else {}
            )
            current_effect = (
                dict(current_result.get("stt_effect"))
                if isinstance(current_result.get("stt_effect"), dict)
                else {}
            )
            current_effect.update(effect)
            current_result.setdefault("extraction", extraction_result)
            current_result["stt_effect"] = current_effect
            current.provider_task_id = task_id
            current.result_data = current_result
            if current.status == "outcome_unknown":
                current.status = "transcribing"
                current.error = None
            current.updated_at = datetime.now(timezone.utc)


async def _finalize_transcription(
    job,
    ctx: ToolContext,
    target,
    oss,
    extraction_result: dict[str, Any],
    *,
    persist_guard=None,
    durable_recovery: bool = False,
) -> Any:
    from agent.driver import LeaseLostError
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    from tool.video_workflow import record_segment_transcript

    audio = await _job_asset(job)
    head = await oss.head(audio.oss_key) if audio else None
    if persist_guard is not None:
        await persist_guard()
    if not audio or not head:
        await _settle_job_and_asset(
            job,
            ctx,
            job_status="failed",
            asset_status="failed",
            error="sandbox reported audio extraction complete but the OSS audio is missing",
            expected_statuses={job.status},
        )
        return await _owned_job(job.id, ctx, "stt")
    await _mark_asset(
        audio.id,
        status="ready",
        size=head["size"] or 0,
        ctx=ctx if persist_guard is not None else None,
    )
    existing_result = job.result_data if isinstance(job.result_data, dict) else {}
    transcript = (
        existing_result.get("transcript")
        if isinstance(existing_result.get("transcript"), dict)
        else None
    )
    if transcript is None:
        effect = (
            existing_result.get("stt_effect")
            if isinstance(existing_result.get("stt_effect"), dict)
            else {}
        )
        operation_key = str(
            effect.get("operation_key")
            or f"openbox:stt:{job.id}:{job.request_hash[:16]}"
        )
        task_id = str(job.provider_task_id or effect.get("task_id") or "")

        # A legacy/incomplete transcribing row has crossed the irreversible
        # POST boundary without a durable receipt. Never turn age into evidence
        # that a second paid request is safe.
        if job.status == "transcribing" and not task_id:
            await _update_job(
                job.id,
                ctx=ctx if persist_guard is not None else None,
                expected_statuses={"transcribing"},
                status="outcome_unknown",
                result_data={
                    "extraction": extraction_result,
                    "stt_effect": {
                        "operation_key": operation_key,
                        "provider": target.engine,
                        "state": "outcome_unknown",
                    },
                },
                error="STT POST may have been accepted without a durable receipt; manual review required.",
            )
            return await _owned_job(job.id, ctx, "stt")

        now = datetime.now(timezone.utc)
        async with get_db_session() as db:
            await _assert_effect_fence_locked(
                db, ctx if persist_guard is not None else None
            )
            claimed = await db.execute(
                update(VideoJob)
                .where(
                    VideoJob.id == job.id,
                    VideoJob.status.in_([
                        "queued",
                        "in_progress",
                        "extraction_completed",
                        "transcribing",
                    ]),
                )
                .values(
                    status="transcribing",
                    result_data={
                        "extraction": extraction_result,
                        "stt_effect": {
                            "operation_key": operation_key,
                            "provider": target.engine,
                            "state": "receipt_persisted" if task_id else "submitting",
                            **({"task_id": task_id} if task_id else {}),
                        },
                    },
                    error=None,
                    updated_at=now,
                )
            )
            if claimed.rowcount != 1:
                return await _owned_job(job.id, ctx, "stt")
        try:
            await ctx.update_output("Audio extracted; running segment speech-to-text QA…")

            async def persist_receipt(received_task_id: str) -> None:
                await _persist_stt_receipt(
                    job,
                    task_id=received_task_id,
                    operation_key=operation_key,
                    extraction_result=extraction_result,
                )

            transcript = await _provider_transcribe(
                target,
                oss.presign_get(audio.oss_key, expires_sec=1800),
                task_id=task_id or None,
                operation_key=operation_key,
                receipt_callback=persist_receipt,
            )
        except Exception as exc:
            if isinstance(exc, LeaseLostError):
                raise
            if persist_guard is not None:
                await persist_guard()
            # Provider exceptions may embed the presigned audio URL. Keep the
            # correlation at job level without copying credentials into logs.
            log.warning(
                "segment transcription provider failed: %s",
                type(exc).__name__,
            )
            current = await _owned_job(job.id, ctx, "stt")
            receipt = str(getattr(current, "provider_task_id", None) or task_id or "")
            state = "receipt_persisted" if receipt else "outcome_unknown"
            await _update_job(
                job.id,
                ctx=ctx if persist_guard is not None else None,
                expected_statuses={"transcribing"},
                status="transcribing" if receipt else "outcome_unknown",
                result_data={
                    "extraction": extraction_result,
                    "stt_effect": {
                        "operation_key": operation_key,
                        "provider": target.engine,
                        "state": state,
                        **({"task_id": receipt} if receipt else {}),
                    },
                },
                error=(
                    "STT receipt is durable; provider polling can resume without resubmission."
                    if receipt
                    else "STT provider outcome is unknown; operator reconciliation required and automatic resubmission is disabled."
                ),
            )
            return await _owned_job(job.id, ctx, "stt")

        # Persist the provider output before the domain comparison. If the
        # process dies after this point, reconciliation resumes from the
        # transcript instead of making another provider call.
        if persist_guard is not None:
            await persist_guard()
        await _update_job(
            job.id,
            ctx=ctx if persist_guard is not None else None,
            expected_statuses={"transcribing"},
            status="transcript_ready",
            result_data={
                "extraction": extraction_result,
                "transcript": transcript,
                "stt_effect": {
                    "operation_key": operation_key,
                    "provider": target.engine,
                    "state": "response_persisted",
                    **(
                        {"task_id": str(transcript.get("task_id") or task_id)}
                        if transcript.get("task_id") or task_id
                        else {}
                    ),
                },
            },
            error=None,
        )

    try:
        comparison = await record_segment_transcript(
            job.segment_id or "",
            transcript["text"],
            transcript,
            user_id=ctx.user_id,
            threshold=target.similarity_threshold,
            session_id=ctx.session_id,
            run_fence=ctx.run_fence,
        )
        if persist_guard is not None:
            await persist_guard()
        await _update_job(
            job.id,
            ctx=ctx if persist_guard is not None else None,
            expected_statuses={"transcript_ready", "transcribing"},
            status="completed",
            result_data={
                "extraction": extraction_result,
                "transcript": transcript,
                "comparison": comparison,
                "stt_effect": {
                    **(
                        existing_result.get("stt_effect")
                        if isinstance(existing_result.get("stt_effect"), dict)
                        else {}
                    ),
                    "state": "completed",
                },
            },
            error=None,
            completed_at=datetime.now(timezone.utc),
        )
    except LeaseLostError:
        raise
    except Exception as exc:
        if persist_guard is not None:
            await persist_guard()
        log.warning(
            "segment transcription domain commit failed: %s",
            type(exc).__name__,
        )
        if durable_recovery:
            await _update_job(
                job.id,
                ctx=ctx if persist_guard is not None else None,
                expected_statuses={"transcript_ready", "transcribing"},
                status="transcript_ready",
                result_data={"extraction": extraction_result, "transcript": transcript},
                error="Transcript is durable; retrying the local QA/domain commit only.",
            )
        else:
            await _update_job(
                job.id,
                ctx=ctx if persist_guard is not None else None,
                expected_statuses={"transcript_ready", "transcribing"},
                status="failed",
                result_data={"extraction": extraction_result},
                error="STT provider request failed; the extracted audio is retained for an explicit retry.",
                completed_at=datetime.now(timezone.utc),
            )
    return await _owned_job(job.id, ctx, "stt")


def _transcription_lines(job, asset=None, *, remote: dict[str, Any] | None = None) -> list[str]:
    lines = [f"job_id={job.id}", f"status={job.status}"]
    if job.production_id:
        lines.append(f"production_id={job.production_id}")
    if job.segment_id:
        lines.append(f"segment_id={job.segment_id}")
    if remote is not None:
        lines.append(f"version={int(remote.get('version') or 0)}")
        lines.append(f"queue_position={int(remote.get('queue_position') or 0)}")
    result = job.result_data or {}
    transcript = result.get("transcript") if isinstance(result.get("transcript"), dict) else {}
    comparison = result.get("comparison") if isinstance(result.get("comparison"), dict) else {}
    if transcript:
        lines.append(f"transcript={transcript.get('text') or ''}")
    if comparison:
        lines.append(f"similarity={comparison.get('similarity')}")
        lines.append(f"verdict={comparison.get('verdict')}")
        if comparison.get("notes"):
            lines.append("notes=" + json.dumps(comparison["notes"], ensure_ascii=False))
    if asset and asset.status == "ready":
        lines.append(f"audio_asset_id={asset.id}")
    if job.error:
        lines.append(f"error={job.error}")
    if job.status not in {"completed", "failed", "cancelled", "outcome_unknown"}:
        lines.append("retry_after_seconds=5")
    return lines


async def execute_transcribe(args: VideoTranscribeArgs, ctx: ToolContext) -> ToolResult:
    from agent.driver import LeaseLostError

    if not ctx.sandbox:
        return ToolResult(title="Sandbox unavailable", output="Video transcription requires the user's WUYING sandbox.")
    try:
        target = _configured_transcription_target()
        # Settings, not a generation route: transcription must not fail because
        # the deployment's default video model is undeclared.
        from core.config import get_config

        video_settings = get_config().video_generation
        from core.oss import get_oss

        oss = get_oss()
    except Exception as exc:
        return ToolResult(title="Video transcription is not configured", output=_public_error(exc))

    if args.action == "submit":
        try:
            from tool.video_workflow import content_hash, prepare_transcription

            approved = await prepare_transcription(
                ctx, args.production_id or "", args.segment_id or ""
            )
            expected_key = f"{args.production_id}:{args.segment_id}:stt"
            if args.idempotency_key != expected_key:
                raise RuntimeError(f"idempotency_key must be the segment STT key: {expected_key}")
            source = approved["asset"]
            request_data = {
                "production_id": args.production_id,
                "segment_id": args.segment_id,
                "source_asset_id": source.id,
                "source_bytes": source.size,
                "model": target.model,
            }
            request_hash = content_hash({"kind": "stt", "request_data": request_data})
            job, audio, created = await _create_pending_job(
                ctx=ctx,
                kind="stt",
                idempotency_key=args.idempotency_key or "",
                model=target.model,
                prompt=None,
                request_data=request_data,
                filename=f"segment-{approved['segment'].ordinal}-speech.mp3",
                request_hash=request_hash,
                production_id=args.production_id,
                segment_id=args.segment_id,
                output_mime="audio/mpeg",
                transient=True,
            )
            if not created:
                metadata = {
                    "job_id": job.id,
                    "status": job.status,
                    "idempotent_reuse": True,
                }
                if job.status == "outcome_unknown":
                    metadata.update(
                        error=True,
                        failure_code="stt_outcome_unknown",
                        outcome_unknown=True,
                        manual_review=True,
                        do_not_retry=True,
                    )
                elif job.status in {"failed", "cancelled"}:
                    metadata.update(
                        error=True,
                        failure_code=f"stt_{job.status}",
                    )
                return ToolResult(
                    title="Existing transcription job",
                    output="\n".join(_transcription_lines(job, audio)),
                    metadata=metadata,
                )
            await ctx.update_output("Queueing FFmpeg audio extraction on WUYING…")
            job, remote = await _dispatch_transcription(
                job,
                ctx,
                video_settings,
                oss,
                persist_guard=_ctx_persist_guard(ctx),
            )
            return ToolResult(
                title="Segment transcription queued",
                output="\n".join(_transcription_lines(job, audio, remote=remote)),
                metadata={
                    "job_id": job.id,
                    "status": job.status,
                    "version": int(remote.get("version") or 0),
                    "retry_after_seconds": remote.get("retry_after_seconds", 5),
                },
            )
        except Exception as exc:
            if isinstance(exc, LeaseLostError):
                raise
            if "job" in locals() and created:
                await _update_job(
                    job.id,
                    ctx=ctx,
                    expected_statuses={"dispatching"},
                    status="dispatch_unknown",
                    error=_public_error(exc),
                )
            return _failure_result(
                "Segment transcription submit failed",
                exc,
                failure_code="stt_extraction_dispatch_failed",
                **({"job_id": job.id} if "job" in locals() else {}),
            )

    job = await _owned_job(args.job_id or "", ctx, "stt")
    if not job:
        return ToolResult(title="Transcription job not found", output="No owned STT job has that job_id.")
    audio = await _job_asset(job)
    if job.status == "completed":
        return ToolResult(
            title="Segment transcription ready",
            output="\n".join(_transcription_lines(job, audio)),
            metadata={"job_id": job.id, "status": job.status, "segment_id": job.segment_id},
        )
    if job.status == "outcome_unknown":
        return _failure_result(
            "Transcription outcome needs operator review",
            job.error or "The STT POST may have been accepted without a durable provider receipt.",
            failure_code="stt_outcome_unknown",
            job_id=job.id,
            status=job.status,
            outcome_unknown=True,
            manual_review=True,
            do_not_retry=True,
        )
    if job.status in {"failed", "cancelled"} and args.action not in {"retry", "cancel"}:
        return ToolResult(
            title="Segment transcription status",
            output="\n".join(_transcription_lines(job, audio)),
            metadata={
                "error": True,
                "failure_code": f"stt_{job.status}",
                "job_id": job.id,
                "status": job.status,
                "segment_id": job.segment_id,
            },
        )
    if args.action == "cancel":
        if job.status == "transcribing":
            return ToolResult(
                title="Transcription finalization in progress",
                output="Audio extraction is complete and STT is already running; it can no longer be cancelled.",
            )
        if job.sandbox_job_id and job.status not in {"failed", "cancelled"}:
            try:
                await _assert_ctx_current(ctx)
                remote = await ctx.sandbox.cancel_media_job(job.sandbox_job_id, ctx.user_id)
            except Exception as exc:
                return _failure_result(
                    "Transcription cancellation failed",
                    exc,
                    failure_code="stt_cancel_failed",
                    job_id=job.id,
                )
        await _assert_ctx_current(ctx)
        await _settle_job_and_asset(
            job,
            ctx,
            job_status="cancelled",
            asset_status="failed",
            error="cancelled",
            expected_statuses={job.status},
        )
        job = await _owned_job(job.id, ctx, "stt")
        return ToolResult(title="Transcription cancelled", output="\n".join(_transcription_lines(job)))

    remote: dict[str, Any] | None = None
    try:
        if args.action == "retry":
            if job.status != "failed":
                return ToolResult(title="Retry not available", output="Only a failed transcription can be retried.")
            if audio and audio.status == "ready":
                await _update_job(
                    job.id,
                    ctx=ctx,
                    expected_statuses={"failed"},
                    status="extraction_completed",
                    error=None,
                    completed_at=None,
                )
                job = await _owned_job(job.id, ctx, "stt")
                job = await _finalize_transcription(
                    job,
                    ctx,
                    target,
                    oss,
                    (job.result_data or {}).get("extraction") or {},
                    persist_guard=_ctx_persist_guard(ctx),
                    durable_recovery=True,
                )
            else:
                replacement = await _transcription_payload(job, ctx, video_settings, oss)
                await _assert_ctx_current(ctx)
                remote = await ctx.sandbox.retry_media_job(
                    job.sandbox_job_id or job.id,
                    ctx.user_id,
                    replacement_payload=replacement,
                )
                await _assert_ctx_current(ctx)
                await _mark_asset(
                    job.output_asset_id,
                    status="pending",
                    size=0,
                    ctx=ctx,
                )
                await _update_job(
                    job.id,
                    ctx=ctx,
                    expected_statuses={"failed"},
                    status=remote["status"],
                    error=None,
                    completed_at=None,
                )
                job = await _owned_job(job.id, ctx, "stt")
        elif job.status == "transcribing":
            # Resume only by a persisted provider receipt. The finalizer turns
            # receipt-less legacy rows into explicit outcome_unknown.
            job = await _finalize_transcription(
                job,
                ctx,
                target,
                oss,
                (job.result_data or {}).get("extraction") or {},
                persist_guard=_ctx_persist_guard(ctx),
                durable_recovery=True,
            )
        elif job.status in {"extraction_completed", "transcript_ready"}:
            job = await _finalize_transcription(
                job,
                ctx,
                target,
                oss,
                (job.result_data or {}).get("extraction") or {},
                persist_guard=_ctx_persist_guard(ctx),
                durable_recovery=True,
            )
        else:
            if job.status in {"dispatch_unknown", "dispatching"} and not job.sandbox_job_id:
                try:
                    job, remote = await _dispatch_transcription(
                        job,
                        ctx,
                        video_settings,
                        oss,
                        persist_guard=_ctx_persist_guard(ctx),
                    )
                except LeaseLostError:
                    raise
                except Exception:
                    remote = await ctx.sandbox.get_media_job(job.id, ctx.user_id)
            elif args.action == "wait":
                await ctx.update_output("Waiting for WUYING to extract the segment audio…")
                remote = await ctx.sandbox.wait_media_job(
                    job.sandbox_job_id or job.id,
                    ctx.user_id,
                    after_version=args.after_version,
                    timeout=args.wait_seconds,
                )
            else:
                remote = await ctx.sandbox.get_media_job(job.sandbox_job_id or job.id, ctx.user_id)
            state = str(remote.get("status") or "failed")
            if state == "completed":
                await _update_job(
                    job.id,
                    ctx=ctx,
                    expected_statuses={job.status},
                    status="extraction_completed",
                    result_data={"extraction": remote.get("result") or {}},
                    error=None,
                )
                job = await _owned_job(job.id, ctx, "stt")
                job = await _finalize_transcription(
                    job,
                    ctx,
                    target,
                    oss,
                    remote.get("result") or {},
                    persist_guard=_ctx_persist_guard(ctx),
                    durable_recovery=True,
                )
            elif state in {"failed", "cancelled"}:
                await _settle_job_and_asset(
                    job,
                    ctx,
                    job_status=state,
                    asset_status="failed",
                    error=str(remote.get("error") or state)[:1200],
                    expected_statuses={job.status},
                )
                job = await _owned_job(job.id, ctx, "stt")
            else:
                await _update_job(
                    job.id,
                    ctx=ctx,
                    expected_statuses={job.status},
                    status=state,
                    error=None,
                )
                job = await _owned_job(job.id, ctx, "stt")
    except LeaseLostError:
        raise
    except Exception as exc:
        return _failure_result(
            "Transcription status check failed",
            exc,
            failure_code="stt_status_failed",
            job_id=job.id,
        )

    audio = await _job_asset(job)
    if job.status == "outcome_unknown":
        return _failure_result(
            "Transcription outcome needs operator review",
            job.error or "STT outcome is unknown.",
            failure_code="stt_outcome_unknown",
            job_id=job.id,
            status=job.status,
            outcome_unknown=True,
            manual_review=True,
            do_not_retry=True,
        )
    if job.status == "transcribing" and job.error:
        return _failure_result(
            "Transcription polling failed; receipt retained",
            job.error,
            failure_code="stt_poll_failed_receipt_safe",
            job_id=job.id,
            status=job.status,
            receipt_retained=bool(job.provider_task_id),
            retry_poll_only=bool(job.provider_task_id),
        )
    title = "Segment transcription ready" if job.status == "completed" else "Segment transcription status"
    metadata = {
        "job_id": job.id,
        "status": job.status,
        "segment_id": job.segment_id,
        "version": int((remote or {}).get("version") or 0),
        "retry_after_seconds": int((remote or {}).get("retry_after_seconds") or 5),
    }
    if job.status in {"failed", "cancelled"}:
        metadata.update(error=True, failure_code=f"stt_{job.status}")
    return ToolResult(
        title=title,
        output="\n".join(_transcription_lines(job, audio, remote=remote)),
        metadata=metadata,
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


async def _dispatch_render(
    job,
    ctx: ToolContext,
    settings,
    oss,
    *,
    persist_guard=None,
) -> tuple[Any, dict[str, Any]]:
    payload = await _render_payload(job, ctx, settings, oss)
    remote = await ctx.sandbox.submit_media_job(payload)
    if persist_guard is not None:
        await persist_guard()
    await _update_job(
        job.id,
        ctx=ctx if persist_guard is not None else None,
        expected_statuses={"dispatching", "dispatch_unknown"},
        sandbox_job_id=remote["job_id"],
        status=remote["status"],
        error=None,
        started_at=datetime.now(timezone.utc) if remote["status"] == "in_progress" else None,
    )
    return await _owned_job(job.id, ctx, "render"), remote


async def _sync_render(
    job,
    remote: dict[str, Any],
    ctx: ToolContext,
    oss,
    *,
    persist_guard=None,
) -> Any:
    status = remote.get("status") or "failed"
    result = remote.get("result") if isinstance(remote.get("result"), dict) else {}
    if status == "completed":
        asset = await _job_asset(job)
        head = await oss.head(asset.oss_key) if asset else None
        if persist_guard is not None:
            await persist_guard()
        if not head:
            status = "failed"
            remote["error"] = "sandbox reported completion but the OSS output is missing"
        else:
            settled = await _settle_job_and_asset(
                job,
                ctx,
                job_status="completed",
                asset_status="ready",
                error=None,
                expected_statuses={job.status},
                result_data=result,
                size=head["size"] or 0,
            )
            if not settled:
                return await _owned_job(job.id, ctx, "render")
            if getattr(job, "production_id", None) and job.output_asset_id:
                from tool.video_workflow import mark_render_complete

                await _assert_ctx_current(ctx)
                await mark_render_complete(
                    job.production_id,
                    job.output_asset_id,
                    user_id=ctx.user_id,
                )
            return await _owned_job(job.id, ctx, "render")
    if persist_guard is not None:
        await persist_guard()
    if status in {"failed", "cancelled"}:
        await _settle_job_and_asset(
            job,
            ctx,
            job_status=status,
            asset_status="failed",
            error=str(remote.get("error") or status)[:1200],
            expected_statuses={job.status},
            result_data=result,
        )
    else:
        await _update_job(
            job.id,
            ctx=ctx if persist_guard is not None else None,
            expected_statuses={job.status},
            status=status,
            result_data={"progress": remote.get("progress") or {}},
        )
    return await _owned_job(job.id, ctx, "render")


async def execute_render(args: VideoRenderArgs, ctx: ToolContext) -> ToolResult:
    from agent.driver import LeaseLostError

    if not ctx.sandbox:
        return ToolResult(title="Sandbox unavailable", output="Video rendering requires the user's WUYING sandbox.")
    try:
        # Same as transcription: render needs the settings, not a route.
        from core.config import get_config

        settings = get_config().video_generation
        from core.oss import get_oss

        oss = get_oss()
    except Exception as exc:
        return ToolResult(title="Video rendering is not configured", output=_public_error(exc))

    if args.action == "submit":
        try:
            from tool.video_workflow import content_hash, prepare_render_submission

            approved = await prepare_render_submission(ctx, args.production_id or "")
            expected_key = f"{approved['production_id']}:render:{approved['scope_hash'][:16]}"
            if args.idempotency_key != expected_key:
                raise RuntimeError(f"idempotency_key must be the approved render key: {expected_key}")
            if args.segment_assets and args.segment_assets != approved["segment_assets"]:
                raise RuntimeError("render inputs differ from the approved production snapshot")
            if args.captions and args.captions != approved["captions"]:
                raise RuntimeError("render captions must come from the accepted STT transcript")
            rows = await _resolve_inputs(approved["segment_assets"], ctx)
            for row in rows:
                if row.mime not in _VIDEO_MIMES:
                    raise RuntimeError(f"asset {row.id} is {row.mime}; render inputs must be videos")
            request_data = {
                "production_id": approved["production_id"],
                "render_scope_hash": approved["scope_hash"],
                "segment_asset_ids": [row.id for row in rows],
                "captions": approved["captions"],
                "subtitles": approved["subtitles"],
                "channel_name": approved["channel_name"],
                "render_engine": args.render_engine,
                "width": approved["width"],
                "height": approved["height"],
            }
            request_hash = content_hash({"kind": "render", "request_data": request_data})
            job, asset, created = await _create_pending_job(
                ctx=ctx,
                kind="render",
                idempotency_key=args.idempotency_key or "",
                model="wuying-media@1",
                prompt=None,
                request_data=request_data,
                filename=args.filename,
                request_hash=request_hash,
                production_id=approved["production_id"],
            )
            if not created:
                if job.status == "completed":
                    await _attach_completed(job, ctx)
                metadata = {
                    "job_id": job.id,
                    "status": job.status,
                    "idempotent_reuse": True,
                }
                if job.status == "dispatch_unknown":
                    metadata.update(
                        error=True,
                        failure_code="render_dispatch_unknown",
                        outcome_unknown=True,
                    )
                elif job.status in {"failed", "cancelled"}:
                    metadata.update(
                        error=True,
                        failure_code=f"render_{job.status}",
                    )
                return ToolResult(
                    title="Existing render job",
                    output="\n".join(_job_lines(job, asset)),
                    metadata=metadata,
                )
            await ctx.update_output("Queueing the HyperFrames/FFmpeg render on WUYING…")
            job, remote = await _dispatch_render(
                job,
                ctx,
                settings,
                oss,
                persist_guard=_ctx_persist_guard(ctx),
            )
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
            if isinstance(exc, LeaseLostError):
                raise
            if "job" in locals() and created:
                await _update_job(
                    job.id,
                    ctx=ctx,
                    expected_statuses={"dispatching"},
                    status="dispatch_unknown",
                    error=_public_error(exc),
                )
            return _failure_result(
                "Video render dispatch failed",
                exc,
                failure_code="render_dispatch_failed",
                **({"job_id": job.id} if "job" in locals() else {}),
            )

    job = await _owned_job(args.job_id or "", ctx, "render")
    if not job:
        return ToolResult(title="Render job not found", output="No owned render job has that job_id.")

    try:
        if args.action == "retry":
            replacement = await _render_payload(job, ctx, settings, oss)
            await _assert_ctx_current(ctx)
            remote = await ctx.sandbox.retry_media_job(
                job.sandbox_job_id or job.id,
                ctx.user_id,
                replacement_payload=replacement,
            )
            await _assert_ctx_current(ctx)
            await _mark_asset(
                job.output_asset_id,
                status="pending",
                size=0,
                ctx=ctx,
            )
            await _update_job(
                job.id,
                ctx=ctx,
                expected_statuses={job.status},
                status=remote["status"],
                error=None,
                completed_at=None,
            )
        elif args.action == "cancel":
            await _assert_ctx_current(ctx)
            remote = await ctx.sandbox.cancel_media_job(job.sandbox_job_id or job.id, ctx.user_id)
        else:
            if job.status in {"dispatch_unknown", "dispatching"} and not job.sandbox_job_id:
                try:
                    job, remote = await _dispatch_render(
                        job,
                        ctx,
                        settings,
                        oss,
                        persist_guard=_ctx_persist_guard(ctx),
                    )
                except LeaseLostError:
                    raise
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
        job = await _sync_render(
            job,
            remote,
            ctx,
            oss,
            persist_guard=_ctx_persist_guard(ctx),
        )
    except LeaseLostError:
        raise
    except Exception as exc:
        return _failure_result(
            "Render status check failed",
            exc,
            failure_code="render_status_failed",
            job_id=job.id,
        )

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
    metadata = {
        "job_id": job.id,
        "status": job.status,
        "asset_id": asset.id if asset and asset.status == "ready" else None,
        "queue_position": queue_position,
        "retry_after_seconds": retry_after,
        "version": version,
        "attached": attached,
        "resource_check": resource_check,
    }
    if job.status in {"failed", "cancelled"}:
        metadata.update(error=True, failure_code=f"render_{job.status}")
    elif job.status == "dispatch_unknown":
        metadata.update(
            error=True,
            failure_code="render_dispatch_unknown",
            outcome_unknown=True,
        )
    return ToolResult(
        title="Rendered video ready" if job.status == "completed" else "Video render status",
        output="\n".join(lines),
        metadata=metadata,
    )


VIDEO_GENERATE_DESCRIPTION = """\
Submit, inspect, wait for, or cancel Seedance generation for an approved segment.
Paid submit requires production_id, segment_id, and the exact idempotency_key.
Never replace a task after an ambiguous result; reconcile the same job or key."""

VIDEO_TRANSCRIBE_DESCRIPTION = """\
Submit, inspect, wait for, cancel, or explicitly retry transcription for a
generated speech segment. It persists actual words, diffs, similarity, and the
quality verdict; status never retries."""

VIDEO_RENDER_DESCRIPTION = """\
Submit, inspect, wait for, cancel, or explicitly retry an OSS-backed WUYING
render. It reports queue state for bounded waits and terminal cleanup evidence."""


video_generate_tool = define_tool(
    "video_generate",
    description=VIDEO_GENERATE_DESCRIPTION,
    parameters=VideoGenerateArgs,
    execute=execute_generate,
    sandbox_required=False,
    parallel_safe=False,
)


video_transcribe_tool = define_tool(
    "video_transcribe",
    description=VIDEO_TRANSCRIBE_DESCRIPTION,
    parameters=VideoTranscribeArgs,
    execute=execute_transcribe,
    sandbox_required=True,
    parallel_safe=False,
)


video_render_tool = define_tool(
    "video_render",
    description=VIDEO_RENDER_DESCRIPTION,
    parameters=VideoRenderArgs,
    execute=execute_render,
    sandbox_required=True,
    parallel_safe=False,
)
