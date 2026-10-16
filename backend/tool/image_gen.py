"""Built-in image generation and editing through an OpenAI-compatible API.

Unlike a CLI running inside the cloud desktop, this tool keeps provider
credentials on the backend.  Source images are resolved from OpenBox's OSS
ledger, sent to ``/images/edits`` as multipart files, and every result is
written back to OSS, recorded in ``file_assets``, and pinned to the assistant
message.  The existing chat gallery and resource centre therefore see image
outputs without any new frontend transport.
"""
from __future__ import annotations

import base64
import binascii
import asyncio
import hashlib
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from core.log import create_logger
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.image_gen")

_MAX_INPUT_BYTES = 50 * 1024 * 1024
_MAX_MASK_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_INPUT_BYTES = 100 * 1024 * 1024
_MAX_OUTPUT_BYTES = 50 * 1024 * 1024
_GPT_IMAGE_2_QUALITIES = {"low", "medium", "high", "auto"}
_OUTPUT_FORMATS = {"png", "jpeg", "webp"}
_OLDER_GPT_IMAGE_SIZES = {"auto", "1024x1024", "1536x1024", "1024x1536"}
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}


class ImageGenArgs(BaseModel):
    prompt: str = Field(
        min_length=1,
        max_length=32_000,
        description="Production-ready image prompt. For edits, state what must remain unchanged.",
    )
    input_images: list[str] = Field(
        default_factory=list,
        max_length=16,
        description=(
            "Optional OSS asset IDs or absolute tenant-scoped sandbox attachment paths. "
            "When present, the tool edits/composites these images instead of generating from scratch."
        ),
    )
    mask_image: str | None = Field(
        default=None,
        description="Optional PNG mask as an OSS asset ID or attachment path; applies to the first input image.",
    )
    n: int = Field(default=1, ge=1, le=4, description="Variants of this one prompt to generate (1-4).")
    size: str | None = Field(
        default=None,
        description="Output size (auto or WIDTHxHEIGHT). Omit to use openbox.json.",
    )
    quality: Literal["low", "medium", "high", "auto"] | None = Field(
        default=None,
        description="Output quality. Omit to use openbox.json.",
    )
    output_format: Literal["png", "jpeg", "webp"] | None = Field(
        default=None,
        description="Output format. Omit to use openbox.json.",
    )
    output_compression: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="JPEG/WebP compression level (0-100). Ignored for PNG.",
    )
    background: Literal["auto", "opaque", "transparent"] | None = Field(
        default=None,
        description="Background mode. Configured gpt-image-2 does not support native transparent output.",
    )
    filename: str | None = Field(
        default=None,
        max_length=160,
        description="Optional display filename for the OSS resource. A unique object key is always used.",
    )

    @model_validator(mode="after")
    def _dependent_options(self):
        if self.mask_image and not self.input_images:
            raise ValueError("mask_image requires at least one input_images entry")
        if self.background == "transparent" and self.output_format == "jpeg":
            raise ValueError("transparent background requires png or webp output")
        return self


@dataclass(frozen=True)
class ProviderTarget:
    provider: str
    model: str
    api_key: str
    base_url: str | None
    timeout_seconds: int
    #: "openai" (synchronous Images API) or "runninghub" (async task API),
    #: chosen by the configured model id (see tool/image_runninghub.MODELS).
    channel: str = "openai"


@dataclass(frozen=True)
class InputImage:
    asset_id: str
    name: str
    mime: str
    data: bytes
    oss_key: str = ""


@dataclass(frozen=True)
class StoredImage:
    asset_id: str
    name: str
    mime: str
    size: int
    path: str
    attached: bool = True
    materialized: bool = True


def _configured_target(channel: str = "default") -> tuple[ProviderTarget, object]:
    from core.config import get_config

    config = get_config()
    settings = config.image_generation
    if channel == "runninghub":
        settings = settings.model_copy(update={"provider": settings.runninghub_provider,
                                               "model": settings.runninghub_model})
    elif channel != "default":
        raise ValueError("Unknown image generation channel")
    provider_name = settings.provider.strip() or config.model.split("/", 1)[0]
    provider = config.provider.get(provider_name)
    if not provider:
        raise RuntimeError(
            f"image_generation.provider '{provider_name}' has no matching provider entry in openbox.json"
        )
    if not provider.api_key:
        raise RuntimeError(
            f"provider.{provider_name}.api_key is empty; configure it in openbox.json via an environment placeholder"
        )
    model = settings.model.rsplit("/", 1)[-1].strip()
    if not model:
        raise RuntimeError("image_generation.model is empty in openbox.json")
    from tool.image_runninghub import MODELS as RUNNINGHUB_MODELS

    target = ProviderTarget(
        provider=provider_name,
        model=model,
        api_key=provider.api_key,
        base_url=provider.base_url.rstrip("/") if provider.base_url else None,
        timeout_seconds=settings.timeout_seconds,
        channel="runninghub" if model in RUNNINGHUB_MODELS else "openai",
    )
    return target, settings


def _validate_size(model: str, size: str) -> str | None:
    """Return an actionable validation error, or None when the size is valid."""
    if size == "auto":
        return None
    match = re.fullmatch(r"(\d+)x(\d+)", size)
    if not match:
        return "size must be 'auto' or WIDTHxHEIGHT (for example 2048x1152)"
    if model.lower() != "gpt-image-2":
        if size not in _OLDER_GPT_IMAGE_SIZES:
            return f"{model} supports only {', '.join(sorted(_OLDER_GPT_IMAGE_SIZES))}"
        return None
    width, height = (int(match.group(1)), int(match.group(2)))
    if width % 16 or height % 16:
        return "gpt-image-2 width and height must both be multiples of 16"
    if max(width, height) > 3840:
        return "gpt-image-2 maximum edge is 3840px"
    if max(width, height) > 3 * min(width, height):
        return "gpt-image-2 aspect ratio cannot exceed 3:1"
    pixels = width * height
    if not 655_360 <= pixels <= 8_294_400:
        return "gpt-image-2 total pixels must be between 655,360 and 8,294,400"
    return None


