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
    operation_key: str | None = None,
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
    if operation_key:
        # Correlation only. The configured gateway is not assumed to honor
        # idempotency for image billing, so unknown responses remain terminal.
        common["extra_headers"] = {"X-Client-Request-Id": operation_key}

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
        from agent.driver import LeaseLostError

        if isinstance(exc, LeaseLostError):
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
                if len(existing) != count:
                    raise RuntimeError("image operation reservation is incomplete")
                by_id = {row.id: row for row in existing}
                return [by_id[asset_id] for asset_id in ids], False
            project_id = ctx.project_id or await _session_project(
                db, ctx.session_id, ctx.user_id
            )
            rows = []
            for index, asset_id in enumerate(ids, start=1):
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

    async with get_db_session() as db:
        await _fence_image_write(db, ctx)
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
        from agent.driver import LeaseLostError

        if isinstance(exc, LeaseLostError):
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
        import hashlib as _hashlib

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
                        output=(
                            f"An identical earlier {mode} result was reused without a new provider call.\n"
                            f"- asset_id={reused.asset_id}; name={reused.name}; path={reused.path}; "
                            f"{reused.mime}; {reused.size} bytes\n"
                            "Use the asset_id (preferred) or displayed path as input_images for a follow-up edit."
                        ),
                        metadata={
                            "mode": mode,
                            "model": target.model,
                            "asset_id": reused.asset_id,
                            "asset_ids": [reused.asset_id],
                            "names": [reused.name],
                            "size": size,
                            "quality": quality,
                            "reused": True,
                        },
                    )
        operation_key = _image_operation_key(ctx, fingerprint)
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
                    "output_format": output_format,
                },
            )
        reservations, created = await _reserve_image_outputs(
            ctx,
            operation_key=operation_key,
            requested_name=args.filename,
            output_format=output_format,
            count=args.n,
        )
        stored: list[StoredImage] = []
        provider_called = False
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
                if effect_claim is not None:
                    from agent.effect_ledger import (
                        assert_effect_dispatchable,
                        mark_effect_submitting,
                    )

                    await mark_effect_submitting(effect_claim)
                    await assert_effect_dispatchable(effect_claim)
                provider_called = True
                provider_operation = _call_provider(
                    target,
                    args,
                    size=size,
                    quality=quality,
                    output_format=output_format,
                    images=images,
                    mask=mask,
                    operation_key=operation_key,
                )
                if effect_claim is not None:
                    from agent.effect_ledger import (
                        run_with_effect_claim_heartbeat,
                    )

                    payloads = await run_with_effect_claim_heartbeat(
                        effect_claim,
                        provider_operation,
                    )
                else:
                    payloads = await provider_operation
                await _assert_image_current(ctx)
            except Exception as exc:
                from agent.driver import LeaseLostError

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
                    if isinstance(exc, LeaseLostError) and not provider_called
                    else "outcome_unknown"
                )
                await _set_reserved_image_status(
                    reservations,
                    next_status,
                    ctx=None,
                    expected={"generating"},
                )
                if isinstance(exc, LeaseLostError):
                    raise
                log.warning("image provider outcome unknown: %s", type(exc).__name__)
                return ToolResult(
                    title=f"Image {mode} outcome unknown",
                    output=(
                        "The image provider request may have been accepted, but no durable provider "
                        "receipt is available. Automatic paid resubmission is disabled."
                    ),
                    metadata={
                        "error": True,
                        "failure_code": "image_provider_outcome_unknown",
                        "operation_key": operation_key,
                        "asset_ids": [row.id for row in reservations],
                        "outcome_unknown": True,
                        "manual_review": True,
                        "do_not_retry": True,
                    },
                )
            if effect_claim is not None:
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
            if len(payloads) != len(reservations):
                await _set_reserved_image_status(
                    reservations,
                    "transfer_failed",
                    ctx=None,
                    expected={"generating"},
                )
                return ToolResult(
                    title="Image provider response incomplete",
                    output="The provider returned a different number of images than were reserved; no paid retry was attempted.",
                    metadata={
                        "error": True,
                        "failure_code": "image_provider_response_incomplete",
                        "operation_key": operation_key,
                        "asset_ids": [row.id for row in reservations],
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
                        state="succeeded",
                        projection={
                            "asset_ids": [item.asset_id for item in stored],
                            "output_count": len(stored),
                        },
                        evidence={"projection": "file_assets_ready"},
                    )
            except Exception as exc:
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
Generate raster images from text, or edit OSS assets and uploads. An optional PNG
mask targets the first input. Results are stored, indexed, and attached
automatically; do not attach them again."""


class _ImageEffectReconciler:
    """Reconcile only the deterministic FileAsset/OSS projection.

    The synchronous image endpoint exposes no queryable provider handle.  A
    complete set of ready deterministic asset rows proves local projection;
    every other state remains manual review and never causes a paid replay.
    """

    can_reconcile_without_handle = True

    async def reconcile(self, effect):
        from agent.effect_ledger import ReconcileDecision
        from db.base import get_db_session
        from db.models.file_asset import FileAsset
        from sqlalchemy import select

        raw_ids = effect.safe_context.get("asset_ids")
        asset_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
        expected = int(effect.safe_context.get("output_count") or len(asset_ids) or 0)
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
        if len(by_id) == len(asset_ids) and all(
            by_id[asset_id].status == "ready" and by_id[asset_id].size > 0
            for asset_id in asset_ids
        ):
            return ReconcileDecision(
                state="succeeded",
                receipt=effect.provider_receipt,
                projection={"asset_ids": asset_ids, "output_count": len(asset_ids)},
                evidence={"projection": "file_assets_ready"},
            )
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
)
