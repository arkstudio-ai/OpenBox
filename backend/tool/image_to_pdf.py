"""image_to_pdf: wrap a generated image (a menu, a poster) into a one-page PDF.

A restaurant prints its menu at the copy shop, and the copy shop wants a PDF,
not a 2048×2896 PNG that comes out of the printer at the wrong size. The
image already lives in OSS as an owned ``file_assets`` row; this tool pulls
the bytes, lays them on an A4 page (or a page the image's own size), stores
the PDF as a new asset and attaches it to the reply next to the image —
same group as the picture, so the result card shows both. No sandbox, no
upstream call, nothing to bill.

The PDF is written by hand rather than through Pillow's PDF encoder: a single
page with one DCT-encoded XObject is a few dozen bytes of syntax, and
writing it ourselves keeps the JPEG quality under our control (dense menu
text at the encoder's default 75 looks smeared in print).
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from agent.driver import LeaseLostError
from core.log import create_logger
from question.runtime import RunRevoked
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.image_to_pdf")

#: A4 in PostScript points (595.276 × 841.89).
A4_PORTRAIT = (595.276, 841.890)
#: Generated images are at most a few tens of MB; anything larger is not a
#: picture we drew.
_MAX_INPUT_BYTES = 48 * 1024 * 1024
#: When the image's aspect ratio is this close to A4 it is an A4 design and
#: the page is A4; otherwise the page takes the image's own proportions.
_A4_RATIO_TOLERANCE = 0.04
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}
_JPEG_QUALITY = 92
_IMAGE_DPI = 200


class ImageToPdfArgs(BaseModel):
    asset_id: str = Field(
        max_length=512,
        description=(
            "asset_id of a generated or uploaded image the user owns (an image_gen "
            "result's asset_id). The PDF is made from that image as it is."
        ),
    )
    paper: Literal["auto", "a4", "image"] = Field(
        default="auto",
        description=(
            "Page size. 'a4': an A4 page (portrait or landscape to match the image), "
            "the image scaled to fit and centred — use for 打印. 'image': the page has "
            "the image's own proportions. 'auto' (default) picks A4 when the image was "
            "drawn in A4 proportions and 'image' otherwise."
        ),
    )
    name: str | None = Field(
        default=None,
        max_length=120,
        description="File name for the PDF, e.g. 菜单.pdf. Defaults to the image's name with .pdf.",
    )


def _safe_name(requested: str | None, fallback_stem: str) -> str:
    raw = PurePosixPath((requested or "").strip()).name
    stem = re.sub(r"\.pdf$", "", raw, flags=re.IGNORECASE)
    stem = re.sub(r"[^\w.一-鿿-]", "_", stem).strip("._")
    if not stem:
        stem = re.sub(r"\.(png|jpe?g|webp)$", "", fallback_stem, flags=re.IGNORECASE) or "document"
    return f"{stem[:80]}.pdf"


def _pdf_escape_name(name: str) -> bytes:
    """A PDF literal string for the Title entry (UTF-16BE with BOM)."""
    return b"<" + ("﻿" + name).encode("utf-16-be").hex().encode() + b">"


def page_box(width_px: int, height_px: int, paper: str) -> tuple[tuple[float, float], tuple[float, float, float, float]]:
    """Return ``((page_w, page_h), (x, y, draw_w, draw_h))`` in points.

    ``paper`` is resolved here: 'auto' becomes 'a4' when the image's aspect
    ratio is within tolerance of A4 (either orientation), else 'image'.
    """
    landscape = width_px > height_px
    ratio = width_px / height_px
    a4_ratio = (A4_PORTRAIT[1] / A4_PORTRAIT[0]) if landscape else (A4_PORTRAIT[0] / A4_PORTRAIT[1])
    if paper == "auto":
        paper = "a4" if abs(ratio - a4_ratio) / a4_ratio <= _A4_RATIO_TOLERANCE else "image"
    if paper == "image":
        page_w = width_px * 72 / _IMAGE_DPI
        page_h = height_px * 72 / _IMAGE_DPI
        return (page_w, page_h), (0.0, 0.0, page_w, page_h)
    page_w, page_h = (A4_PORTRAIT[1], A4_PORTRAIT[0]) if landscape else A4_PORTRAIT
    scale = min(page_w / width_px, page_h / height_px)
    draw_w, draw_h = width_px * scale, height_px * scale
    return (page_w, page_h), ((page_w - draw_w) / 2, (page_h - draw_h) / 2, draw_w, draw_h)


def build_pdf(jpeg: bytes, width_px: int, height_px: int, *, paper: str, title: str) -> tuple[bytes, str]:
    """One page, one JPEG XObject. Returns the bytes and the resolved paper."""
    (page_w, page_h), (x, y, draw_w, draw_h) = page_box(width_px, height_px, paper)
    resolved = "a4" if (page_w, page_h) in (A4_PORTRAIT, (A4_PORTRAIT[1], A4_PORTRAIT[0])) else "image"
    content = f"q {draw_w:.3f} 0 0 {draw_h:.3f} {x:.3f} {y:.3f} cm /Im0 Do Q".encode()
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.3f} {page_h:.3f}] "
            f"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>"
        ).encode(),
        (
            f"<< /Type /XObject /Subtype /Image /Width {width_px} /Height {height_px} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n"
        ).encode() + jpeg + b"\nendstream",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
        b"<< /Title " + _pdf_escape_name(title) + b" /Producer (bossip image_to_pdf) >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue(), resolved


def render_jpeg(data: bytes) -> tuple[bytes, int, int]:
    """Decode any supported image to an RGB JPEG; alpha is flattened on white."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            rgb = flat
        else:
            rgb = image.convert("RGB")
        buffer = io.BytesIO()
        rgb.save(buffer, "JPEG", quality=_JPEG_QUALITY, optimize=True)
        return buffer.getvalue(), rgb.width, rgb.height