def _align_size(model: str, size: str) -> tuple[str, str | None]:
    """Snap WIDTHxHEIGHT onto gpt-image-2's 16px grid, keeping the aspect ratio.

    Familiar sizes such as 1920x1080 are refused only because 1080 is not a
    multiple of 16. Returns ``(size, note)``; ``note`` is set when the size
    changed so the caller can say so. Other limits are left to _validate_size.
    """
    match = re.fullmatch(r"(\d+)x(\d+)", size)
    if model.lower() != "gpt-image-2" or not match:
        return size, None
    width, height = int(match.group(1)), int(match.group(2))
    if width < 16 or height < 16 or (width % 16 == 0 and height % 16 == 0):
        return size, None
    ratio = width / height
    # Fix one side on the grid and derive the other from the ratio, so every
    # candidate keeps the requested shape to within half a grid step.
    candidates = set()
    for side in (width // 16 * 16, width // 16 * 16 + 16):
        candidates.add((side, max(16, round(side / ratio / 16) * 16)))
    for side in (height // 16 * 16, height // 16 * 16 + 16):
        candidates.add((max(16, round(side * ratio / 16) * 16), side))

    def distance(candidate: tuple[int, int]):
        w, h = candidate
        change = max(abs(w - width) / width, abs(h - height) / height)
        return (change, abs(w / h - ratio) / ratio, w * h, w)

    valid = [c for c in candidates if _validate_size(model, f"{c[0]}x{c[1]}") is None]
    w, h = min(valid or candidates, key=distance)
    aligned = f"{w}x{h}"
    return aligned, (
        f"size {size} was adjusted to {aligned} because {model} needs width and "
        "height in multiples of 16 (aspect ratio kept as closely as that allows)"
    )


async def _find_owned_asset(ref: str, ctx: ToolContext):
    """Resolve an asset id or the logical /workspace path shown to the agent."""
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    value = ref.strip()
    if value.startswith("asset:"):
        value = value[6:]
    filters = (
        FileAsset.user_id == ctx.user_id,
        FileAsset.status == "ready",
        FileAsset.is_deleted.is_(False),
    )
    async with get_db_session() as db:
        row = (
            await db.execute(select(FileAsset).where(FileAsset.id == value, *filters))
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
                    .where(FileAsset.name == name, FileAsset.session_id == ctx.session_id, *filters)
                    .order_by(FileAsset.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row:
                return row
        return (
            await db.execute(
                select(FileAsset)
                .where(FileAsset.name == name, *filters)
                .order_by(FileAsset.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _download_asset(oss, row, client, *, limit: int) -> InputImage:
    if row.mime not in _IMAGE_MIMES:
        raise RuntimeError(
            f"{row.name} is {row.mime or 'an unknown type'}; image inputs must be PNG, JPEG, or WebP"
        )
    if row.size and row.size > limit:
        raise RuntimeError(f"{row.name} is too large ({row.size} bytes; max {limit})")
    url = oss.presign_get(row.oss_key, expires_sec=300)
    data = bytearray()
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise RuntimeError(f"could not read {row.name} from OSS (HTTP {response.status_code})")
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise RuntimeError(f"{row.name} exceeds the {limit}-byte image limit")
    return InputImage(row.id, row.name, row.mime, bytes(data), row.oss_key)


async def _load_inputs(
    refs: list[str], mask_ref: str | None, ctx: ToolContext, oss
) -> tuple[list[InputImage], InputImage | None]:
    import httpx

    resolved = []
    for ref in refs:
        row = await _find_owned_asset(ref, ctx)
        if not row:
            raise RuntimeError(
                f"image '{ref}' is not a ready OSS resource owned by this user; "
                "attach it first or stage a local file with view_image"
            )
        resolved.append(row)
    mask_row = None
    if mask_ref:
        mask_row = await _find_owned_asset(mask_ref, ctx)
        if not mask_row:
            raise RuntimeError(f"mask '{mask_ref}' is not a ready OSS resource owned by this user")

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        images = [await _download_asset(oss, row, client, limit=_MAX_INPUT_BYTES) for row in resolved]
        mask = await _download_asset(oss, mask_row, client, limit=_MAX_MASK_BYTES) if mask_row else None

    total = sum(len(image.data) for image in images) + (len(mask.data) if mask else 0)
    if total > _MAX_TOTAL_INPUT_BYTES:
        raise RuntimeError(
            f"combined image inputs are too large ({total} bytes; max {_MAX_TOTAL_INPUT_BYTES})"
        )
    if mask and mask.mime != "image/png":
        raise RuntimeError("mask_image must be a PNG with an alpha channel")
    return images, mask


async def _download_generated_url(url: str) -> bytes:
    import httpx

    data = bytearray()
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise RuntimeError(f"generated image URL returned HTTP {response.status_code}")
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > _MAX_OUTPUT_BYTES:
                    raise RuntimeError("generated image exceeds the 50 MB output limit")
    return bytes(data)


async def _response_images(response) -> list[bytes]:
    items = getattr(response, "data", None) or []
    outputs: list[bytes] = []
    for item in items:
        encoded = getattr(item, "b64_json", None)
        url = getattr(item, "url", None)
        if encoded:
            if encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[-1]
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise RuntimeError("image provider returned invalid base64 data") from exc
        elif url:
            payload = await _download_generated_url(url)
        else:
            raise RuntimeError("image provider returned an item without b64_json or url")
        if not payload:
            raise RuntimeError("image provider returned an empty image")
        if len(payload) > _MAX_OUTPUT_BYTES:
            raise RuntimeError("generated image exceeds the 50 MB output limit")
        outputs.append(payload)
    if not outputs:
        # Some gateways answer 200 with an error body instead of a 4xx. That
        # is still a received response, so it stays an unknown outcome, but
        # its message is the only clue to what went wrong.
        error = getattr(response, "error", None)
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or error
        suffix = f"; provider error: {_redact_provider_text(str(error))}" if error else ""
        raise RuntimeError("image provider returned no images" + suffix)
    return outputs


async def _call_provider(
    target: ProviderTarget,
    args: ImageGenArgs,
    *,
    size: str,
    quality: str,
    output_format: str,
    images: list[InputImage],
    mask: InputImage | None,
    ctx: ToolContext | None = None,
    operation_key: str | None = None,
) -> list[bytes]:
    from openai import AsyncOpenAI
    from agent.trajectory import RequestCapture, context_for_tool, public_value
    from question.runtime import assert_current

    client_kwargs = {
        "api_key": target.api_key,
        "max_retries": 0,  # a retry can bill for a second image; leave that choice to the agent/user
        "timeout": target.timeout_seconds,
    }
    if target.base_url:
        client_kwargs["base_url"] = target.base_url

    common = {
        "model": target.model,
        "prompt": args.prompt,
        "n": args.n,
        "size": size,
        "quality": quality,
        "output_format": output_format,
    }
    if args.output_compression is not None and output_format in {"jpeg", "webp"}:
        common["output_compression"] = args.output_compression
    if args.background is not None:
        common["background"] = args.background
    if operation_key:
        # Correlation only. The configured gateway is not assumed to honor
        # idempotency for image billing, so unknown responses remain terminal.
        common["extra_headers"] = {"X-Client-Request-Id": operation_key}

    try:
        # A revoked run starts no paid image request (the request-start fence).
        await assert_current("request")
        # Binary inputs are recorded as references to their owned assets. The
        # provider request contains only public parameters and those references,
        # never SDK objects, credentials, or expiring signed URLs.
        context = await context_for_tool(ctx) if ctx is not None else None
        retained = {}
        if context is not None:
            from trajectory.artifacts import capture_asset_reference
            for item in [*images, *([mask] if mask else [])]:
                retained[item.asset_id] = await capture_asset_reference(
                    context, asset_id=item.asset_id, oss_key=item.oss_key, media_type=item.mime,
                    size_bytes=len(item.data), name=item.name)

        def input_reference(item):
            return {"asset_id": item.asset_id, "name": item.name, "media_type": item.mime,
                    "sha256": hashlib.sha256(item.data).hexdigest(), "size_bytes": len(item.data),
                    "availability": (retained.get(item.asset_id) or {}).get("availability", "not_recorded")}

        snapshot = {**common, "input": {
            "operation": "edit" if images else "generate",
            "images": [input_reference(item) for item in images],
            "mask": input_reference(mask) if mask else None,
        }}
        capture = await RequestCapture.start(
            ctx, purpose="image_generation", model_id=f"{target.provider}/{target.model}",
            payload=snapshot, capture_level="adapter_input",
        )
    except Exception as exc:
        # Everything above precedes the provider SDK, so nothing was sent.
        _mark_not_sent(exc)
        raise
    if ctx is not None:
        ctx._trajectory_image_request_id = capture.context.request_id if capture.context else None
        ctx._trajectory_image_capture = capture

    client = None
    try:
        try:
            client = AsyncOpenAI(**client_kwargs)
        except Exception as exc:
            _mark_not_sent(exc)
            raise
        if images:
            files = [(image.name, image.data, image.mime) for image in images]
            edit_kwargs = dict(common)
            edit_kwargs["image"] = files[0] if len(files) == 1 else files
            if mask:
                edit_kwargs["mask"] = (mask.name, mask.data, mask.mime)
            response = await client.images.edit(**edit_kwargs)
        else:
            response = await client.images.generate(**common)
        capture.response_ended = time.monotonic()
        outputs = await _response_images(response)
        usage = public_value(getattr(response, "usage", None))
        response_items = getattr(response, "data", None) or []
        blocks = [{"type": "image", "block_id": f"image:{index}",
                   "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                   "artifact_resolution": "request_id_and_sha256",
                   "revised_prompt": getattr(response_items[index], "revised_prompt", None)
                       if index < len(response_items) else None}
                  for index, data in enumerate(outputs)]
        # The bytes are stored once by _store_output as deletable asset payloads;
        # request_id + digest links each returned image to that durable version.
        await capture.chunk({"created": getattr(response, "created", None), "model": target.model,
                             "response": {"output": blocks}, "usage": usage}, blocks=blocks)
        if isinstance(usage, dict):
            await capture.capture_usage(usage)
        await capture.finish("completed")
        return outputs
    except asyncio.CancelledError as exc:
        await capture.finish("cancelled", error=exc)
        raise
    except Exception as exc:
        await capture.finish("failed", error=exc)
        raise
    finally:
        if client is not None:
            await client.close()


def _runninghub_effect_context(target: ProviderTarget, planned) -> dict[str, Any]:
    """What reconciliation needs to query the task later, minus any secret."""
    from tool import image_runninghub as runninghub
    from tool.video_providers import provider_route_fingerprint

    route = runninghub.route_for(target.provider, target.model, target.api_key, target.base_url)
    return {"channel": "runninghub", "endpoint": planned.endpoint, "billing_tier": planned.tier,
            "provider_route_fingerprint": provider_route_fingerprint(route)}


def _image_task_pending_result(mode: str, task_id: str, reason: str, *, operation_key: str,
                               asset_ids: list[str], notes: list[str]) -> ToolResult:
    return ToolResult(
        title=f"Image {mode} still in progress",
        output="\n".join([
            f"RunningHub accepted this task (provider_task_id={task_id}), but {reason}.",
            "It keeps running; the image is saved to the resource centre automatically when it "
            "finishes. Do not resubmit; tell the person it is still being generated.",
            *notes,
        ]),
        metadata={"error": True, "failure_code": "image_task_pending", "provider_task_id": task_id,
                  "operation_key": operation_key, "asset_ids": asset_ids, "pending": True,
                  "do_not_retry": True, "outcome_unknown": False, "manual_review": False},
    )


async def _runninghub_generate(
    target: ProviderTarget,
    planned,
    *,
    ctx: ToolContext,
    effect_claim,
    receipt: dict[str, Any],
    asset_urls: dict[str, str],
) -> list[bytes]:
    """Submit one RunningHub task, record its taskId as the receipt, then poll.

    Once a taskId exists every later failure is either the task's own final
    failure or a pending task that reconciliation finishes by querying that
    id; the task is never submitted twice.
    """
    from agent.driver import LeaseLostError
    from agent.trajectory import service_scope
    from question.runtime import RunRevoked
    from tool import image_runninghub as runninghub

    route = runninghub.route_for(target.provider, target.model, target.api_key, target.base_url)
    try:
        # Signed input URLs are recorded as asset references, never verbatim.
        async with service_scope(ctx, asset_urls=asset_urls):
            task_id = await runninghub.submit_task(route, planned)
    except (RunRevoked, LeaseLostError) as exc:
        # The service fence refuses a revoked run before any request.
        _mark_not_sent(exc)
        raise
    receipt["task_id"] = task_id
    log.info("RunningHub image task accepted task_id=%s model=%s endpoint=%s",
             task_id, target.model, planned.endpoint)
    if effect_claim is not None:
        from agent.effect_ledger import record_effect_accepted

        await record_effect_accepted(effect_claim, provider_handle=task_id,
                                     receipt={"task_id": task_id, "endpoint": planned.endpoint})

    async def progress(state: str, elapsed: float) -> None:
        await ctx.update_output(f"RunningHub image task {state} ({int(elapsed)}s)…")

    data = await runninghub.wait_for_task(route, task_id, timeout=target.timeout_seconds,
                                          progress=progress, should_stop=ctx.abort.is_set)
    try:
        return [await _download_generated_url(url) for url in runninghub.result_urls(data)[:1]]
    except Exception as exc:
        raise runninghub.ImageTaskPending(task_id, "the finished image could not be downloaded yet") from exc


def _detect_output(data: bytes, fallback_format: str) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    fallback = "jpg" if fallback_format == "jpeg" else fallback_format
    return f"image/{'jpeg' if fallback == 'jpg' else fallback}", fallback


def _safe_filename(requested: str | None, asset_id: str, index: int, total: int, ext: str) -> str:
    raw = PurePosixPath(requested or f"generated-{asset_id}").name
    stem = re.sub(r"\.(png|jpe?g|webp)$", "", raw, flags=re.IGNORECASE)
    stem = re.sub(r"[^\w.一-鿿-]", "_", stem).strip("._") or f"generated-{asset_id}"
    if total > 1:
        stem = f"{stem}-{index}"
    return f"{stem[:180]}.{ext}"


async def _upload_bytes(oss, key: str, mime: str, data: bytes) -> int:
    import hashlib
    import httpx

    url = oss.presign_put(key, mime, expires_sec=600)
    upload_error: Exception | None = None
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.put(url, content=data, headers={"Content-Type": mime})
        if response.status_code not in (200, 201, 204):
            upload_error = RuntimeError(
                f"OSS upload failed (HTTP {response.status_code})"
            )
    except Exception as exc:
        upload_error = exc
    head = await oss.head(key)
    if not head or int(head.get("size") or 0) != len(data):
        if upload_error is not None:
            raise upload_error
        raise RuntimeError("generated image is missing from OSS after upload")
    etag = str(head.get("etag") or "").strip('"').casefold()
    if re.fullmatch(r"[0-9a-f]{32}", etag):
        digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
        if etag != digest:
            raise RuntimeError("generated image OSS digest does not match the provider output")
    elif upload_error is not None:
        # After a lost/failed PUT response, matching length alone cannot prove
        # that this deterministic key contains these paid provider bytes.
        raise upload_error
    return head["size"] or len(data)


async def _store_output(
    ctx: ToolContext,
    oss,
    data: bytes,
    fallback_format: str,
    requested_name: str | None,
    prompt: str,
    mode: str,
    index: int,
    total: int,
    *,
    reserved_asset=None,
) -> StoredImage:
    from datetime import datetime, timezone

    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from models.message import FilePart, FileRelation
    from project.workspace import asset_sandbox_path
    from sandbox.assets import _session_project, _use_internal_oss, ensure_cli
    from session.session import save_part

    asset_id = reserved_asset.id if reserved_asset is not None else ascending("asset")
    mime, ext = _detect_output(data, fallback_format)
    name = (
        reserved_asset.name
        if reserved_asset is not None
        else _safe_filename(requested_name, asset_id, index, total, ext)
    )
    key = (
        reserved_asset.oss_key
        if reserved_asset is not None
        else f"assets/{ctx.user_id}/{asset_id}/{name}"
    )
    await _assert_image_current(ctx)
    if reserved_asset is not None:
        from sqlalchemy import update

        async with get_db_session() as db:
            if reserved_asset is not None:
                await _fence_image_write(db, ctx)
            changed = await db.execute(
                update(FileAsset)
                .where(
                    FileAsset.id == asset_id,
                    FileAsset.user_id == ctx.user_id,
                    FileAsset.status == "generating",
                )
                .values(status="uploading", mime=mime, size=len(data))
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise RuntimeError("image output reservation is no longer writable")
    size = await _upload_bytes(oss, key, mime, data)
    project_id = ctx.project_id

    try:
        async with get_db_session() as db:
            if reserved_asset is not None:
                await _fence_image_write(db, ctx)
            project_id = ctx.project_id or await _session_project(db, ctx.session_id, ctx.user_id)
            if reserved_asset is None:
                asset = FileAsset(
                        id=asset_id,
                        user_id=ctx.user_id,
                        workspace_id=ctx.workspace_id,
                        session_id=ctx.session_id,
                        project_id=project_id or None,
                        name=name,
                        oss_key=key,
                        mime=mime,
                        size=size,
                        status="ready",
                        source="agent",
                        transient=False,
                        created_at=datetime.now(timezone.utc),
                    )
                db.add(asset)
            else:
                from sqlalchemy import update

                changed = await db.execute(
                    update(FileAsset)
                    .where(
                        FileAsset.id == asset_id,
                        FileAsset.user_id == ctx.user_id,
                        FileAsset.status == "uploading",
                    )
                    .values(
                        project_id=project_id or None,
                        mime=mime,
                        size=size,
                        status="ready",
                    )
                    .execution_options(synchronize_session=False)
                )
                if changed.rowcount != 1:
                    raise RuntimeError("image output reservation changed before OSS commit")
            if reserved_asset is not None:
                await db.flush()
                asset = await db.get(FileAsset, asset_id)
            from trajectory.artifacts import capture_result_asset_in_tx
            await capture_result_asset_in_tx(db, ctx, asset,
                request_id=getattr(ctx, "_trajectory_image_request_id", None))
            await db.commit()
    except Exception:
        if reserved_asset is None:
            try:
                await oss.delete(key)
            except Exception:
                log.warning("failed to clean up an OSS image after database failure", exc_info=True)
        raise

    path = asset_sandbox_path(
        ctx.user_id,
        project_id,
        name,
        asset_id=asset_id,
    )

    materialized = False
    if ctx.sandbox:
        try:
            await ensure_cli(
                ctx.sandbox,
                getattr(ctx.sandbox, "base_url", "") or ctx.session_id,
            )
            get_url = oss.presign_get(
                key,
                expires_sec=600,
                internal=_use_internal_oss(oss),
            )
            pull = await ctx.sandbox.execute(
                f'PATH="$HOME/.local/bin:$PATH" obx-file get {shlex.quote(get_url)} {shlex.quote(path)}',
                timeout=120,
            )
            materialized = pull.exit_code == 0
            if not materialized:
                log.warning("generated image stayed in OSS but could not reach workspace: %s", pull.stderr[:200])
        except Exception as exc:
            from agent.driver import LeaseLostError

            if isinstance(exc, LeaseLostError):
                raise
            log.warning("generated image stayed in OSS but could not reach workspace", exc_info=True)

    attached = True
    try:
        await save_part(
            FilePart(
                path=path,
                mime_type=mime,
                asset_id=asset_id,
                oss_key=key,
                size=size,
                transient=False,
                relation=FileRelation(
                    source_part_id=ctx.part_id or None,
                    group_id=f"tool:{ctx.part_id}" if ctx.part_id else asset_id,
                    role="result",
                    kind="generated_image",
                    label="Edited image" if mode == "edit" else "Generated image",
                    caption=prompt[:4000],
                    ordinal=index,
                    metadata={"mode": mode, "variant_count": total},
                ),
                session_id=ctx.session_id,
                message_id=ctx.message_id,
            ),
            is_new=True,
            user_id=ctx.user_id,
            run_fence=ctx.run_fence,
        )
    except Exception as exc:
        from question.runtime import RunRevoked
        from agent.driver import LeaseLostError
        if isinstance(exc, (RunRevoked, LeaseLostError)):
            raise
        # The durable resource is still valid and visible in the resource
        # centre.  Do not delete paid-for output merely because the chat card
        # could not be pinned; report the distinction to the caller instead.
        attached = False
        log.warning("generated image saved to OSS but could not be attached to chat", exc_info=True)

    return StoredImage(asset_id, name, mime, size, path, attached, materialized)


def _fingerprint(
    *,
    op: str,
    model: str,
    prompt: str,
    size: str,
    quality: str,
    output_format: str,
    background: str | None,
    output_compression: int | None,
    n: int,
    source_digests: list[str],
    mask_digest: str | None,
) -> str:
    """Content identity of one image request.

    Sources are hashed by BYTES (already in memory from _load_inputs), not by
    URL or asset id — a re-signed URL of the same image must still hit, and a
    different image behind the same name must never hit.
    """
    import hashlib
    import json

    payload = {
        "op": op,
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "output_format": output_format,
        "background": background,
        "output_compression": output_compression,
        "n": n,
        "source_digests": source_digests,
        "mask_digest": mask_digest,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _image_operation_key(ctx: ToolContext, fingerprint: str) -> str:
    import hashlib

    # Tool-call identity is stable across recovery of the same call. Run
    # generation is deliberately excluded so lease takeover does not create a
    # second paid identity.
    call_identity = str(ctx.part_id or f"{ctx.user_id}:{fingerprint}")
    raw = (
        f"image_gen:v1\x1f{ctx.user_id}\x1f{ctx.session_id}"
        f"\x1f{call_identity}\x1f{fingerprint}"
    )
    return "openbox-image-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _reserved_image_id(operation_key: str, index: int) -> str:
    import hashlib

    digest = hashlib.sha256(f"{operation_key}:{index}".encode("utf-8")).hexdigest()[:32]
    return f"asset_img_{digest}"


async def _assert_image_current(ctx: ToolContext) -> None:
    guard = getattr(ctx, "assert_run_current", None)
    if callable(guard):
        await guard()


async def _fence_image_write(db, ctx: ToolContext | None) -> None:
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


async def _reserve_image_outputs(
    ctx: ToolContext,
    *,
    operation_key: str,
    requested_name: str | None,
    output_format: str,
    count: int,
    allow_extend: bool = False,
) -> tuple[list[Any], bool]:
    """Reserve deterministic asset/OSS identities before the paid POST."""
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from sandbox.assets import _session_project

    ext = "jpg" if output_format == "jpeg" else output_format
    mime = "image/jpeg" if output_format == "jpeg" else f"image/{output_format}"
    ids = [_reserved_image_id(operation_key, index) for index in range(1, count + 1)]
    now = datetime.now(timezone.utc)
    created = False
    try:
        async with get_db_session() as db:
            await _fence_image_write(db, ctx)
            existing = list(
                (
                    await db.execute(
                        select(FileAsset).where(
                            FileAsset.id.in_(ids),
                            FileAsset.user_id == ctx.user_id,
                        )
                    )
                ).scalars()
            )
            if existing:
                if len(existing) != count and not allow_extend:
                    raise RuntimeError("image operation reservation is incomplete")
                by_id = {row.id: row for row in existing}
                if len(existing) == count:
                    return [by_id[asset_id] for asset_id in ids], False
            else:
                by_id = {}
            project_id = ctx.project_id or await _session_project(
                db, ctx.session_id, ctx.user_id
            )
            rows = []
            for index, asset_id in enumerate(ids, start=1):
                if asset_id in by_id:
                    rows.append(by_id[asset_id])
                    continue
                name = _safe_filename(
                    requested_name,
                    asset_id,
                    index,
                    count,
                    ext,
                )
                row = FileAsset(
                    id=asset_id,
                    user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id,
                    session_id=ctx.session_id,
                    project_id=project_id or None,
                    name=name,
                    oss_key=f"assets/{ctx.user_id}/{asset_id}/{name}",
                    mime=mime,
                    size=0,
                    status="pending",
                    source="agent",
                    transient=False,
                    created_at=now,
                )
                db.add(row)
                rows.append(row)
            await db.flush()
            created = True
            return rows, created
    except IntegrityError:
        async with get_db_session() as db:
            rows = list(
                (
                    await db.execute(
                        select(FileAsset).where(
                            FileAsset.id.in_(ids),
                            FileAsset.user_id == ctx.user_id,
                        )
                    )
                ).scalars()
            )
            by_id = {row.id: row for row in rows}
            if len(by_id) != count:
                raise RuntimeError("image operation reservation raced incompletely")
            return [by_id[asset_id] for asset_id in ids], False


async def _set_reserved_image_status(
    rows: list[Any],
    status: str,
    *,
    ctx: ToolContext | None,
    expected: set[str] | None = None,
) -> bool:
    from sqlalchemy import update

    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    ids = [row.id for row in rows]
    async with get_db_session() as db:
        await _fence_image_write(db, ctx)
        conditions = [FileAsset.id.in_(ids)]
        if ctx is not None and ctx.user_id:
            conditions.append(FileAsset.user_id == ctx.user_id)
        else:
            owners = {str(getattr(row, "user_id", "") or "") for row in rows}
            if len(owners) != 1 or not next(iter(owners)):
                raise RuntimeError("image reservations do not have one durable owner")
            conditions.append(FileAsset.user_id == next(iter(owners)))
        if expected:
            conditions.append(FileAsset.status.in_(tuple(expected)))
        result = await db.execute(
            update(FileAsset)
            .where(*conditions)
            .values(status=status)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == len(ids)


async def _find_reusable_image(fingerprint: str):
    """Newest cached generation with a live, ready asset — any user."""
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.image_gen_cache import ImageGenCache

    async with get_db_session() as db:
        row = (
            await db.execute(
                select(ImageGenCache, FileAsset)
                .join(FileAsset, FileAsset.id == ImageGenCache.asset_id)
                .where(
                    ImageGenCache.fingerprint == fingerprint,
                    FileAsset.status == "ready",
                    FileAsset.is_deleted.is_(False),
                )
                .order_by(ImageGenCache.created_at.desc())
                .limit(1)
            )
        ).first()
    return (row[0], row[1]) if row else None


async def _store_reused(
    ctx: ToolContext,
    oss,
    cached_asset,
    requested_name: str | None,
    prompt: str,
    mode: str,
) -> StoredImage | None:
    """Fulfil a request by OSS server-side copy of a cached identical output.

    The caller gets their own FileAsset row and OSS key; None means the copy
    failed and the caller should fall through to a normal paid generation.
    """
    from datetime import datetime, timezone

    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from models.message import FilePart, FileRelation
    from project.workspace import asset_sandbox_path
    from sandbox.assets import _session_project, _use_internal_oss, ensure_cli
    from session.session import save_part

    asset_id = ascending("asset")
    ext = cached_asset.name.rsplit(".", 1)[-1] if "." in cached_asset.name else "png"
    name = _safe_filename(requested_name, asset_id, 1, 1, ext)
    key = f"assets/{ctx.user_id}/{asset_id}/{name}"
    await _assert_image_current(ctx)
    try:
        source_head = await oss.head(cached_asset.oss_key)
    except Exception:
        source_head = None
    try:
        head = await oss.copy(cached_asset.oss_key, key)
    except Exception:
        # COPY may have committed even when its response was lost.
        try:
            head = await oss.head(key)
        except Exception:
            head = None
    expected_size = int(
        (source_head or {}).get("size") or getattr(cached_asset, "size", 0) or 0
    )
    actual_size = int((head or {}).get("size") or 0)
    if (
        not head
        or actual_size <= 0
        or (expected_size > 0 and actual_size != expected_size)
    ):
        return None
    source_etag = str((source_head or {}).get("etag") or "").strip('"').casefold()
    dest_etag = str(head.get("etag") or "").strip('"').casefold()
    if source_etag and dest_etag and source_etag != dest_etag:
        return None
    await _assert_image_current(ctx)
    size = head["size"]
    mime = cached_asset.mime
    from trajectory.artifacts import capture_result_asset_in_tx

    async with get_db_session() as db:
        await _fence_image_write(db, ctx)
        project_id = ctx.project_id or await _session_project(db, ctx.session_id, ctx.user_id)
        asset = FileAsset(
                id=asset_id,
                user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                session_id=ctx.session_id,
                project_id=project_id or None,
                name=name,
                oss_key=key,
                mime=mime,
                size=size,
                status="ready",
                source="agent",
                transient=False,
                created_at=datetime.now(timezone.utc),
        )
        db.add(asset)
        await capture_result_asset_in_tx(db, ctx, asset)
        await db.commit()

    path = asset_sandbox_path(
        ctx.user_id,
        project_id,
        name,
        asset_id=asset_id,
    )
    materialized = False
    if ctx.sandbox:
        try:
            await ensure_cli(ctx.sandbox, getattr(ctx.sandbox, "base_url", "") or ctx.session_id)
            get_url = oss.presign_get(key, expires_sec=600, internal=_use_internal_oss(oss))
            pull = await ctx.sandbox.execute(
                f'PATH="$HOME/.local/bin:$PATH" obx-file get {shlex.quote(get_url)} {shlex.quote(path)}',
                timeout=120,
            )
            materialized = pull.exit_code == 0
        except Exception as exc:
            from agent.driver import LeaseLostError

            if isinstance(exc, LeaseLostError):
                raise
            log.warning("reused image stayed in OSS but could not reach workspace", exc_info=True)

    attached = True
    try:
        await save_part(
            FilePart(
                path=path,
                mime_type=mime,
                asset_id=asset_id,
                oss_key=key,
                size=size,
                transient=False,
                relation=FileRelation(
                    source_part_id=ctx.part_id or None,
                    group_id=f"tool:{ctx.part_id}" if ctx.part_id else asset_id,
                    role="result",
                    kind="generated_image",
                    label="Edited image" if mode == "edit" else "Generated image",
                    caption=prompt[:4000],
                    ordinal=1,
                    metadata={"mode": mode, "variant_count": 1, "reused": True},
                ),
                session_id=ctx.session_id,
                message_id=ctx.message_id,
            ),
            is_new=True,
            user_id=ctx.user_id,
            run_fence=ctx.run_fence,
        )
    except Exception as exc:
        from question.runtime import RunRevoked
        from agent.driver import LeaseLostError
        if isinstance(exc, (RunRevoked, LeaseLostError)):
            raise
        attached = False
        log.warning("reused image saved to OSS but could not be attached to chat", exc_info=True)

    return StoredImage(asset_id, name, mime, size, path, attached, materialized)


async def _record_cache(
    fingerprint: str, op: str, model: str, request_data: dict, stored: list[StoredImage], ctx: ToolContext
) -> None:
    """Best-effort: a cache-write failure must never fail a paid generation."""
    from datetime import datetime, timezone

    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.image_gen_cache import ImageGenCache

    try:
        async with get_db_session() as db:
            for item in stored:
                db.add(
                    ImageGenCache(
                        id=ascending("imgc"),
                        user_id=ctx.user_id,
                        fingerprint=fingerprint,
                        op=op,
                        model=model,
                        request_data=request_data,
                        asset_id=item.asset_id,
                        created_at=datetime.now(timezone.utc),
                    )
                )
    except Exception as exc:
        log.debug("image_gen cache insert failed: %s", exc)


def _public_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    prefix = f"HTTP {status}: " if status else ""
    return (prefix + message).strip()[:800]


#: Set on an exception raised before the provider SDK sent anything.
_NOT_SENT = "_openbox_image_request_not_sent"
#: 4xx answers that may still describe an accepted request (a timed-out or
#: duplicate in-flight one), so they keep the unknown-outcome protection.
_AMBIGUOUS_4XX = frozenset({408, 409, 499})
_QUOTA_HINTS = ("quota", "balance", "billing", "credit", "余额", "额度", "欠费", "充值")
_CONTENT_HINTS = ("moderation", "content_policy", "content policy", "safety", "sensitive",
                  "违规", "敏感", "审核")
_REDACTIONS = (
    (re.compile(r"</?[A-Za-z][^<>]*>"), " "),
    (re.compile(r"https?://\S+"), "[url]"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"), "[redacted]"),
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?key[A-Za-z_-]*|secret|token|password|signature|sig)"
                r"(\s*[=:]\s*)[^\s,;&\"']+"), r"\1\2[redacted]"),
    (re.compile(r"[A-Za-z0-9+/=_-]{40,}"), "[redacted]"),
)
#: kind -> (user-facing reason, what the agent should do next)
_REJECTIONS = {
    "invalid_request": ("参数不支持", "Correct the parameters named in the provider message "
                        "and call image_gen again; a corrected retry is safe."),
    "content_policy": ("内容审核未通过", "Revise the prompt or input images, then call image_gen again."),
    "permission": ("权限或额度问题", "Do not retry unchanged; tell the person the image service "
                   "refused this account or model and an administrator must check it."),
    "quota": ("权限或额度问题（额度不足）", "Do not retry; tell the person the image service "
              "account is out of quota or balance."),
    "not_found": ("模型或接口不可用", "Do not retry unchanged; tell the person the configured "
                  "image model or endpoint is unavailable."),
    "rate_limit": ("频率限制，稍后再试", "Wait about 30 seconds before at most one retry; if it "
                   "repeats, tell the person to try again later."),
    "not_sent": ("请求未发出", "The request never reached the provider; one retry is safe."),
    "rejected": ("服务商拒绝了请求", "Do not retry unchanged; report the reason to the person."),
}


@dataclass(frozen=True)
class _ProviderFailure:
    """One provider-call failure, split by whether the request was accepted."""

    definite: bool  # provably not accepted: no image produced, nothing charged
    kind: str
    status: int | None
    detail: str  # redacted, truncated provider text; "" when there is none
    reason: str  # short cause for logs and the unknown-outcome message

    @property
    def retryable(self) -> bool:
        return self.kind in {"invalid_request", "content_policy", "rate_limit", "not_sent"}


def _mark_not_sent(exc: BaseException) -> None:
    try:
        setattr(exc, _NOT_SENT, True)
    except Exception:  # pragma: no cover - an exception type without __dict__
        pass


def _redact_provider_text(text: str, limit: int = 300) -> str:
    """Provider text minus markup, URLs and anything shaped like a credential."""
    value = str(text)
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _provider_error_text(exc: BaseException) -> str:
    body = getattr(exc, "body", None)
    message = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("msg") or body.get("detail") or body.get("error")
    elif isinstance(body, str):
        message = body
    return _redact_provider_text(str(message or getattr(exc, "message", None) or exc))


def _rejection_kind(status: int, exc: BaseException, detail: str) -> str:
    code = str(getattr(exc, "code", None) or "").lower()
    text = f"{code} {detail}".lower()
    if status == 402 or (status in (401, 403, 429) and any(h in text for h in _QUOTA_HINTS)):
        return "quota"
    if status in (401, 403):
        return "permission"
    if status == 429:
        return "rate_limit"
    if status == 404:
        return "not_found"
    if any(hint in text for hint in _CONTENT_HINTS):
        return "content_policy"
    return "invalid_request" if status in (400, 413, 415, 422) else "rejected"


def _classify_provider_failure(exc: BaseException) -> _ProviderFailure:
    """Split failures into proven refusals and possibly-accepted requests.

    Proven refusals are an HTTP 4xx answer (other than 408/409/499) and a
    request that never left this process. Everything after a request may have
    reached the provider (timeouts, 5xx, dropped connections, unusable 200
    responses, undownloadable image URLs) keeps the no-resubmit protection.
    """
    import httpx
    import openai

    from tool.image_runninghub import ImageTaskRejected

    if getattr(exc, _NOT_SENT, False):
        return _ProviderFailure(True, "not_sent", None, "",
                                f"{type(exc).__name__} before the request was sent")
    if isinstance(exc, ImageTaskRejected):
        # RunningHub's own refusal: an HTTP 4xx, a business code such as 605,
        # or a connection that never opened. No taskId exists.
        return _ProviderFailure(True, exc.kind, exc.status, exc.detail or exc.reason, exc.reason)
    if isinstance(exc, openai.APIStatusError):
        status, detail = exc.status_code, _provider_error_text(exc)
        if 400 <= status < 500 and status not in _AMBIGUOUS_4XX:
            return _ProviderFailure(True, _rejection_kind(status, exc, detail), status, detail,
                                    f"HTTP {status}")
        return _ProviderFailure(False, "unknown", status, detail,
                                f"HTTP {status} after the request was sent")
    if isinstance(exc, openai.APIConnectionError):
        cause = exc.__cause__
        if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                              httpx.ProxyError, httpx.UnsupportedProtocol)):
            return _ProviderFailure(True, "not_sent", None, "",
                                    f"{type(cause).__name__}: no connection was established")
        if isinstance(exc, openai.APITimeoutError):
            return _ProviderFailure(False, "unknown", None, "",
                                    "no answer within the timeout after the request was sent")
        return _ProviderFailure(False, "unknown", None, "",
                                "the connection dropped after the request was sent")
    return _ProviderFailure(False, "unknown", None, _redact_provider_text(str(exc)),
                            f"{type(exc).__name__} while reading the provider response")


async def _record_image_rejection(effect_claim, reservations: list[Any],
                                  failure: _ProviderFailure, *,
                                  code: str = "image_provider_rejected") -> None:
    """Persist a proven refusal or failed task: the projection, then the ledger.

    The projection is written without the run fence, so even a worker that
    lost its fence leaves evidence the reconciler can settle as failed.
    """
    error = {"code": code, "kind": failure.kind, "http_status": failure.status}
    await _set_reserved_image_status(reservations, "rejected", ctx=None, expected={"generating"})
    if effect_claim is None:
        return
    from agent.effect_ledger import (
        EffectLeaseLostError,
        record_effect_outcome_unknown,
        settle_effect,
    )

    try:
        await settle_effect(effect_claim, state="failed", error=error,
                            evidence={"provider_accepted": False})
        return
    except Exception:
        log.warning("image rejection could not settle its effect directly", exc_info=True)
    try:
        # Close the claim; reconciliation reads the rejected projection.
        await record_effect_outcome_unknown(effect_claim, error=error)
    except EffectLeaseLostError:
        pass


def _rejected_result(mode: str, failure: _ProviderFailure, *, operation_key: str,
                     asset_ids: list[str], notes: list[str]) -> ToolResult:
    label, next_step = _REJECTIONS[failure.kind]
    answer = f"HTTP {failure.status}" if failure.status else "no provider response"
    lines = [
        f"The image provider refused this {mode} request: 未受理，不扣费 "
        "(not accepted, nothing charged).",
        f"reason={label} ({answer})",
        *([f"provider_message={failure.detail}"] if failure.detail else []),
        f"next_step={next_step}",
        *notes,
    ]
    return ToolResult(
        title=f"Image {mode} rejected: {label}",
        output="\n".join(lines),
        metadata={
            "error": True,
            "failure_code": "image_provider_rejected",
            "rejection_kind": failure.kind,
            "provider_status": failure.status,
            "operation_key": operation_key,
            "asset_ids": asset_ids,
            "provider_accepted": False,
            "charged": False,
            "retryable": failure.retryable,
            "outcome_unknown": False,
            "manual_review": False,
        },
    )


async def _execute(args: ImageGenArgs, ctx: ToolContext) -> ToolResult:
    from core.oss import OssNotConfigured, get_oss

    ctx._trajectory_image_request_id = None
    ctx._trajectory_image_capture = None

    try:
        from session.image_pref import selected
        channel = await selected(ctx.user_id)
        target, settings = _configured_target("runninghub") if channel == "runninghub" else _configured_target()
    except Exception as exc:
        return ToolResult(title="image_gen is not configured", output=_public_error(exc))

    requested_size = args.size or settings.default_size
    size, size_note = _align_size(target.model, requested_size)
    notes = [f"Note: {size_note}."] if size_note else []
    quality = args.quality or settings.default_quality
    output_format = args.output_format or settings.output_format
    rh_model = None
    if target.channel == "runninghub":
        from tool import image_runninghub as runninghub

        rh_model = runninghub.MODELS[target.model]
        refusal = runninghub.unsupported(
            rh_model, n=args.n, mask=bool(args.mask_image), background=args.background,
            input_count=len(args.input_images), prompt=args.prompt,
        )
        if refusal:
            return ToolResult(title="Image request not supported", output=refusal,
                              metadata={"error": True, "failure_code": "image_request_unsupported"})
        try:
            runninghub.plan_request(rh_model, prompt=args.prompt, size=size,
                                    image_urls=["https://check.invalid"] * len(args.input_images),
                                    output_format=output_format)
            error = None
        except runninghub.ImageRequestRefused as exc:
            error = str(exc)
    else:
        error = _validate_size(target.model, size)
    if error:
        if size_note:
            error = f"{error} (requested {requested_size}, aligned to {size})"
        return ToolResult(title="Invalid image size", output=error)
    if quality not in _GPT_IMAGE_2_QUALITIES:
        return ToolResult(title="Invalid image quality", output="quality must be low, medium, high, or auto")
    if output_format not in _OUTPUT_FORMATS:
        return ToolResult(title="Invalid output format", output="output_format must be png, jpeg, or webp")
    if args.background == "transparent" and target.model.lower() == "gpt-image-2":
        return ToolResult(
            title="Transparent output is unavailable",
            output=(
                "The configured gpt-image-2 API does not support background=transparent. "
                "Use an opaque/chroma-key background, or explicitly configure another model that supports native alpha."
            ),
        )
    if args.background == "transparent" and output_format == "jpeg":
        return ToolResult(title="Invalid transparent output", output="Use png or webp for transparent output")
    if ctx.abort.is_set():
        return ToolResult(title="Image generation cancelled", output="Cancelled before contacting the image provider.")

    try:
        oss = get_oss()
    except OssNotConfigured as exc:
        return ToolResult(
            title="image_gen unavailable",
            output=f"Generated images require OSS so chat and the resource centre can display them: {exc}",
        )

    mode = "edit" if args.input_images else "generate"
    try:
        from billing.media import precheck_compose as _precheck_media

        await _precheck_media(ctx.session_id)
    except Exception as exc:
        return ToolResult(title="Image generation refused", output=f"{getattr(exc, 'code', 'BILLING')}: {exc}")
    try:
        if args.input_images:
            await ctx.update_output("Loading source images from OSS…")
        images, mask = await _load_inputs(args.input_images, args.mask_image, ctx, oss)
        import hashlib as _hashlib

        planned = None
        input_urls: dict[str, str] = {}
        if rh_model is not None:
            oversized = next((image for image in images
                              if len(image.data) > rh_model.max_input_mb * 1024 * 1024), None)
            if oversized is not None:
                return ToolResult(
                    title="Image request not supported",
                    output=f"{oversized.name} is larger than the {rh_model.max_input_mb} MB {rh_model.name} accepts",
                    metadata={"error": True, "failure_code": "image_request_unsupported"},
                )
            # RunningHub fetches inputs itself: short-lived signed URLs of the
            # caller's own (ownership-checked) assets, never public copies.
            input_urls = {
                oss.presign_get(image.oss_key, expires_sec=runninghub.INPUT_URL_TTL_SECONDS): image.asset_id
                for image in images
            }
            planned = runninghub.plan_request(
                rh_model, prompt=args.prompt, size=size, image_urls=list(input_urls),
                output_format=output_format,
                input_size=runninghub.image_dimensions(images[0].data) if images else None,
            )
            if planned.note:
                notes.append(f"Note: {planned.note}.")
        fingerprint = _fingerprint(
            op=mode,
            model=target.model,
            prompt=args.prompt,
            size=size,
            quality=quality,
            output_format=output_format,
            background=args.background,
            output_compression=args.output_compression,
            n=args.n,
            source_digests=[_hashlib.sha256(img.data).hexdigest() for img in images],
            mask_digest=_hashlib.sha256(mask.data).hexdigest() if mask else None,
        )
        if getattr(settings, "dedupe", False) and args.n == 1:
            cached = await _find_reusable_image(fingerprint)
            if cached:
                reused = await _store_reused(
                    ctx, oss, cached[1], args.filename, args.prompt, mode
                )
                if reused:
                    verb = "Edited" if mode == "edit" else "Generated"
                    return ToolResult(
                        title=f"{verb} 1 image (reused identical result)",
                        output="\n".join([
                            f"An identical earlier {mode} result was reused without a new provider call.",
                            f"- asset_id={reused.asset_id}; name={reused.name}; path={reused.path}; "
                            f"{reused.mime}; {reused.size} bytes",
                            *notes,
                            "Use the asset_id (preferred) or displayed path as input_images for a follow-up edit.",
                        ]),
                        metadata={
                            "mode": mode,
                            "model": target.model,
                            "asset_id": reused.asset_id,
                            "asset_ids": [reused.asset_id],
                            "names": [reused.name],
                            "size": size,
                            "requested_size": requested_size,
                            "quality": quality,
                            "reused": True,
                        },
                    )
        operation_key = _image_operation_key(ctx, fingerprint)
        ctx._image_notification_key = operation_key
        effect_prepared = None
        effect_claim = None
        if ctx.run_fence is not None:
            # The generic ledger is the provider send authority.  FileAsset
            # reservations remain the product projection, but can no longer
            # be mistaken for evidence that a paid request was or was not
            # accepted by the provider.
            from agent.effect_ledger import EffectRunFence, prepare_effect

            effect_prepared = await prepare_effect(
                EffectRunFence.from_tool_context(ctx),
                adapter="image_gen",
                provider=target.provider,
                operation=mode,
                logical_key=operation_key,
                request_digest=fingerprint,
                project_id=ctx.project_id or None,
                idempotency_key=operation_key,
                safe_context={
                    "asset_ids": [
                        _reserved_image_id(operation_key, index)
                        for index in range(1, args.n + 1)
                    ],
                    "mode": mode,
                    "model": target.model,
                    "output_count": args.n,
                    "billing_key": f"image:{ctx.part_id}",
                    "output_format": output_format,
                    **(_runninghub_effect_context(target, planned) if planned is not None else {}),
                },
            )
        reservations, created = await _reserve_image_outputs(
            ctx,
            operation_key=operation_key,
            requested_name=args.filename,
            output_format=output_format,
            count=args.n,
        )
        if effect_prepared is not None:
            receipt = effect_prepared.snapshot.provider_receipt or {}
            received_count = receipt.get("output_count")
            received_ids = receipt.get("asset_ids")
            if (isinstance(received_count, int) and 0 < received_count <= 16
                    and received_ids == [_reserved_image_id(operation_key, i) for i in range(1, received_count + 1)]):
                reservations, _ = await _reserve_image_outputs(
                    ctx, operation_key=operation_key, requested_name=args.filename,
                    output_format=output_format, count=received_count, allow_extend=True,
                )
        stored: list[StoredImage] = []
        provider_called = False
        task_receipt: dict[str, Any] = {}
        should_generate = created
        if effect_prepared is not None and effect_prepared.snapshot.state != "prepared":
            # ``succeeded`` is allowed to fall through to the ready-reservation
            # projection below. Every other post-send state is reconcile-only;
            # a stable FileAsset row must never reopen provider dispatch.
            should_generate = False
        if not created:
            if all(row.status == "ready" and row.size > 0 for row in reservations):
                from project.workspace import asset_sandbox_path

                stored = [
                    StoredImage(
                        row.id,
                        row.name,
                        row.mime,
                        row.size,
                        asset_sandbox_path(
                            row.user_id,
                            row.project_id,
                            row.name,
                            asset_id=row.id,
                        ),
                        attached=False,
                        materialized=False,
                    )
                    for row in reservations
                ]
            elif (
                all(row.status == "pending" for row in reservations)
                and (
                    effect_prepared is None
                    or effect_prepared.snapshot.state == "prepared"
                )
            ):
                # A prior generation lost its lease at the explicit pre-send
                # guard. No provider call occurred, so the stable reservation
                # is safe for exactly one caller to claim below.
                should_generate = True
            elif all(row.status == "rejected" for row in reservations):
                # A replay of a call the provider provably refused: nothing
                # was accepted or charged, so this is not an unknown outcome.
                return ToolResult(
                    title=f"Image {mode} was already rejected",
                    output=(
                        "The provider already refused this exact image request: 未受理，不扣费 "
                        "(not accepted, nothing charged). Adjust the request before calling "
                        "image_gen again."
                    ),
                    metadata={
                        "error": True,
                        "failure_code": "image_provider_rejected",
                        "operation_key": operation_key,
                        "asset_ids": [row.id for row in reservations],
                        "provider_accepted": False,
                        "charged": False,
                        "outcome_unknown": False,
                        "manual_review": False,
                    },
                )
            elif (
                effect_prepared is not None
                and effect_prepared.snapshot.provider_handle
                and effect_prepared.snapshot.state in {"accepted", "outcome_unknown"}
            ):
                # A replay of an accepted RunningHub task that is still being
                # finished: reconciliation owns it through its taskId.
                return _image_task_pending_result(
                    mode, effect_prepared.snapshot.provider_handle, "it is still being finished",
                    operation_key=operation_key, asset_ids=[row.id for row in reservations], notes=notes,
                )
            else:
                states = sorted({str(row.status) for row in reservations})
                return ToolResult(
                    title="Image operation needs operator review",
                    output=(
                        "This exact image operation already crossed its durable send boundary "
                        "without a complete local result. It will not be submitted again automatically."
                    ),
                    metadata={
                        "error": True,
                        "failure_code": "image_outcome_unknown",
                        "operation_key": operation_key,
                        "asset_ids": [row.id for row in reservations],
                        "states": states,
                        "outcome_unknown": True,
                        "manual_review": True,
                        "do_not_retry": True,
                    },
                )
        if not should_generate and not stored:
            return ToolResult(
                title="Image operation needs reconciliation",
                output=(
                    "This exact image effect is already beyond its durable send boundary, "
                    "but no complete ready asset projection is available. It will not be "
                    "submitted again automatically."
                ),
                metadata={
                    "error": True,
                    "failure_code": "image_effect_reconciliation_required",
                    "operation_key": operation_key,
                    "asset_ids": [row.id for row in reservations],
                    "effect_state": (
                        effect_prepared.snapshot.state
                        if effect_prepared is not None
                        else None
                    ),
                    "manual_review": True,
                    "do_not_retry": True,
                },
            )
        if should_generate:
            if effect_prepared is not None:
                from agent.effect_ledger import claim_effect_for_dispatch

                effect_claim = await claim_effect_for_dispatch(
                    effect_prepared.snapshot.effect_id,
                    EffectRunFence.from_tool_context(ctx),
                )
            claimed = await _set_reserved_image_status(
                reservations,
                "generating",
                ctx=ctx,
                expected={"pending"},
            )
            if not claimed:
                raise RuntimeError("image operation send reservation was lost")
            await ctx.update_output(
                "Editing image with the configured provider…"
                if images
                else "Generating image with the configured provider…"
            )
            try:
                await _assert_image_current(ctx)
                async def bounded_provider():
                    # The provider deadline expires before the outer tool's
                    # cleanup/delivery budget, including SDK close and tracing.
                    if planned is not None:
                        # RunningHub's adapter persists the taskId and owns its
                        # poll deadline, returning ImageTaskPending for recovery.
                        return await provider_operation
                    async with asyncio.timeout(target.timeout_seconds):
                        return await provider_operation

                if effect_claim is not None:
                    from agent.effect_ledger import (
                        assert_effect_dispatchable,
                        mark_effect_submitting,
                    )

                    await mark_effect_submitting(effect_claim)
                    await assert_effect_dispatchable(effect_claim)
                provider_called = True
                if planned is not None:
                    provider_operation = _runninghub_generate(
                        target, planned, ctx=ctx, effect_claim=effect_claim,
                        receipt=task_receipt, asset_urls=input_urls,
                    )
                else:
                    provider_operation = _call_provider(
                        target,
                        args,
                        size=size,
                        quality=quality,
                        output_format=output_format,
                        images=images,
                        mask=mask,
                        ctx=ctx,
                        operation_key=operation_key,
                    )
                if effect_claim is not None:
                    from agent.effect_ledger import (
                        run_with_effect_claim_heartbeat,
                    )

                    payloads = await run_with_effect_claim_heartbeat(
                        effect_claim,
                        bounded_provider(),
                    )
                else:
                    payloads = await bounded_provider()
                await _assert_image_current(ctx)
            except Exception as exc:
                from agent.driver import LeaseLostError
                from question.runtime import RunRevoked
                from tool.image_runninghub import ImageTaskFailed, ImageTaskPending

                asset_ids = [row.id for row in reservations]
                if isinstance(exc, ImageTaskPending):
                    # Accepted and still running (or not yet downloadable):
                    # release the claim with the taskId so reconciliation can
                    # finish it. Reservations stay "generating".
                    if effect_claim is not None:
                        from agent.effect_ledger import (
                            EffectLeaseLostError,
                            record_effect_outcome_unknown,
                        )

                        try:
                            await record_effect_outcome_unknown(
                                effect_claim, error={"code": "image_task_pending"},
                                provider_handle=exc.task_id,
                            )
                        except EffectLeaseLostError:
                            pass
                    log.warning("RunningHub image task pending task_id=%s reason=%s", exc.task_id, exc.reason)
                    return _image_task_pending_result(mode, exc.task_id, exc.reason,
                                                      operation_key=operation_key,
                                                      asset_ids=asset_ids, notes=notes)
                if isinstance(exc, ImageTaskFailed):
                    # The accepted task ended in failure: final, no image.
                    await _record_image_rejection(
                        effect_claim, reservations,
                        _ProviderFailure(True, "rejected", None, "", exc.message), code="image_task_failed",
                    )
                    return ToolResult(
                        title=f"Image {mode} failed",
                        output="\n".join([
                            f"RunningHub ran this {mode} task (provider_task_id={exc.task_id}) but it failed: "
                            f"{exc.message}.",
                            "No image was produced and nothing was charged to the person's credits; "
                            "a corrected retry is allowed.",
                            *notes,
                        ]),
                        metadata={"error": True, "failure_code": "image_task_failed",
                                  "provider_task_id": exc.task_id, "operation_key": operation_key,
                                  "asset_ids": asset_ids, "retryable": True,
                                  "outcome_unknown": False, "manual_review": False},
                    )
                failure = (
                    _classify_provider_failure(exc)
                    if provider_called
                    else _ProviderFailure(False, "unknown", None, "",
                                          f"{type(exc).__name__} at the send boundary")
                )
                if failure.definite:
                    # The provider refused, or the request never left: nothing
                    # was accepted or charged, so a corrected retry is safe.
                    await _record_image_rejection(effect_claim, reservations, failure)
                    log.warning(
                        "image provider refused %s: kind=%s status=%s type=%s message=%s",
                        mode, failure.kind, failure.status, type(exc).__name__,
                        failure.detail or "-",
                    )
                    if isinstance(exc, (LeaseLostError, RunRevoked)):
                        raise
                    return _rejected_result(
                        mode, failure, operation_key=operation_key,
                        asset_ids=[row.id for row in reservations], notes=notes,
                    )
                if effect_claim is not None and provider_called:
                    from agent.effect_ledger import (
                        EffectLeaseLostError,
                        record_effect_outcome_unknown,
                    )

                    try:
                        await record_effect_outcome_unknown(
                            effect_claim,
                            error={
                                "code": "image_provider_response_unknown",
                                "error_type": type(exc).__name__,
                                "http_status": failure.status,
                            },
                        )
                    except EffectLeaseLostError:
                        # A takeover owns the durable classification now.  The
                        # old worker must not repair over the new claim.
                        pass
                elif effect_claim is not None:
                    from agent.effect_ledger import (
                        EffectLeaseLostError,
                        abandon_effect_before_dispatch,
                    )

                    try:
                        await abandon_effect_before_dispatch(
                            effect_claim,
                            reason=f"pre_send_{type(exc).__name__}",
                        )
                    except EffectLeaseLostError:
                        # If ``submitting`` actually committed before an
                        # unknown database result, only reconciliation may
                        # classify it. Never force it back to prepared.
                        pass

                # A lease check that fails before entering the provider SDK is
                # a proven pre-send failure, so the stable reservation may be
                # returned to pending. Once the call begins, any exception is
                # ambiguous and paid resubmission remains disabled.
                next_status = (
                    "pending"
                    if isinstance(exc, (LeaseLostError, RunRevoked)) and not provider_called
                    else "outcome_unknown"
                )
                await _set_reserved_image_status(
                    reservations,
                    next_status,
                    ctx=None,
                    expected={"generating"},
                )
                if isinstance(exc, (LeaseLostError, RunRevoked)):
                    raise
                task_id = task_receipt.get("task_id")
                log.warning(
                    "image provider outcome unknown: mode=%s status=%s type=%s reason=%s "
                    "task_id=%s message=%s", mode, failure.status, type(exc).__name__,
                    failure.reason, task_id or "-", failure.detail or "-",
                )
                return ToolResult(
                    title=f"Image {mode} outcome unknown",
                    output="\n".join([
                        "The image provider request may have been accepted, but no durable provider "
                        "receipt is available. Automatic paid resubmission is disabled.",
                        f"reason={failure.reason}",
                        *([f"provider_task_id={task_id}"] if task_id else []),
                        *([f"provider_message={failure.detail}"] if failure.detail else []),
                        "next_step=Tell the person the outcome is uncertain; do not resubmit this "
                        "request unless they explicitly ask.",
                        *notes,
                    ]),
                    metadata={
                        "error": True,
                        "failure_code": "image_provider_outcome_unknown",
                        "operation_key": operation_key,
                        "asset_ids": [row.id for row in reservations],
                        "provider_status": failure.status,
                        "provider_task_id": task_id,
                        "outcome_unknown": True,
                        "manual_review": True,
                        "do_not_retry": True,
                    },
                )
            if len(payloads) > len(reservations):
                reservations, _ = await _reserve_image_outputs(
                    ctx, operation_key=operation_key, requested_name=args.filename,
                    output_format=output_format, count=len(payloads), allow_extend=True,
                )
                await _set_reserved_image_status(reservations, "generating", ctx=ctx, expected={"pending"})
            if len(payloads) < len(reservations):
                await _set_reserved_image_status(reservations[len(payloads):], "rejected", ctx=None,
                                                  expected={"generating"})
                reservations = reservations[:len(payloads)]
            if not payloads:
                if effect_claim is not None:
                    from agent.effect_ledger import settle_effect
                    await settle_effect(effect_claim, state="failed", error={"code": "image_provider_response_incomplete"})
                return ToolResult(title="Image provider returned no images", output="No images were returned or charged. No automatic paid retry was attempted.",
                                  metadata={"error": True, "failure_code": "image_provider_response_incomplete", "do_not_retry": True})
            if effect_claim is not None and planned is None:
                # RunningHub recorded its taskId receipt at submit time.
                import hashlib as _receipt_hashlib

                from agent.effect_ledger import (
                    EffectLeaseLostError,
                    record_effect_accepted,
                )

                try:
                    await record_effect_accepted(
                        effect_claim,
                        provider_handle=None,
                        receipt={
                            "asset_ids": [row.id for row in reservations],
                            "output_count": len(payloads),
                            "output_sha256": [
                                _receipt_hashlib.sha256(payload).hexdigest()
                                for payload in payloads
                            ],
                        },
                    )
                except EffectLeaseLostError:
                    await _set_reserved_image_status(
                        reservations,
                        "outcome_unknown",
                        ctx=None,
                        expected={"generating"},
                    )
                    return ToolResult(
                        title=f"Image {mode} receipt needs reconciliation",
                        output=(
                            "The provider returned image bytes, but this worker no longer owns "
                            "the durable effect receipt. Automatic paid resubmission is disabled."
                        ),
                        metadata={
                            "error": True,
                            "failure_code": "image_receipt_fenced_out",
                            "operation_key": operation_key,
                            "asset_ids": [row.id for row in reservations],
                            "outcome_unknown": True,
                            "manual_review": True,
                            "do_not_retry": True,
                        },
                    )
            await ctx.update_output("Uploading generated image to OSS…")
            try:
                for index, (payload, reservation) in enumerate(
                    zip(payloads, reservations, strict=True), start=1
                ):
                    if effect_claim is not None:
                        from agent.effect_ledger import (
                            renew_effect_claim,
                            run_with_effect_claim_heartbeat,
                        )

                        effect_claim = await renew_effect_claim(effect_claim)
                    store_operation = _store_output(
                        ctx,
                        oss,
                        payload,
                        output_format,
                        args.filename,
                        args.prompt,
                        mode,
                        index,
                        len(payloads),
                        reserved_asset=reservation,
                    )
                    async with asyncio.timeout(240):
                        if effect_claim is not None:
                            stored_image = await run_with_effect_claim_heartbeat(
                                effect_claim,
                                store_operation,
                            )
                        else:
                            stored_image = await store_operation
                    stored.append(stored_image)
                if effect_claim is not None:
                    from agent.effect_ledger import settle_effect

                    effect_claim = await renew_effect_claim(effect_claim)
                    await settle_effect(
                        effect_claim,
                        state="failed" if len(stored) < args.n else "succeeded",
                        error={"code": "image_provider_response_incomplete"} if len(stored) < args.n else None,
                        projection={
                            "asset_ids": [item.asset_id for item in stored],
                            "output_count": len(stored),
                        },
                        evidence={"projection": "file_assets_ready"},
                    )
            except Exception as exc:
                if effect_claim is not None:
                    from agent.effect_ledger import EffectLeaseLostError, record_effect_outcome_unknown
                    try:
                        await record_effect_outcome_unknown(effect_claim, error={"code": "image_delivery_failed"})
                    except EffectLeaseLostError:
                        pass
                await _set_reserved_image_status(
                    reservations,
                    "transfer_failed",
                    ctx=None,
                    expected={"generating", "uploading"},
                )
                from agent.driver import LeaseLostError

                if isinstance(exc, LeaseLostError):
                    raise
                log.warning("image delivery failed after provider response: %s", type(exc).__name__)
                return ToolResult(
                    title="Generated image delivery failed",
                    output=(
                        "The provider response was received, but OSS/database delivery did not finish. "
                        "The paid provider request will not be repeated automatically."
                    ),
                    metadata={
                        "error": True,
                        "failure_code": "image_delivery_failed",
                        "operation_key": operation_key,
                        "asset_ids": [row.id for row in reservations],
                        "manual_review": True,
                        "do_not_retry": True,
                    },
                )
    except asyncio.CancelledError:
        # CancelledError bypasses Exception. Preserve the paid-send boundary
        # even after run revocation; these writes only close owned reservations
        # and never turn ready assets back into failures or reopen dispatch.
        rows = locals().get("reservations") or []
        claim = locals().get("effect_claim")
        if claim is not None:
            from agent.effect_ledger import EffectLeaseLostError, record_effect_outcome_unknown, abandon_effect_before_dispatch
            try:
                if locals().get("provider_called"):
                    await record_effect_outcome_unknown(claim, error={"code": "image_interrupted_after_send"})
                else:
                    await abandon_effect_before_dispatch(claim, reason="image_cancelled_before_send")
            except EffectLeaseLostError:
                pass
        if rows:
            await _set_reserved_image_status(rows, "outcome_unknown" if locals().get("provider_called") else "rejected",
                                             ctx=None, expected={"pending", "generating", "uploading"})
        raise
    except Exception as exc:
        from agent.driver import LeaseLostError

        if isinstance(exc, LeaseLostError):
            raise
        log.warning("image_gen %s failed: %s", mode, _public_error(exc))
        return ToolResult(
            title=f"Image {mode} failed",
            output=_public_error(exc),
            metadata={"error": True, "failure_code": "image_preflight_failed"},
        )

    if fingerprint and provider_called:
        await _record_cache(
            fingerprint,
            mode,
            target.model,
            {"size": size, "quality": quality, "output_format": output_format},
            stored,
            ctx,
        )

    credits = None
    billing_key = f"image:{ctx.part_id or stored[0].asset_id}"
    try:
        from billing.media import settle_image

        credits = await settle_image(
            key=billing_key, workspace_id=ctx.workspace_id, user_id=ctx.user_id,
            session_id=ctx.session_id, model_id=target.model, count=min(args.n, len(stored)),
            # RunningHub prices by resolution tier, as its price-preview does.
            **({"tier": planned.tier} if planned is not None else {}),
        )
    except Exception:  # billing must never fail a stored image
        log.warning("image_gen settlement failed", exc_info=True)

    capture = getattr(ctx, "_trajectory_image_capture", None)
    if capture is not None and capture.context is not None:
        from types import SimpleNamespace
        from sqlalchemy import select
        from db.base import get_db_session
        from db.models.billing import UsageEvent
        from agent.trajectory import capture_billing
        async with get_db_session() as db:
            usage_row = await db.scalar(select(UsageEvent).where(
                UsageEvent.idempotency_key == billing_key,
                UsageEvent.user_id == ctx.user_id,
                UsageEvent.session_id == ctx.session_id,
                UsageEvent.workspace_id == ctx.workspace_id,
            ))
        if usage_row is not None:
            await capture_billing(capture.context, SimpleNamespace(event_id=usage_row.id),
                                  {**(capture.usage or {}), "images": len(stored)},
                                  usage_row.credits, "image_generation")

    verb = "Edited" if mode == "edit" else "Generated"
    model_label = f"{rh_model.name} ({target.model})" if rh_model is not None else target.model
    lines = [
        f"{verb} {len(stored)} image{'s' if len(stored) != 1 else ''} with {model_label}.",
        "Each image is stored in OSS and indexed in the resource centre:",
    ]
    if task_receipt.get("task_id"):
        lines.insert(1, f"provider_task_id={task_receipt['task_id']}")
    for item in stored:
        card = "attached to chat" if item.attached else "resource saved; chat attachment failed"
        workspace = "workspace copy ready" if item.materialized else "OSS only; workspace copy unavailable"
        lines.append(
            f"- asset_id={item.asset_id}; name={item.name}; path={item.path}; "
            f"{item.mime}; {item.size} bytes; {card}; {workspace}"
        )
    if credits is not None:
        from billing.media import billing_status_lines

        lines.append(f"credits={format(credits.normalize(), 'f')}")
        lines += billing_status_lines()
    lines += notes
    if len(stored) != args.n:
        lines.append(f"Requested {args.n}, received and saved {len(stored)}. Missing outputs were not retried; unexpected extra outputs are not charged.")
    lines.append("Use the asset_id (preferred) or displayed path as input_images for a follow-up edit.")
    return ToolResult(
        title=f"{verb} {len(stored)} image{'s' if len(stored) != 1 else ''}",
        output="\n".join(lines),
        metadata={
            "mode": mode,
            "model": target.model,
            "asset_id": stored[0].asset_id,
            "asset_ids": [item.asset_id for item in stored],
            "names": [item.name for item in stored],
            "size": size,
            "requested_size": requested_size,
            "quality": quality,
            "credits": format(credits.normalize(), "f") if credits is not None else None,
            "requested_count": args.n,
            "received_count": len(stored),
            **({"error": True, "failure_code": "image_provider_response_incomplete", "do_not_retry": True,
                "missing_count": args.n - len(stored)} if len(stored) < args.n else {}),
            **({"provider_task_id": task_receipt["task_id"]} if task_receipt.get("task_id") else {}),
        },
    )


async def _notify_image_failure(ctx: ToolContext) -> None:
    """Durable feedback independent of a later model response, deduped per call."""
    if not ctx.user_id or not ctx.session_id or not ctx.part_id:
        return
    try:
        from db.base import get_db_session
        from db.models.session import Session
        from notifications.events import emit
        async with get_db_session() as db:
            session = await db.get(Session, ctx.session_id)
            if session is None or session.user_id != ctx.user_id or session.is_deleted:
                return
            await emit(db, user_id=ctx.user_id, workspace_id=session.workspace_id,
                       session_id=ctx.session_id, kind="task_failed",
                       event_key=f"image:{getattr(ctx, '_image_notification_key', None) or ctx.part_id}:failed",
                       name="图片处理")
    except Exception:
        log.warning("image failure notification could not be persisted", exc_info=True)


async def execute(args: ImageGenArgs, ctx: ToolContext) -> ToolResult:
    ctx._image_notification_key = None
    try:
        result = await _execute(args, ctx)
    except asyncio.CancelledError:
        await _notify_image_failure(ctx)
        raise
    if not result.metadata.get("asset_ids") and not result.metadata.get("asset_id"):
        result.metadata.setdefault("error", True)
    if result.metadata.get("error") and result.metadata.get("failure_code") != "image_task_pending":
        await _notify_image_failure(ctx)
    return result


def _image_body_timeout(_args: dict, _ctx: ToolContext) -> float:
    from core.config import get_config
    # Preflight, provider wait, upload and terminal cleanup are separate phases.
    return float(get_config().image_generation.timeout_seconds) + 180.0 + 4 * 240.0


IMAGE_GEN_DESCRIPTION = """\
Generate raster images from text, or edit OSS assets and uploads. An optional PNG
mask targets the first input. Results are stored, indexed, and attached
automatically; do not attach them again."""


class _ImageEffectReconciler:
    """Reconcile only the deterministic FileAsset/OSS projection.

    The synchronous image endpoint exposes no queryable provider handle.  A
    complete set of ready deterministic asset rows proves local projection,
    and a complete set of ``rejected`` rows records a refusal the dispatching
    worker observed (HTTP 4xx, or a request that never left) but could not
    settle itself; every other state remains manual review and never causes a
    paid replay.
    """

    can_reconcile_without_handle = True

    async def reconcile(self, effect):
        from agent.effect_ledger import ReconcileDecision
        from db.base import get_db_session
        from db.models.file_asset import FileAsset
        from sqlalchemy import select

        raw_ids = effect.safe_context.get("asset_ids")
        receipt = effect.provider_receipt or {}
        received_ids = receipt.get("asset_ids")
        if (isinstance(received_ids, list) and received_ids
                and len(received_ids) <= 16
                and received_ids == [_reserved_image_id(effect.idempotency_key, i)
                                     for i in range(1, len(received_ids) + 1)]):
            raw_ids = received_ids
        asset_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
        expected = len(received_ids) if raw_ids is received_ids else int(effect.safe_context.get("output_count") or len(asset_ids))
        if not asset_ids or len(asset_ids) != expected:
            return ReconcileDecision(
                state="manual_review",
                evidence={"code": "image_projection_identity_incomplete"},
            )
        async with get_db_session() as db:
            rows = list(
                (
                    await db.execute(
                        select(FileAsset).where(
                            FileAsset.id.in_(asset_ids),
                            FileAsset.user_id == effect.tenant_id,
                            FileAsset.is_deleted.is_(False),
                        )
                    )
                ).scalars()
            )
        by_id = {row.id: row for row in rows}
        hashes = receipt.get("output_sha256")
        if (len(by_id) == len(asset_ids) and isinstance(hashes, list)
                and len(hashes) == len(asset_ids)):
            # Recover an upload that succeeded before its DB commit. Only
            # digest-verified bytes at this operation's owned keys qualify.
            from core.oss import get_oss
            from sqlalchemy import update
            for asset_id, digest in zip(asset_ids, hashes):
                row = by_id[asset_id]
                if row.status == "ready" or row.status not in {"generating", "uploading", "transfer_failed", "outcome_unknown"}:
                    continue
                if not row.oss_key.startswith(f"assets/{effect.tenant_id}/{asset_id}/"):
                    continue
                try:
                    data = await get_oss().get_object(row.oss_key, timeout=20)
                except Exception:
                    continue
                if not data or len(data) > _MAX_OUTPUT_BYTES or hashlib.sha256(data).hexdigest() != digest:
                    continue
                mime, _ = _detect_output(data, str(effect.safe_context.get("output_format") or "png"))
                async with get_db_session() as db:
                    result = await db.execute(update(FileAsset).where(FileAsset.id == asset_id,
                        FileAsset.user_id == effect.tenant_id, FileAsset.status == row.status,
                        FileAsset.is_deleted.is_(False)).values(status="ready", size=len(data), mime=mime))
                    if result.rowcount == 1:
                        row.status, row.size = "ready", len(data)
        if len(by_id) == len(asset_ids) and all(
            by_id[asset_id].status == "ready" and by_id[asset_id].size > 0
            for asset_id in asset_ids
        ):
            await _settle_reconciled_image_charge(effect, rows)
            return ReconcileDecision(
                state="failed" if len(asset_ids) < int(effect.safe_context.get("output_count") or 0) else "succeeded",
                receipt=effect.provider_receipt,
                projection={"asset_ids": asset_ids, "output_count": len(asset_ids)},
                evidence={"projection": "file_assets_ready"},
            )
        if len(by_id) == len(asset_ids) and all(
            by_id[asset_id].status == "rejected" for asset_id in asset_ids
        ):
            return ReconcileDecision(
                state="failed",
                receipt=effect.provider_receipt,
                evidence={"code": "image_provider_rejected", "projection": "file_assets_rejected"},
            )
        if (
            effect.safe_context.get("channel") == "runninghub"
            and effect.provider_handle
            and len(by_id) == len(asset_ids)
        ):
            return await _reconcile_runninghub_task(effect, [by_id[asset_id] for asset_id in asset_ids])
        if rows:
            await _set_reserved_image_status(rows, "manual_review", ctx=None,
                                             expected={"pending", "generating", "uploading", "outcome_unknown", "transfer_failed"})
            notice_ctx = ToolContext(user_id=effect.tenant_id, session_id=effect.session_id,
                                     part_id=effect.effect_id, workspace_id=rows[0].workspace_id or "")
            notice_ctx._image_notification_key = effect.idempotency_key
            await _notify_image_failure(notice_ctx)
        return ReconcileDecision(
            state="manual_review",
            receipt=effect.provider_receipt,
            evidence={
                "code": "image_bytes_not_recoverable",
                "known_asset_count": len(by_id),
                "expected_asset_count": len(asset_ids),
                "states": sorted({str(row.status) for row in rows}),
            },
        )


async def _reconcile_runninghub_task(effect, rows: list[Any]):
    """Finish an accepted RunningHub task by querying its taskId, never resending.

    SUCCESS projects the image into the reserved assets (resource centre; the
    chat turn has ended) and records the charge once per effect; FAILED or
    CANCELLED is final; anything else is asked again later.
    """
    from agent.effect_ledger import ReconcileDecision
    from core.config import get_config
    from tool import image_runninghub as runninghub
    from tool import video_providers

    task_id = effect.provider_handle
    context = effect.safe_context
    try:
        route = runninghub.configured_route(effect.provider, str(context.get("model") or ""), get_config())
    except Exception:
        return ReconcileDecision(state="manual_review", provider_handle=task_id,
                                 evidence={"code": "runninghub_route_unavailable"})
    if video_providers.provider_route_fingerprint(route) != context.get("provider_route_fingerprint"):
        # Task ids belong to an account; another credential cannot answer.
        return ReconcileDecision(state="manual_review", provider_handle=task_id,
                                 evidence={"code": "runninghub_route_changed"})
    data = await video_providers.status(route, task_id, operation="image_status")
    state = runninghub.task_state(data)
    if state in ("failed", "cancelled"):
        await _set_reserved_image_status(rows, "rejected", ctx=None,
                                         expected={"generating", "outcome_unknown", "transfer_failed"})
        return ReconcileDecision(state="failed", provider_handle=task_id,
                                 evidence={"code": "image_task_failed", "task_state": state})
    urls = runninghub.result_urls(data)[:len(rows)]
    if state != "completed" or len(urls) < len(rows):
        return ReconcileDecision(state="accepted", provider_handle=task_id, retry_after_seconds=60,
                                 evidence={"code": "image_task_running", "task_state": state})
    payloads = [await _download_generated_url(url) for url in urls]
    await _project_reconciled_images(rows, payloads, str(context.get("output_format") or "png"))
    try:
        from billing.media import settle_image

        await settle_image(key=context.get("billing_key") or f"image:effect:{effect.effect_id}", workspace_id=rows[0].workspace_id,
                           user_id=effect.tenant_id, session_id=effect.session_id,
                           model_id=str(context.get("model") or ""), count=len(rows),
                           tier=context.get("billing_tier"))
    except Exception:  # billing must never undo a delivered image
        log.warning("reconciled image settlement failed", exc_info=True)
    asset_ids = [row.id for row in rows]
    return ReconcileDecision(
        state="succeeded", provider_handle=task_id, receipt={"task_id": task_id},
        projection={"asset_ids": asset_ids, "output_count": len(asset_ids)},
        evidence={"projection": "file_assets_ready", "reconciled_from": "runninghub_query"},
    )


async def _settle_reconciled_image_charge(effect, rows: list[Any]) -> None:
    key = effect.safe_context.get("billing_key")
    if not rows or not isinstance(key, str) or not key.startswith("image:"):
        return  # Legacy effects lack proof of the original settlement key.
    try:
        from billing.media import settle_image
        await settle_image(key=key, workspace_id=rows[0].workspace_id,
                           user_id=effect.tenant_id, session_id=effect.session_id,
                           model_id=str(effect.safe_context.get("model") or ""),
                           count=min(len(rows), int(effect.safe_context.get("output_count") or len(rows))),
                           **({"tier": effect.safe_context["billing_tier"]} if effect.safe_context.get("billing_tier") else {}))
    except Exception:
        log.warning("reconciled image settlement failed", exc_info=True)


async def _project_reconciled_images(rows: list[Any], payloads: list[bytes], fallback_format: str) -> None:
    """Write late images into their reserved, deterministic OSS keys and rows."""
    from sqlalchemy import update

    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    oss = get_oss()
    for row, data in zip(rows, payloads, strict=True):
        mime, _ext = _detect_output(data, fallback_format)
        size = await _upload_bytes(oss, row.oss_key, mime, data)
        async with get_db_session() as db:
            # transfer_failed included: the in-call delivery may have failed
            # after RunningHub finished; its result URL can be fetched again.
            changed = await db.execute(
                update(FileAsset)
                .where(FileAsset.id == row.id, FileAsset.user_id == row.user_id,
                       FileAsset.status.in_(("generating", "outcome_unknown", "uploading", "transfer_failed")))
                .values(status="ready", mime=mime, size=size)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise RuntimeError("image reservation changed during reconciliation")


_image_effect_reconciler = _ImageEffectReconciler()


def _register_image_effect_reconciler() -> None:
    from agent.effect_ledger import register_effect_reconciler

    register_effect_reconciler("image_gen", _image_effect_reconciler)


_register_image_effect_reconciler()


image_gen_tool = define_tool(
    "image_gen",
    description=IMAGE_GEN_DESCRIPTION,
    parameters=ImageGenArgs,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
    batch_serial_safe=True,
    body_timeout=_image_body_timeout,
)
