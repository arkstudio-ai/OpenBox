"""Video generation and speech-to-text: the platform video primitives.

Each works on its own — a prompt and an idempotency key is the whole contract
for a video, an owned audio asset is the whole contract for a transcript.
What stays on the backend is what only the backend can hold: provider
credentials, ownership, per-request capability limits, and the guards that
keep one paid generation from being paid for twice. Everything about *making
a good video* — how to write the lines, how to keep a presenter consistent,
how to cut and caption the result — is skill knowledge, and editing is the
agent running ffmpeg in the sandbox. Skills describe workflow; they never
change which tools exist.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from auth.jwt import create_asset_download_token
from core.log import create_logger
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.video_production")

_SEGMENT_TERMINAL = {"completed", "failed", "cancelled"}
#: Everything that is not terminal for a paid generation. A duplicate submit
#: must be refused across this whole window, the OSS transfer included.
_IN_FLIGHT_STATUSES = {
    "submitting",
    "queued",
    "in_progress",
    "generating",
    "finalizing",
    "transfer_failed",
}
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
#: Roles the backend can infer from a mime type. Anything else must be said
#: explicitly, because guessing "this image is the last frame" would silently
#: change what the caller paid for.
_DEFAULT_ROLE_BY_KIND = {"image": "reference_image", "video": "reference_video"}


def _asset_kind(mime: str) -> str:
    if mime.startswith(_IMAGE_PREFIX):
        return "image"
    if mime in _VIDEO_MIMES:
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "other"

#: How many bounded waits one run may spend on a single generation before it
#: hands the job to the next turn. Each wait is capped at 25s, so this is the
#: in-turn budget in 25-second units.
#:
#: Measured over 39 paid generations on 2026-09-01: median 197s, p90 301s. At
#: 8 (200s) the budget landed exactly on the median — 46% of jobs paused and
#: needed the person to say "continue", for work that was already nearly done.
#: 13 covers p90, so the common case finishes inside the turn and the pause
#: goes back to meaning what it should: this one is genuinely slow.
_MAX_INLINE_GENERATION_WAITS = 13
_DEFERRED_PROVIDER_RECHECK_SECONDS = 60


#: Reference roles a generation request can carry. ``first_frame`` /
#: ``last_frame`` pin the ends of a shot (the honest way to keep one look
#: across separately generated clips); ``reference_audio`` drives the
#: performance from a track. Availability is per model — see action="models".
VideoRefRole = Literal[
    "reference_image",
    "reference_video",
    "reference_audio",
    "first_frame",
    "last_frame",
]


class VideoInputRef(BaseModel):
    """One input asset, optionally with the role it plays in the shot."""

    asset_id: str = Field(max_length=512)
    #: Omitted: inferred from the mime type. Audio must say its role.
    role: VideoRefRole | None = None


class VideoGenerateArgs(BaseModel):
    action: Literal["models", "estimate", "submit", "status", "wait", "cancel", "fetch"]
    job_id: str | None = Field(default=None, max_length=96)
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=180)
    # ── describe the shot directly ──
    #: Describe the shot. Supplying it means an open generation.
    prompt: str | None = Field(default=None, min_length=1, max_length=32_000)
    #: An id from action="models". Omitted uses the person's chosen model.
    model: str | None = Field(default=None, max_length=160)
    resolution: Literal["480p", "720p", "1080p"] | None = None
    ratio: str | None = Field(default=None, max_length=16)
    #: Seconds, or -1 to let the model choose.
    duration: int | None = Field(default=None, ge=-1, le=300)
    generate_audio: bool | None = None
    watermark: bool | None = None
    #: Reuse one seed to hold a look steady across separate shots. 0 means
    #: "none": models that fill every schema field send 0 for an untouched
    #: optional int, and refusing that would make every seedless model
    #: unusable from those callers.
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    #: Which shot this is, 1-based. Concurrent shots finish out of order, so
    #: attach order is completion order; without this the chat labels whichever
    #: finished second "第 2 段" no matter which shot it actually is.
    shot: int | None = Field(default=None, ge=1, le=200)
    input_assets: list[VideoInputRef] = Field(default_factory=list, max_length=8)
    #: Pay twice for a second take of a request already in flight.
    allow_duplicate: bool = False
    #: For action="fetch": the owned asset to deliver to the workspace.
    asset_id: str | None = Field(default=None, max_length=512)
    wait_seconds: float = Field(default=25.0, ge=0.0, le=25.0)
    after_version: int = Field(default=0, ge=0)
    #: Increment on every wait call, so repeated bounded waits are explicit
    #: rather than an accidental loop. On polling_paused=true, end the run and
    #: resume this job_id in a later turn; never resubmit it.
    wait_iteration: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _required_by_action(self):
        if self.action == "models":
            return self
        if self.action == "fetch":
            if not (self.asset_id or self.job_id):
                raise ValueError("fetch requires asset_id or job_id")
            return self
        if self.action == "estimate":
            if not self.prompt:
                raise ValueError("estimate requires prompt")
            return self
        if self.action == "submit":
            if not self.idempotency_key:
                raise ValueError("submit requires idempotency_key to prevent duplicate billing")
            if not self.prompt:
                raise ValueError("submit requires prompt")
            return self
        if not self.job_id:
            raise ValueError(f"{self.action} requires job_id")
        return self


class VideoTranscribeArgs(BaseModel):
    action: Literal["submit", "status", "wait", "cancel", "retry"]
    job_id: str | None = Field(default=None, max_length=96)
    #: An owned, ready audio asset to transcribe. Extract it from a video in
    #: the sandbox (ffmpeg -vn) and register it with share_file(attach=false).
    asset_id: str | None = Field(default=None, max_length=512)
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=180)
    wait_seconds: float = Field(default=25.0, ge=0.0, le=25.0)
    after_version: int = Field(default=0, ge=0)
    wait_iteration: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _required_by_action(self):
        if self.action == "submit":
            if not self.idempotency_key:
                raise ValueError("submit requires idempotency_key")
            if not self.asset_id:
                raise ValueError("submit requires asset_id (an owned audio asset)")
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

    Routing lives in tool/video_providers.py; the ark relay
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


def _relay_metadata_payload(
    target: VideoProviderTarget, payload: dict[str, Any]
) -> dict[str, Any]:
    """The metadata-carried shape, built from a TokenSpace contents payload.

    The gateway's task adaptors read resolution, ratio, duration, seed,
    generate_audio and content[] out of `metadata` and nowhere else. Images
    additionally ride top-level as `images`, which the adaptor appends to
    content[] before merging metadata — the one field that works flat.
    """
    content = list(payload.get("content") or [])
    prompt = " ".join(
        str(item.get("text") or "") for item in content if item.get("type") == "text"
    ).strip()
    if not prompt:
        raise RuntimeError("relay video request requires a prompt")

    media = [item for item in content if item.get("type") != "text"]
    metadata: dict[str, Any] = {"resolution": str(payload.get("resolution") or "").lower()}
    ratio = str(payload.get("ratio") or "").strip()
    if ratio and ratio != "adaptive":
        metadata["ratio"] = ratio
    duration = payload.get("duration")
    if isinstance(duration, int) and duration != -1:
        metadata["duration"] = duration
    for option in ("generate_audio", "watermark", "seed"):
        if option in payload:
            metadata[option] = payload[option]
    if media:
        metadata["content"] = media

    body: dict[str, Any] = {
        "model": target.model.strip(),
        "prompt": prompt,
        "metadata": metadata,
    }
    images = [
        _content_url(item.get("image_url"))
        for item in media
        if item.get("type") == "image_url"
    ]
    images = [url for url in images if url]
    if images:
        body["images"] = images
    return body


def _bossip_video_payload(target: VideoProviderTarget, payload: dict[str, Any]) -> dict[str, Any]:
    """Translate TokenSpace contents format to BossIP's public `/v1/videos` shape.

    The relay deliberately owns material-library upload under its own upstream
    account. OpenBox therefore gives it short-lived OSS URLs instead of
    account-scoped TokenSpace ``asset://`` identifiers.
    """
    resolution = str(payload.get("resolution") or "").lower()

    # A model the relay routes under its own id keeps that id and takes the
    # metadata shape, which is what its channel adaptor actually reads.
    # Rewriting it to one of the three name-encoded sd2 tiers below was a
    # workaround from before those models were declared, and it cost the
    # caller its resolution: measured 2026-09-01, 480p/9:16 flattened to the
    # top level came back 1280x720, while the same values under `metadata`
    # came back 496x864.
    from core.config import get_config as _cfg
    from tool import video_providers as _vp

    declared = _vp.declared_model(target.model, _cfg())
    if declared is not None and getattr(declared, "wire_shape", "flat") == "metadata":
        return _relay_metadata_payload(target, payload)

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


def _presigned_provider_refs(
    rows: list[Any], *, input_url_ttl_seconds: int, roles: list[str] | None = None
) -> list[dict[str, str]]:
    """Turn owned image/video/audio rows into provider reference inputs.

    This matches BossIP's normal image-to-video path: OpenBox sends scoped OSS
    URLs, and the configured gateway handles any provider-specific preparation.
    ``roles`` overrides the mime-derived default positionally, carrying
    first_frame / last_frame / reference_audio through to channels that can
    express them.
    """
    if not rows:
        return []

    from core.oss import get_oss

    oss = get_oss()
    refs: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        url = oss.presign_get(row.oss_key, expires_sec=input_url_ttl_seconds)
        kind = _asset_kind(row.mime)
        default_role = _DEFAULT_ROLE_BY_KIND.get(kind, "reference_image")
        refs.append(
            {
                "kind": kind,
                "url": url,
                "role": (roles[index] if roles and index < len(roles) else default_role),
            }
        )
    return refs


def _ark_reference_content(refs: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "type": f"{ref['kind']}_url",
            f"{ref['kind']}_url": {"url": ref["url"]},
            "role": ref["role"],
        }
        for ref in refs
    ]


def _validate_generation(
    model: str, resolution: str, duration: int, generate_audio: bool,
    *, declared: Any | None = None,
) -> None:
    if resolution not in _RESOLUTIONS:
        raise RuntimeError(f"unsupported resolution: {resolution}")
    lowered = model.lower()
    # See video_providers.validate_request: a declared model carries its own
    # resolution list, so this legacy single-model rule only guards models the
    # registry does not describe.
    if declared is None and resolution == "1080p" and model != "doubao-seedance-2-0-260128":
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
    reserve_output: bool = True,
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
            if existing.request_hash and existing.request_hash != request_hash:
                raise RuntimeError(
                    "idempotency key conflict: the existing job has a different request hash"
                )
            asset = await db.get(FileAsset, existing.output_asset_id) if existing.output_asset_id else None
            return existing, asset, False

    job_id = ascending("video")
    if not reserve_output:
        # Nothing is produced: transcription of an existing audio asset writes
        # its result into the job row, and reserving an empty asset would leave
        # a permanently pending file in the resource centre.
        now = datetime.now(timezone.utc)
        async with get_db_session() as db:
            project_id = ctx.project_id or await _session_project(db, ctx.session_id, ctx.user_id)
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
                status="dispatching",
                model=model,
                prompt=prompt,
                request_data=request_data,
                result_data={},
                output_asset_id=None,
                attempt=0,
                created_at=now,
                updated_at=now,
            )
            db.add(job)
            try:
                await db.commit()
                return job, None, True
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
            return existing, None, False

    asset_id = ascending("asset")
    name = (
        _safe_audio_filename(filename, job_id)
        if output_mime == "audio/mpeg"
        else _safe_filename(filename, job_id, rendered=kind == "render")
    )
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
    from db.models.video_production import VideoProduction, VideoSegment
    from db.models.video_job import VideoJob
    from models.message import FilePart, FileRelation
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
                    ordinal=(
                        segment.ordinal
                        if segment
                        else (job.request_data or {}).get("shot")
                    ),
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
        )
        return True
    except Exception:
        await _update_job(job.id, attached_message_id=None)
        log.warning("video asset saved but chat attachment failed", exc_info=True)
        return False


#: Refusals this backend decides for itself. Their text names only the
#: caller's own request, so it is safe to show and useless to withhold.
from tool.video_providers import VideoRequestError as _RequestError


def content_hash(value: Any) -> str:
    """Stable hash of a request, for idempotency-key conflict detection."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_transport_failure(exc: Exception) -> bool:
    """True when the request never got a reply — DNS, connect, or timeout.

    These carry the host we dialled at most, which is our own configuration,
    so their text is safe to show. Anything that did get a response is not
    one of these and stays scrubbed.
    """
    import httpx

    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout,
                            httpx.ReadTimeout, httpx.WriteTimeout,
                            httpx.PoolTimeout, httpx.NetworkError))


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
    if _is_transport_failure(exc):
        # A connection that never opened carries no provider content to leak —
        # the message is about our own network, not their response. Scrubbing
        # it to "operation failed" made a local outage read as a provider
        # fault: on 2026-09-01 a few seconds of packet loss was reported to
        # the user as "the generation service is down", while the paid task
        # was fine and the sweep recovered it minutes later.
        return (
            "could not reach the video provider (network or proxy). The paid "
            "task is unaffected and recovery will retry it; do not resubmit."
        )
    return f"{exc.__class__.__name__}: operation failed"


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
    target: VideoTranscriptionTarget, audio_url: str
) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=target.timeout_seconds, follow_redirects=True) as client:
        submitted = await client.post(
            f"{target.base_url}/api/v1/services/audio/asr/transcription",
            headers={
                "Authorization": _auth_header(target.api_key),
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            },
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


async def _provider_transcribe(target: VideoTranscriptionTarget, audio_url: str) -> dict[str, Any]:
    if target.engine == "dashscope":
        return await _dashscope_transcribe(target, audio_url)

    import httpx

    async with httpx.AsyncClient(timeout=target.timeout_seconds, follow_redirects=True) as client:
        response = await client.post(
            f"{target.base_url}/v1/audio/transcriptions",
            headers={"Authorization": _auth_header(target.api_key), "Content-Type": "application/json"},
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
        if persist_guard is not None:
            await persist_guard()
        await _update_job(
            job.id,
            status="transfer_failed",
            error=_public_error(exc),
            result_data={"provider_status": data.get("status")},
        )
        await _mark_asset(job.output_asset_id, status="pending")
        return await _owned_job(job.id, ctx, "segment")
    if persist_guard is not None:
        await persist_guard()
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


def _asset_download_url(asset) -> str:
    token = create_asset_download_token(str(asset.user_id), str(asset.id))
    return f"/api/assets/{asset.id}/download?token={quote(token, safe='')}"


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
        download_url = _asset_download_url(asset)
        lines.extend(
            [
                f"asset_id={asset.id}",
                f"name={asset.name}",
                f"path=/workspace/generated_videos/{asset.name}",
                f"download_url={download_url}",
                f"bytes={asset.size}",
            ]
        )
        if getattr(job, "kind", None) == "render":
            lines.append(
                "handoff_instruction=use the attached final-video card or the exact "
                "download_url; never construct a markdown URL from path or asset_id"
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


def _start_segment_finalization(job, data, ctx, settings, target) -> asyncio.Task[Any]:
    """Keep OSS finalization alive when a bounded wait or client is cancelled."""
    existing = _SEGMENT_FINALIZATION_TASKS.get(job.id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(
        _finalize_segment(job, data, ctx, settings, target),
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

    asset = await _job_asset(job)
    if not asset:
        return None
    try:
        head = await get_oss().copy(source_asset.oss_key, asset.oss_key)
    except Exception:
        head = None
    if not head or not head.get("size"):
        return None
    now = datetime.now(timezone.utc)
    await _mark_asset(job.output_asset_id, status="ready", size=head["size"])
    await _update_job(
        job.id,
        status="completed",
        # Audit only: the source identifiers stay in result_data and are never
        # printed in tool output (they can belong to another user).
        result_data={"reuse": True, "reused_from_job": source_job.id, "bytes": head["size"]},
        attempt=1,
        started_at=now,
        completed_at=now,
        error=None,
    )
    return await _owned_job(job.id, ctx, "segment")


# ── open generation: resolving a request that carries its own content ───────

async def _resolve_open_inputs(
    refs: list[Any], ctx: ToolContext
) -> tuple[list[Any], list[str]]:
    """Resolve open-mode inputs, returning (asset rows, per-row role)."""
    rows: list[Any] = []
    roles: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        row = await _find_owned_asset(ref.asset_id, ctx)
        if not row:
            raise _RequestError(
                f"asset '{ref.asset_id}' is not a ready OSS resource owned by this user"
            )
        kind = _asset_kind(row.mime)
        if kind == "other":
            raise _RequestError(
                f"asset '{ref.asset_id}' is {row.mime}; inputs must be image, video or audio"
            )
        role = ref.role or _DEFAULT_ROLE_BY_KIND.get(kind)
        if role is None:
            raise _RequestError(
                f"asset '{ref.asset_id}' is {row.mime}; say which role it plays "
                "(reference_audio) — the backend does not guess for audio"
            )
        if role == "reference_audio" and kind != "audio":
            raise _RequestError(f"role reference_audio needs an audio asset, not {row.mime}")
        if role in {"first_frame", "last_frame", "reference_image"} and kind != "image":
            raise _RequestError(f"role {role} needs an image asset, not {row.mime}")
        if role == "reference_video" and kind != "video":
            raise _RequestError(f"role reference_video needs a video asset, not {row.mime}")
        if row.id in seen:
            continue
        seen.add(row.id)
        rows.append(row)
        roles.append(role)
    return rows, roles


async def _resolve_open_submission(args: VideoGenerateArgs, ctx: ToolContext) -> dict[str, Any]:
    """Normalize an open request into the same shape an approved segment has.

    Defaults come from the person's composer choice and the model's own
    declaration, never from a hard-coded house style: a caller who says
    nothing gets the configured default, and a caller who says 16:9 gets 16:9.
    """
    from core.config import get_config
    from tool import video_providers

    config = get_config()
    settings = config.video_generation
    model = (args.model or "").strip() or await _session_video_model_id(ctx)
    declared = video_providers.declared_model(model, config) if model else None

    # A caller that populates every schema field sends the zero value for an
    # optional int it never meant to set. Reading that as a real request makes
    # the parameter refuse work nobody asked for, so treat it as absent — a
    # deliberate seed is reused precisely because it is a specific number.
    seed = args.seed or None
    duration_arg = args.duration or None

    resolution = args.resolution or ""
    if not resolution:
        allowed = list(getattr(declared, "resolutions", None) or [])
        resolution = allowed[0] if len(allowed) == 1 else settings.default_resolution
    ratio = (args.ratio or settings.default_ratio or "9:16").strip()
    duration = settings.default_duration if duration_arg is None else duration_arg
    generate_audio = (
        settings.default_generate_audio if args.generate_audio is None else args.generate_audio
    )
    watermark = settings.default_watermark if args.watermark is None else args.watermark
    dropped: list[str] = []
    if seed is not None and not getattr(declared, "supports_seed", False):
        # See validate_request: reproducibility is worth less than the shot.
        seed = None
        dropped.append(f"seed (model {model or settings.model} has none)")
    return {
        "production_id": None,
        "segment_id": None,
        "prompt": args.prompt or "",
        "model": model or None,
        "character_reference_asset": None,
        "input_assets": list(args.input_assets),
        "resolution": resolution,
        "ratio": ratio,
        "duration": duration,
        "generate_audio": generate_audio,
        "watermark": watermark,
        "seed": seed,
        "content_hash": "",
        "plan_hash": "",
        "reconciling_existing": False,
        "dropped": dropped,
        "shot": args.shot,
    }


async def _session_video_model_id(ctx: ToolContext) -> str:
    """The video model the person picked in the composer, if any.

    The pick is a convenience, never a precondition: any failure to read it
    falls through to the configured default rather than blocking a request.
    """
    try:
        from db.base import get_db_session
        from db.models.session import Session as SessionORM

        if not ctx.session_id:
            return ""
        async with get_db_session() as db:
            session = await db.get(SessionORM, ctx.session_id)
        if session and session.user_id == ctx.user_id and session.video_model:
            return session.video_model
        return ""
    except Exception:
        return ""


async def _in_flight_duplicate(prompt_hash: str, ctx: ToolContext, *, exclude_key: str):
    """An identical request this user already has running, if any.

    Idempotency keys stop a *retry* from paying twice; they cannot stop a fresh
    key carrying the same content. This does, because the content key is
    derived from the request rather than supplied by the caller.
    """
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return (
            await db.execute(
                select(VideoJob)
                .where(
                    VideoJob.user_id == ctx.user_id,
                    VideoJob.kind == "segment",
                    VideoJob.prompt_hash == prompt_hash,
                    VideoJob.idempotency_key != exclude_key,
                    VideoJob.status.in_(tuple(_IN_FLIGHT_STATUSES)),
                )
                .order_by(VideoJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _check_submit_budget(ctx: ToolContext) -> None:
    """Refuse a paid submit past the daily ceiling.

    Back-pressure, not an approval: nobody is asked to confirm a charge. The
    agent is told the ceiling was reached so it can relay that to the person,
    which is what the credits ledger will do properly once it exists.
    """
    from core.config import get_config

    limit = int(getattr(get_config().video_generation, "daily_job_limit", 0) or 0)
    if limit <= 0:
        return
    used = await _daily_submit_count(ctx)
    if used >= limit:
        raise _RequestError(
            f"daily video generation limit reached ({used}/{limit} in the last 24h); "
            "tell the user rather than retrying"
        )


async def _daily_submit_count(ctx: ToolContext) -> int:
    from sqlalchemy import func

    from db.base import get_db_session
    from db.models.video_job import VideoJob

    since = datetime.now(timezone.utc) - timedelta(days=1)
    async with get_db_session() as db:
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(VideoJob)
                    .where(
                        VideoJob.user_id == ctx.user_id,
                        VideoJob.kind == "segment",
                        VideoJob.created_at >= since,
                    )
                )
            ).scalar_one()
            or 0
        )


def _model_capability_lines(config) -> list[str]:
    """The declared registry, rendered for the agent.

    The relay publishes no model or pricing endpoint, so this registry is the
    only description of what each model accepts. Reading it here beats copying
    it into a skill document that then drifts.
    """
    settings = config.video_generation
    lines = [f"default_model={settings.model}", ""]
    for entry in settings.models:
        parts = [f"id={entry.id}"]
        if entry.name:
            parts.append(f"name={entry.name}")
        if entry.tier:
            parts.append(f"tier={entry.tier}")
        if entry.resolutions:
            parts.append(f"resolutions={'/'.join(entry.resolutions)}")
        if entry.ratios:
            parts.append(f"ratios={'/'.join(entry.ratios)}")
        if entry.duration_range:
            low, high = entry.duration_range
            parts.append(f"duration={low}-{high}s")
        if entry.supports_smart_duration:
            parts.append("duration=-1 ok")
        capabilities = [
            name
            for name, on in (
                ("seed", entry.supports_seed),
                ("first/last frame", entry.supports_first_last_frame),
                ("reference audio", entry.supports_reference_audio),
                ("reference image", entry.supports_reference_image),
                ("reference video", entry.supports_reference_video),
            )
            if on
        ]
        if capabilities:
            parts.append(f"supports={', '.join(capabilities)}")
        lines.append("  " + "  ".join(parts))
    if not settings.models:
        lines.append("  (no models declared; the configured default is used for every request)")
    return lines


async def _execute_models(ctx: ToolContext) -> ToolResult:
    from core.config import get_config

    config = get_config()
    lines = _model_capability_lines(config)
    chosen = await _session_video_model_id(ctx)
    if chosen:
        lines.insert(1, f"person_selected_model={chosen}")
    return ToolResult(
        title="Video models",
        output="\n".join(lines),
        metadata={"models": [entry.id for entry in config.video_generation.models]},
    )


async def _execute_estimate(args: VideoGenerateArgs, ctx: ToolContext) -> ToolResult:
    """Validate a request and report what it would cost, without submitting."""
    from core.config import get_config
    from tool import video_providers

    try:
        approved = await _resolve_open_submission(args, ctx)
        target, _settings = _configured_target(approved["model"])
        inputs, roles = await _resolve_open_inputs(approved["input_assets"], ctx)
        video_providers.validate_request(
            target,
            resolution=approved["resolution"],
            ratio=approved["ratio"],
            duration=approved["duration"],
            generate_audio=approved["generate_audio"],
            input_mimes=[row.mime for row in inputs],
            declared=video_providers.declared_model(target.model, get_config()),
            roles=tuple(roles),
        )
    except Exception as exc:
        return ToolResult(
            title="This request would be rejected",
            output=_public_error(exc),
            metadata={"valid": False},
        )

    duration = approved["duration"]
    billed = "model-chosen length" if duration == -1 else f"{duration}s"
    used = await _daily_submit_count(ctx)
    limit = int(getattr(get_config().video_generation, "daily_job_limit", 0) or 0)
    lines = [
        "valid=true",
        f"model={target.model}",
        f"resolution={approved['resolution']}  ratio={approved['ratio']}  duration={billed}",
        f"generate_audio={approved['generate_audio']}  watermark={approved['watermark']}",
        f"inputs={len(inputs)}" + (f" roles={'/'.join(roles)}" if roles else ""),
        f"seed={approved['seed']}" if approved["seed"] is not None else "seed=unset",
        *(
            [f"dropped={'; '.join(approved['dropped'])}"]
            if approved.get("dropped")
            else []
        ),
        # Billing is per second of output on this route, so an explicit
        # duration is the whole cost story; there is no per-call price to read.
        f"daily_submits_used={used}" + (f"/{limit}" if limit else " (no limit configured)"),
        "",
        "Nothing was submitted. Re-send as action=\"submit\" with an idempotency_key to pay for it.",
    ]
    return ToolResult(
        title="Video request looks valid",
        output="\n".join(lines),
        metadata={"valid": True, "model": target.model},
    )


async def _execute_fetch(args: VideoGenerateArgs, ctx: ToolContext) -> ToolResult:
    """Put an owned video asset into the workspace so bash tools can edit it."""
    if not ctx.sandbox:
        return ToolResult(
            title="No sandbox",
            output="Materializing an asset into the workspace needs the user's cloud desktop.",
        )
    asset = None
    if args.asset_id:
        asset = await _find_owned_asset(args.asset_id, ctx)
    elif args.job_id:
        job = await _owned_job(args.job_id, ctx, "segment")
        asset = await _job_asset(job) if job else None
    if not asset or asset.status != "ready":
        return ToolResult(
            title="Asset not available",
            output="No ready asset owned by this user matches that id.",
        )
    try:
        path = await _materialize_asset(asset, ctx)
    except Exception as exc:
        return ToolResult(title="Could not deliver the asset", output=_public_error(exc))
    return ToolResult(
        title=asset.name,
        output=f"workspace_path={path}\nasset_id={asset.id}\nmime={asset.mime}\nsize={asset.size}",
        metadata={"asset_id": asset.id, "path": path, "mime": asset.mime},
    )


async def _try_materialize(job, ctx: ToolContext) -> str | None:
    """Best-effort delivery of a finished video into the workspace.

    Editing happens in the sandbox with ffmpeg, so a finished take is only
    useful once it has landed there. This must never fail the call that
    produced it: with no sandbox, or a flaky pull, the download URL still
    stands and action="fetch" can retry later.
    """
    if job.status != "completed" or not getattr(ctx, "sandbox", None):
        return None
    try:
        asset = await _job_asset(job)
        if not asset or asset.status != "ready":
            return None
        return await _materialize_asset(asset, ctx)
    except Exception as exc:
        log.info(f"workspace delivery skipped for {job.id}: {type(exc).__name__}: {exc}")
        return None


async def _materialize_asset(asset, ctx: ToolContext) -> str:
    """Copy one OSS asset into the sandbox workspace, returning its path."""
    from core.oss import get_oss
    from sandbox.assets import deliver

    paths = await deliver(
        ctx.sandbox,
        getattr(ctx.sandbox, "base_url", "") or ctx.session_id,
        get_oss(),
        [asset],
    )
    if not paths:
        raise RuntimeError("the sandbox reported no delivered path")
    return paths[0]


async def execute_generate(args: VideoGenerateArgs, ctx: ToolContext) -> ToolResult:
    if args.action == "models":
        return await _execute_models(ctx)
    if args.action == "estimate":
        return await _execute_estimate(args, ctx)
    if args.action == "fetch":
        return await _execute_fetch(args, ctx)
    if args.action == "submit":
        try:
            target, settings = _configured_target(None)
        except Exception as exc:
            return ToolResult(
                title="Video generation is not configured",
                output=_public_error(exc),
            )
        try:
            approved = await _resolve_open_submission(args, ctx)
            await _check_submit_budget(ctx)
            prompt = approved["prompt"]
            resolution = approved["resolution"]
            ratio = approved["ratio"]
            duration = approved["duration"]
            generate_audio = approved["generate_audio"]
            watermark = approved["watermark"]
            if approved.get("model"):
                # Per-segment model override routes to its own channel.
                target, settings = _configured_target(approved["model"])
            seed = approved.get("seed")
            if ratio not in _RATIOS:
                raise RuntimeError(f"unsupported ratio: {ratio}")
            inputs, roles = await _resolve_open_inputs(approved["input_assets"], ctx)
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
                roles=tuple(roles),
            )
            channel = getattr(target, "channel", "ark")
            refs = _presigned_provider_refs(
                inputs,
                input_url_ttl_seconds=settings.provider_input_url_ttl_seconds,
                roles=roles,
            )
            provider_content = _ark_reference_content(refs) if channel == "ark" else []
            request_data = {
                "roles": list(roles),
                # Which shot this is, so the chat can order concurrent results
                # by script position rather than by whichever finished first.
                "shot": approved.get("shot"),
                "seed": seed,
                "input_asset_ids": [row.id for row in inputs],
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
                            # A different seed or a reference used as a last
                            # frame instead of a plain reference produces a
                            # different video; neither may collide in reuse.
                            "seed": seed,
                            "roles": list(roles),
                        },
                    )
            if (
                prompt_hash
                and not args.allow_duplicate
                and getattr(settings, "refuse_duplicate_in_flight", True)
            ):
                running = await _in_flight_duplicate(
                    prompt_hash, ctx, exclude_key=args.idempotency_key or ""
                )
                if running is not None:
                    return ToolResult(
                        title="An identical generation is already running",
                        output=(
                            f"job_id={running.id}\nstatus={running.status}\n"
                            "This exact request (same prompt, model, parameters and inputs) "
                            "is already in flight, so submitting again would pay twice. "
                            "Wait on that job_id, or pass allow_duplicate=true if a second "
                            "take is genuinely wanted."
                        ),
                        metadata={
                            "job_id": running.id,
                            "status": running.status,
                            "duplicate_in_flight": True,
                        },
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
                prompt_hash=prompt_hash,
            )
            if not created:
                if job.status == "completed":
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
                return ToolResult(
                    title=(
                        "Existing video submission needs operator review"
                        if ambiguous_submit
                        else "Existing video generation job"
                    ),
                    output="\n".join(lines),
                    metadata={
                        "job_id": job.id,
                        "status": job.status,
                        "idempotent_reuse": True,
                        "ambiguous_submit": ambiguous_submit,
                    },
                )

            if prompt_hash:
                reusable = await _find_reusable_segment(prompt_hash)
                if reusable:
                    source_job, source_asset = reusable
                    reused_job = await _complete_from_reuse(job, source_job, source_asset, ctx)
                    if reused_job is not None:
                        # No paid call happened. The spend approval remains
                        # hash-bound audit evidence; source identifiers stay
                        # out of the output.
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

            from core.config import get_config as _get_config

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
                if seed is not None:
                    payload["seed"] = seed
                submit_path = None
            else:
                submit_path, payload = video_providers.build_payload(
                    target,
                    prompt=prompt,
                    refs=refs,
                    resolution=resolution,
                    ratio=ratio,
                    duration=duration,
                    generate_audio=generate_audio,
                    watermark=watermark,
                    seed=seed,
                    declared=video_providers.declared_model(target.model, _get_config()),
                )

            async def submit_and_persist_provider_identity():
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
                await _update_job(
                    job.id,
                    provider_task_id=video_providers.extract_task_id(target, submitted),
                    status=stored_state,
                    attempt=1,
                    started_at=datetime.now(timezone.utc),
                    error=None,
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
                job = await _finalize_segment(job, response, ctx, settings, target)
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
            lines = _job_lines(job, asset)
            if approved.get("dropped"):
                # Degrading must stay visible: a caller who asked for a seed
                # deserves to know it was not honoured, even though the shot
                # itself is exactly what they asked for.
                lines.append(f"dropped={'; '.join(approved['dropped'])}")
            workspace_path = await _try_materialize(job, ctx)
            if workspace_path:
                lines.append(f"workspace_path={workspace_path}")
            return ToolResult(
                title=("Video ready" if job.status == "completed" else "Video generation submitted"),
                output="\n".join(lines),
                metadata={
                    "job_id": job.id,
                    "status": job.status,
                    "asset_id": asset.id if asset and asset.status == "ready" else None,
                    "workspace_path": workspace_path,
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
        if job.provider_task_id and job.status != "transfer_failed":
            try:
                await _provider_cancel(target, job.provider_task_id)
            except Exception as exc:
                return ToolResult(title="Video cancellation failed", output=_public_error(exc))
        cancel_note = (
            "cancelled locally; the gateway channel has no upstream cancel and the provider task may still complete"
            if getattr(target, "channel", "ark") != "ark"
            else "cancelled"
        )
        await _update_job(
            job.id, status="cancelled", completed_at=datetime.now(timezone.utc), error=cancel_note
        )
        await _mark_asset(job.output_asset_id, status="failed")
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
                return ToolResult(
                    title="Video status check failed", output=_public_error(exc)
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
                await _update_job(
                    job.id,
                    status=state,
                    error=message[:1000],
                    completed_at=datetime.now(timezone.utc),
                )
                await _mark_asset(job.output_asset_id, status="failed")
                job = await _owned_job(job.id, ctx, "segment")
            else:
                if state != job.status or getattr(job, "error", None):
                    await _update_job(job.id, status=state, error=None)
                    job = await _owned_job(job.id, ctx, "segment")
            version = _job_snapshot_version(job)
        except Exception as exc:
            if is_wait and _is_timeout_error(exc):
                timed_out = True
                job = await _owned_job(job.id, ctx, "segment")
                break
            return ToolResult(title="Video status check failed", output=_public_error(exc))
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
    workspace_path = None
    if job.status == "completed":
        attached = await _attach_completed(job, ctx)
        job = await _owned_job(job.id, ctx, "segment")
        workspace_path = await _try_materialize(job, ctx)
        title = "Video ready"
    else:
        attached = False
        title = (
            "Video submission needs operator review"
            if job.status == "submitting" and not job.provider_task_id
            else "Video generation status"
        )
    lines = _job_lines(job, asset, retry_after=round(poll_interval_seconds))
    if workspace_path:
        lines.append(f"workspace_path={workspace_path}")
    version = _job_snapshot_version(job)
    still_running = job.status not in _SEGMENT_TERMINAL
    lines.append(f"version={version}")
    polling_paused = bool(
        is_wait
        and still_running
        and args.wait_iteration >= _MAX_INLINE_GENERATION_WAITS
    )
    if polling_paused:
        title = "Video still processing"
        lines.extend(
            [
                "still_running=true",
                "polling_paused=true",
                f"next_check_after_seconds={_DEFERRED_PROVIDER_RECHECK_SECONDS}",
                (
                    "instruction=stop this assistant run now and report that the durable "
                    "provider task is still processing; do not call video_generate again "
                    "in this run, do not cancel, and do not resubmit; resume this exact "
                    "job_id in a later turn"
                ),
            ]
        )
    elif still_running:
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
    if polling_paused:
        metadata.update(
            {
                "polling_paused": True,
                "next_check_after_seconds": _DEFERRED_PROVIDER_RECHECK_SECONDS,
                "do_not_resubmit": True,
            }
        )
    return ToolResult(
        title=title,
        output="\n".join(lines),
        metadata=metadata,
    )


def _transcription_lines(job) -> list[str]:
    lines = [f"job_id={job.id}", f"status={job.status}"]
    result = job.result_data or {}
    transcript = result.get("transcript") if isinstance(result.get("transcript"), dict) else {}
    if transcript:
        lines.append(f"transcript={transcript.get('text') or ''}")
        if transcript.get("duration_ms"):
            lines.append(f"duration_ms={transcript['duration_ms']}")
    source = (job.request_data or {}).get("source_asset_id")
    if source:
        lines.append(f"source_asset_id={source}")
    if job.error:
        lines.append(f"error={job.error}")
    return lines


async def execute_transcribe(args: VideoTranscribeArgs, ctx: ToolContext) -> ToolResult:
    """Speech-to-text over an owned audio asset.

    A backend-only call: the audio already exists as an asset, so there is no
    queue, no sandbox and no extraction step. Whoever wants the words out of a
    video extracts them with ffmpeg first and registers that file.
    """
    try:
        target = _configured_transcription_target()
        from core.config import get_config
        from core.oss import get_oss

        video_settings = get_config().video_generation
        oss = get_oss()
    except Exception as exc:
        return ToolResult(title="Transcription is not configured", output=_public_error(exc))

    if args.action == "submit":
        created = False
        try:
            source = await _find_owned_asset(args.asset_id or "", ctx)
            if not source:
                raise _RequestError(
                    f"asset '{args.asset_id}' is not a ready OSS resource owned by this user"
                )
            if not source.mime.startswith("audio/"):
                raise _RequestError(
                    f"asset '{args.asset_id}' is {source.mime}; transcription needs audio. "
                    "Extract it first (ffmpeg -vn) and register that file."
                )
            request_data = {
                "source_asset_id": source.id,
                "source_bytes": source.size,
                "model": target.model,
            }
            job, _unused, created = await _create_pending_job(
                ctx=ctx,
                kind="stt",
                idempotency_key=args.idempotency_key or "",
                model=target.model,
                prompt=None,
                request_data=request_data,
                filename=None,
                request_hash=content_hash({"kind": "stt", "request_data": request_data}),
                reserve_output=False,
            )
            if not created:
                return ToolResult(
                    title="Existing transcription job",
                    output="\n".join(_transcription_lines(job)),
                    metadata={"job_id": job.id, "status": job.status, "idempotent_reuse": True},
                )
            await ctx.update_output("Transcribing the audio…")
            await _update_job(job.id, status="transcribing", started_at=datetime.now(timezone.utc))
            audio_url = oss.presign_get(
                source.oss_key, expires_sec=video_settings.provider_input_url_ttl_seconds
            )
            transcript = await _provider_transcribe(target, audio_url)
            await _update_job(
                job.id,
                status="completed",
                result_data={"transcript": transcript},
                error=None,
                completed_at=datetime.now(timezone.utc),
            )
            job = await _owned_job(job.id, ctx, "stt")
            return ToolResult(
                title="Transcription ready",
                output="\n".join(_transcription_lines(job)),
                metadata={
                    "job_id": job.id,
                    "status": job.status,
                    "text": transcript.get("text", ""),
                },
            )
        except Exception as exc:
            if created:
                await _update_job(
                    job.id,
                    status="failed",
                    error=_public_error(exc),
                    completed_at=datetime.now(timezone.utc),
                )
            return ToolResult(title="Transcription failed", output=_public_error(exc))

    job = await _owned_job(args.job_id or "", ctx, "stt")
    if not job:
        return ToolResult(
            title="Transcription job not found", output="No owned STT job has that job_id."
        )

    if args.action == "cancel":
        if job.status in {"completed", "failed", "cancelled"}:
            return ToolResult(
                title="Nothing to cancel", output="\n".join(_transcription_lines(job))
            )
        await _update_job(
            job.id,
            status="cancelled",
            error="cancelled",
            completed_at=datetime.now(timezone.utc),
        )
        job = await _owned_job(job.id, ctx, "stt")
        return ToolResult(title="Transcription cancelled", output="\n".join(_transcription_lines(job)))

    if args.action == "retry":
        if job.status != "failed":
            return ToolResult(
                title="Retry not available", output="Only a failed transcription can be retried."
            )
        try:
            source = await _find_owned_asset(
                (job.request_data or {}).get("source_asset_id") or "", ctx
            )
            if not source:
                raise RuntimeError("the source audio asset is no longer available")
            await _update_job(job.id, status="transcribing", error=None, completed_at=None)
            audio_url = oss.presign_get(
                source.oss_key, expires_sec=video_settings.provider_input_url_ttl_seconds
            )
            transcript = await _provider_transcribe(target, audio_url)
            await _update_job(
                job.id,
                status="completed",
                result_data={"transcript": transcript},
                error=None,
                completed_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            await _update_job(
                job.id,
                status="failed",
                error=_public_error(exc),
                completed_at=datetime.now(timezone.utc),
            )
            return ToolResult(title="Transcription retry failed", output=_public_error(exc))
        job = await _owned_job(job.id, ctx, "stt")

    # status / wait: the call itself is synchronous, so there is nothing to
    # poll — an in-flight row means another turn is mid-call.
    return ToolResult(
        title=("Transcription ready" if job.status == "completed" else "Transcription status"),
        output="\n".join(_transcription_lines(job)),
        metadata={"job_id": job.id, "status": job.status},
    )


VIDEO_GENERATE_DESCRIPTION = """\
Generate video. This is the only way to create one, and it works on its own: \
describe the shot in `prompt`, optionally naming model, resolution, ratio, \
duration, audio, seed and reference assets, and pass an idempotency_key. \
Use action="models" to read what each model accepts (the registry is the only \
description of that) and action="estimate" to validate a request for free \
before paying. A finished video lands in OSS and, when a sandbox is present, \
in the workspace for ffmpeg editing; action="fetch" re-delivers any owned \
video asset there. \
Distinct jobs may run together; never parallelize two mutations of one job. \
A paid submit is never replaced after an ambiguous result — reconcile the \
same job or key."""

VIDEO_TRANSCRIBE_DESCRIPTION = """\
Transcribe speech from any audio asset you own: pass asset_id and an \
idempotency_key. To check what a generated video actually says, extract its \
audio in the sandbox (ffmpeg -vn), register it with share_file(attach=false), \
then transcribe that asset. Returns the spoken words; comparing them against \
an intended line is the caller's job."""

video_generate_tool = define_tool(
    "video_generate",
    description=VIDEO_GENERATE_DESCRIPTION,
    parameters=VideoGenerateArgs,
    execute=execute_generate,
    sandbox_required=False,
    parallel_safe=True,
)


video_transcribe_tool = define_tool(
    "video_transcribe",
    description=VIDEO_TRANSCRIBE_DESCRIPTION,
    parameters=VideoTranscribeArgs,
    execute=execute_transcribe,
    # Transcribing an owned audio asset is backend-only; only the legacy
    # project path needs the desktop, and it checks for itself.
    sandbox_required=False,
    parallel_safe=True,
)