async def _download(oss, row) -> bytes:
    import httpx

    url = oss.presign_get(row.oss_key, expires_sec=300)
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > _MAX_INPUT_BYTES:
                    raise ValueError("image is too large to convert")
    return bytes(data)


async def _source_group_id(ctx: ToolContext, asset_id: str) -> str | None:
    """The reply group the image was attached under, so the PDF joins its card."""
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.part import Part

    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(Part.data)
                .where(Part.session_id == ctx.session_id, Part.user_id == ctx.user_id, Part.type == "file")
                .order_by(Part.created_at.desc())
                .limit(200)
            )
        ).scalars().all()
    for data in rows:
        if isinstance(data, dict) and data.get("asset_id") == asset_id:
            relation = data.get("relation") or {}
            group = relation.get("group_id") if isinstance(relation, dict) else None
            return group or None
    return None


async def execute(args: ImageToPdfArgs, ctx: ToolContext) -> ToolResult:
    from core.oss import OssNotConfigured, get_oss
    from tool.asset_delivery import find_owned_ready_asset

    if not ctx.message_id:
        return ToolResult(title="Nothing to attach to", output="This call has no reply to attach the PDF to.",
                          metadata={"error": True})
    try:
        oss = get_oss()
    except OssNotConfigured as e:
        return ToolResult(title="image_to_pdf unavailable", output=f"OSS transfer is not configured: {e}",
                          metadata={"error": True})

    source = await find_owned_ready_asset(args.asset_id, ctx)
    if source is None:
        return ToolResult(title="Asset not available",
                          output="No ready asset owned by this user matches that asset_id.",
                          metadata={"error": True})
    if (source.mime or "") not in _IMAGE_MIMES:
        return ToolResult(title=f"Not an image: {source.name}",
                          output=f"{source.name} is {source.mime or 'an unknown type'}; only PNG, JPEG or WebP images can be exported.",
                          metadata={"error": True, "asset_id": source.id})

    name = _safe_name(args.name, source.name)
    try:
        data = await _download(oss, source)
        jpeg, width, height = render_jpeg(data)
        pdf, paper = build_pdf(jpeg, width, height, paper=args.paper, title=name[:-4])
    except (RunRevoked, LeaseLostError):
        raise
    except Exception as e:
        log.warning("image_to_pdf conversion failed", exc_info=True)
        return ToolResult(title=f"Could not convert {source.name}", output=f"{type(e).__name__}: {str(e)[:300]}",
                          metadata={"error": True, "asset_id": source.id})

    try:
        asset = await _store(ctx, oss, name, pdf, source_asset_id=source.id)
    except (RunRevoked, LeaseLostError):
        raise
    except Exception as e:
        log.warning("image_to_pdf storage failed", exc_info=True)
        return ToolResult(title=f"Could not store {name}", output=f"{type(e).__name__}: {str(e)[:300]}",
                          metadata={"error": True, "asset_id": source.id})

    group_id = await _source_group_id(ctx, source.id) or (f"tool:{ctx.part_id}" if ctx.part_id else asset.id)
    attached = await _attach(ctx, asset, group_id=group_id, source_asset_id=source.id, paper=paper)
    pages = "A4" if paper == "a4" else f"{width}×{height} px"
    return ToolResult(
        title=name,
        output=(
            f"attached={'true' if attached else 'false'}\nasset_id={asset.id}\nname={name}\n"
            f"paper={paper} ({pages})\nbytes={len(pdf)}\nsource_asset_id={source.id}\n"
            + ("The PDF is now a card on this reply next to the image; the user downloads it from the card. "
               "Do not write a link or URL for it."
               if attached else
               "The PDF is stored but could not be attached to this reply; re-send it with share_file asset_id.")
        ),
        metadata={"asset_id": asset.id, "mime": "application/pdf", "size": len(pdf), "paper": paper,
                  "source_asset_id": source.id, "attached": attached},
    )


