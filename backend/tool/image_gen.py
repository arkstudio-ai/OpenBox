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
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

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
            "Optional OSS asset IDs or attachment paths such as /workspace/uploads/photo.png. "
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


@dataclass(frozen=True)
class InputImage:
    asset_id: str
    name: str
    mime: str
    data: bytes


@dataclass(frozen=True)
class StoredImage:
    asset_id: str
    name: str
    mime: str
    size: int
    path: str
    attached: bool = True
    materialized: bool = True


def _configured_target() -> tuple[ProviderTarget, object]:
    from core.config import get_config

    config = get_config()
    settings = config.image_generation
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
    target = ProviderTarget(
        provider=provider_name,
        model=model,
        api_key=provider.api_key,
        base_url=provider.base_url.rstrip("/") if provider.base_url else None,
        timeout_seconds=settings.timeout_seconds,
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
    return InputImage(row.id, row.name, row.mime, bytes(data))


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
        raise RuntimeError("image provider returned no images")
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
) -> list[bytes]:
    from openai import AsyncOpenAI

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

    client = AsyncOpenAI(**client_kwargs)
    try:
        if images:
            files = [(image.name, image.data, image.mime) for image in images]
            edit_kwargs = dict(common)
            edit_kwargs["image"] = files[0] if len(files) == 1 else files
            if mask:
                edit_kwargs["mask"] = (mask.name, mask.data, mask.mime)
            response = await client.images.edit(**edit_kwargs)
        else:
            response = await client.images.generate(**common)
        return await _response_images(response)
    finally:
        await client.close()


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
    import httpx

    url = oss.presign_put(key, mime, expires_sec=600)
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.put(url, content=data, headers={"Content-Type": mime})
    if response.status_code not in (200, 201, 204):
        raise RuntimeError(f"OSS upload failed (HTTP {response.status_code})")
    head = await oss.head(key)
    if not head:
        raise RuntimeError("generated image is missing from OSS after upload")
    return head["size"] or len(data)


async def _store_output(
    ctx: ToolContext,
    oss,
    data: bytes,
    fallback_format: str,
    requested_name: str | None,
    index: int,
    total: int,
) -> StoredImage:
    from datetime import datetime, timezone

    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from models.message import FilePart
    from sandbox.assets import _session_project, _use_internal_oss, ensure_cli
    from session.session import save_part

    asset_id = ascending("asset")
    mime, ext = _detect_output(data, fallback_format)
    name = _safe_filename(requested_name, asset_id, index, total, ext)
    key = f"assets/{ctx.user_id}/{asset_id}/{name}"
    size = await _upload_bytes(oss, key, mime, data)
    path = f"/workspace/generated_images/{name}"

    try:
        async with get_db_session() as db:
            project_id = ctx.project_id or await _session_project(db, ctx.session_id, ctx.user_id)
            db.add(
                FileAsset(
                    id=asset_id,
                    user_id=ctx.user_id,
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
            )
            await db.commit()
    except Exception:
        try:
            await oss.delete(key)
        except Exception:
            log.warning("failed to clean up an OSS image after database failure", exc_info=True)
        raise

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
        except Exception:
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
                session_id=ctx.session_id,
                message_id=ctx.message_id,
            ),
            is_new=True,
            user_id=ctx.user_id,
        )
    except Exception:
        # The durable resource is still valid and visible in the resource
        # centre.  Do not delete paid-for output merely because the chat card
        # could not be pinned; report the distinction to the caller instead.
        attached = False
        log.warning("generated image saved to OSS but could not be attached to chat", exc_info=True)

    return StoredImage(asset_id, name, mime, size, path, attached, materialized)


def _public_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    prefix = f"HTTP {status}: " if status else ""
    return (prefix + message).strip()[:800]


async def execute(args: ImageGenArgs, ctx: ToolContext) -> ToolResult:
    from core.oss import OssNotConfigured, get_oss

    try:
        target, settings = _configured_target()
    except Exception as exc:
        return ToolResult(title="image_gen is not configured", output=_public_error(exc))

    size = args.size or settings.default_size
    quality = args.quality or settings.default_quality
    output_format = args.output_format or settings.output_format
    error = _validate_size(target.model, size)
    if error:
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
        if args.input_images:
            await ctx.update_output("Loading source images from OSS…")
        images, mask = await _load_inputs(args.input_images, args.mask_image, ctx, oss)
        await ctx.update_output(
            "Editing image with the configured provider…"
            if images
            else "Generating image with the configured provider…"
        )
        payloads = await _call_provider(
            target,
            args,
            size=size,
            quality=quality,
            output_format=output_format,
            images=images,
            mask=mask,
        )
        await ctx.update_output("Uploading generated image to OSS…")
        stored: list[StoredImage] = []
        for index, payload in enumerate(payloads, start=1):
            stored.append(
                await _store_output(
                    ctx,
                    oss,
                    payload,
                    output_format,
                    args.filename,
                    index,
                    len(payloads),
                )
            )
    except Exception as exc:
        log.warning("image_gen %s failed: %s", mode, _public_error(exc))
        return ToolResult(title=f"Image {mode} failed", output=_public_error(exc))

    verb = "Edited" if mode == "edit" else "Generated"
    lines = [
        f"{verb} {len(stored)} image{'s' if len(stored) != 1 else ''} with {target.model}.",
        "Each image is stored in OSS and indexed in the resource centre:",
    ]
    for item in stored:
        card = "attached to chat" if item.attached else "resource saved; chat attachment failed"
        workspace = "workspace copy ready" if item.materialized else "OSS only; workspace copy unavailable"
        lines.append(
            f"- asset_id={item.asset_id}; name={item.name}; path={item.path}; "
            f"{item.mime}; {item.size} bytes; {card}; {workspace}"
        )
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
            "quality": quality,
        },
    )


IMAGE_GEN_DESCRIPTION = """\
Generate or edit raster images with the image provider configured in openbox.json. \
With no input_images this creates images from text; with OSS asset IDs or \
/workspace/uploads paths it performs image-to-image editing/compositing, and an \
optional PNG mask can target the first input. Every result is uploaded to OSS, \
attached to the current reply, and indexed in the resource centre automatically. \
Do not call view_image or share_file on the result again. Use the imagegen skill \
to shape prompts and choose parameters."""


image_gen_tool = define_tool(
    "image_gen",
    description=IMAGE_GEN_DESCRIPTION,
    parameters=ImageGenArgs,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
)