async def _upload(oss, key: str, pdf: bytes) -> None:
    import httpx

    url = oss.presign_put(key, "application/pdf", expires_sec=600)
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.put(url, content=pdf, headers={"Content-Type": "application/pdf"})
        response.raise_for_status()


async def _store(ctx: ToolContext, oss, name: str, pdf: bytes, *, source_asset_id: str):
    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from sandbox.assets import _session_project
    from trajectory.artifacts import capture_result_asset_in_tx

    asset_id = ascending("asset")
    key = f"assets/{ctx.user_id}/{asset_id}/{name}"
    await _upload(oss, key, pdf)
    try:
        async with get_db_session() as db:
            project_id = ctx.project_id or await _session_project(db, ctx.session_id, ctx.user_id)
            asset = FileAsset(
                id=asset_id,
                user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                session_id=ctx.session_id,
                project_id=project_id or None,
                name=name,
                oss_key=key,
                mime="application/pdf",
                size=len(pdf),
                status="ready",
                source="agent",
                transient=False,
                created_at=datetime.now(timezone.utc),
            )
            db.add(asset)
            await db.flush()
            await capture_result_asset_in_tx(db, ctx, asset)
            await db.commit()
    except Exception:
        try:
            await oss.delete(key)
        except Exception:
            log.warning("failed to clean up an OSS pdf after database failure", exc_info=True)
        raise
    return asset


async def _attach(ctx: ToolContext, asset, *, group_id: str, source_asset_id: str, paper: str) -> bool:
    from models.message import FilePart, FileRelation
    from project.workspace import asset_sandbox_path
    from session.session import save_part

    path = asset_sandbox_path(ctx.user_id, ctx.project_id or None, asset.name, asset_id=asset.id)
    try:
        await save_part(
            FilePart(
                path=path,
                mime_type="application/pdf",
                asset_id=asset.id,
                oss_key=asset.oss_key,
                size=asset.size,
                transient=False,
                relation=FileRelation(
                    source_part_id=ctx.part_id or None,
                    group_id=group_id,
                    role="result",
                    kind="exported_pdf",
                    label=asset.name,
                    metadata={"source_asset_id": source_asset_id, "paper": paper},
                ),
                session_id=ctx.session_id,
                message_id=ctx.message_id,
            ),
            is_new=True,
            user_id=ctx.user_id,
            run_fence=ctx.run_fence,
        )
    except (RunRevoked, LeaseLostError):
        raise
    except Exception:
        log.warning("pdf attachment failed", exc_info=True)
        return False
    return True


IMAGE_TO_PDF_DESCRIPTION = """\
Export an image the user owns (an image_gen result: a menu, a poster, a price \
list) as a one-page PDF for printing or sending to a copy shop. Pass the \
image's asset_id; paper='a4' puts it on an A4 page (打印), 'image' keeps the \
image's own proportions, 'auto' decides from the image. The PDF is stored as \
a new asset and attached to this reply next to the image. Needs no sandbox, \
calls no model and costs no credits. Does not edit the image."""

image_to_pdf_tool = define_tool(
    "image_to_pdf",
    description=IMAGE_TO_PDF_DESCRIPTION,
    parameters=ImageToPdfArgs,
    execute=execute,
    sandbox_required=False,
    discovery_hint="export an image (menu, poster) as a printable A4 PDF; 导出 PDF / 打印菜单",
)
