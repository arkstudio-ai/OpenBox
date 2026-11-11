"""image_to_pdf: an owned image becomes a one-page PDF card next to the image."""
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from pypdf import PdfReader

import tool.image_to_pdf as module
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.part import Part
from db.repository.user_repo import PgUserRepo
from tool.image_to_pdf import ImageToPdfArgs, build_pdf, image_to_pdf_tool, page_box, render_jpeg
from tool.tool import ToolContext


def _png(width, height, mode="RGBA"):
    buffer = io.BytesIO()
    Image.new(mode, (width, height), (200, 30, 30, 255) if mode == "RGBA" else (200, 30, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def test_auto_paper_is_a4_only_for_a4_proportions():
    assert page_box(2048, 2896, "auto")[0] == module.A4_PORTRAIT
    assert page_box(2896, 2048, "auto")[0] == (module.A4_PORTRAIT[1], module.A4_PORTRAIT[0])
    (page_w, page_h), (x, y, draw_w, draw_h) = page_box(1536, 1024, "auto")
    assert (page_w, page_h) == (draw_w, draw_h) and (x, y) == (0.0, 0.0)
    # Forced A4 centres a 3:2 table card on a landscape page.
    (page_w, page_h), (x, y, draw_w, draw_h) = page_box(1536, 1024, "a4")
    assert (page_w, page_h) == (module.A4_PORTRAIT[1], module.A4_PORTRAIT[0])
    assert x == 0.0 and y > 0 and abs(draw_w - page_w) < 1e-6


def test_pdf_round_trips_through_pypdf_with_the_image_intact():
    jpeg, width, height = render_jpeg(_png(2048, 2896))
    pdf, paper = build_pdf(jpeg, width, height, paper="auto", title="老王菜单")
    reader = PdfReader(io.BytesIO(pdf))
    [page] = reader.pages
    assert paper == "a4"
    assert (round(float(page.mediabox.width), 3), round(float(page.mediabox.height), 3)) == (595.276, 841.89)
    assert reader.metadata.title == "老王菜单"
    [image] = page.images
    assert image.image.size == (2048, 2896)
    assert image.image.getpixel((10, 10))[0] > 150  # red, alpha flattened rather than dropped


def test_safe_name_keeps_chinese_and_strips_paths():
    assert module._safe_name("老王菜单.PDF", "x.png") == "老王菜单.pdf"
    assert module._safe_name("../a b.pdf", "x.png") == "a_b.pdf"
    assert module._safe_name(None, "generated-asset_1.png") == "generated-asset_1.pdf"


async def _user():
    uid = uuid4().hex
    return await PgUserRepo().create(id=uid, username="pdf" + uid[:8], password_hash=None)


async def _image_asset(user, *, mime="image/png", with_part=True):
    asset_id = f"asset_{uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_db_session() as db:
        db.add(FileAsset(
            id=asset_id, user_id=user["id"], workspace_id=user["default_workspace_id"], session_id="s_pdf",
            name="generated-menu.png", oss_key=f"assets/{user['id']}/{asset_id}/generated-menu.png",
            mime=mime, size=10, status="ready", source="agent", transient=False, created_at=now,
        ))
        if with_part:
            db.add(Part(
                id=f"part_{uuid4().hex[:12]}", message_id="m_pdf", session_id="s_pdf", user_id=user["id"],
                type="file", created_at=now,
                data={"type": "file", "asset_id": asset_id, "mime_type": mime,
                      "relation": {"group_id": "tool:call_image", "role": "result", "kind": "generated_image"}},
            ))
        await db.commit()
    return asset_id


@pytest.fixture
def transport(monkeypatch):
    """OSS and HTTP stay out of the test: the bytes go through these fakes."""
    uploads = {}

    async def fake_download(oss, row):
        return _png(2048, 2896)

    async def fake_upload(oss, key, pdf):
        uploads[key] = pdf

    oss = SimpleNamespace(presign_get=lambda *a, **k: "https://oss/get", delete=lambda key: None)
    monkeypatch.setattr("core.oss.get_oss", lambda: oss)
    monkeypatch.setattr(module, "_download", fake_download)
    monkeypatch.setattr(module, "_upload", fake_upload)
    saved = []

    async def fake_save_part(part, is_new=False, *, user_id, run_fence=None):
        saved.append((part, is_new, user_id))

    monkeypatch.setattr("session.session.save_part", fake_save_part)
    return SimpleNamespace(uploads=uploads, saved=saved)


def _ctx(user, **over):
    base = dict(session_id="s_pdf", user_id=user["id"], workspace_id=user["default_workspace_id"],
                message_id="m_pdf", part_id="call_pdf")
    base.update(over)
    return ToolContext(**base)


@pytest.mark.asyncio
async def test_execute_stores_the_pdf_and_joins_the_image_card(transport):
    user = await _user()
    image_id = await _image_asset(user)

    result = await image_to_pdf_tool.execute(
        {"asset_id": image_id, "paper": "a4", "name": "老王菜单.pdf"}, _ctx(user))

    assert not result.metadata.get("error"), result.output
    pdf_id = result.metadata["asset_id"]
    assert result.metadata["paper"] == "a4" and result.metadata["attached"] is True
    assert "http" not in result.output and "token=" not in result.output
    [(key, pdf)] = transport.uploads.items()
    assert key == f"assets/{user['id']}/{pdf_id}/老王菜单.pdf" and pdf.startswith(b"%PDF-1.4")
    async with get_db_session() as db:
        row = await db.get(FileAsset, pdf_id)
    assert row.mime == "application/pdf" and row.status == "ready" and row.size == len(pdf)
    assert row.session_id == "s_pdf" and row.source == "agent"
    [(part, is_new, user_id)] = transport.saved
    assert is_new is True and user_id == user["id"]
    assert part.asset_id == pdf_id and part.mime_type == "application/pdf" and part.message_id == "m_pdf"
    assert part.relation.group_id == "tool:call_image" and part.relation.kind == "exported_pdf"
    assert part.relation.metadata == {"source_asset_id": image_id, "paper": "a4"}
    assert part.path.endswith("老王菜单.pdf")


@pytest.mark.asyncio
async def test_execute_without_an_image_part_uses_its_own_group(transport):
    user = await _user()
    image_id = await _image_asset(user, with_part=False)

    result = await image_to_pdf_tool.execute({"asset_id": image_id, "paper": "image"}, _ctx(user))

    assert not result.metadata.get("error"), result.output
    [(part, _, _)] = transport.saved
    assert part.relation.group_id == "tool:call_pdf"
    assert result.metadata["paper"] == "image" and part.path.endswith("generated-menu.pdf")


@pytest.mark.asyncio
async def test_execute_refuses_other_users_and_non_images(transport):
    user = await _user()
    other = await _user()
    image_id = await _image_asset(user)
    video_id = await _image_asset(user, mime="video/mp4")

    foreign = await image_to_pdf_tool.execute({"asset_id": image_id}, _ctx(other))
    assert foreign.metadata["error"] is True and "No ready asset" in foreign.output
    video = await image_to_pdf_tool.execute({"asset_id": video_id}, _ctx(user))
    assert video.metadata["error"] is True and "only PNG, JPEG or WebP" in video.output
    assert not transport.uploads and not transport.saved


@pytest.mark.asyncio
async def test_storage_failure_removes_the_uploaded_pdf(transport, monkeypatch):
    user = await _user()
    image_id = await _image_asset(user)
    deleted = []

    async def fail_capture(db, ctx, asset, *, request_id=None):
        raise RuntimeError("db down")

    monkeypatch.setattr("trajectory.artifacts.capture_result_asset_in_tx", fail_capture)

    async def record_delete(key):
        deleted.append(key)
        return True

    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(
        presign_get=lambda *a, **k: "https://oss/get", delete=record_delete))

    result = await image_to_pdf_tool.execute({"asset_id": image_id}, _ctx(user))

    assert result.metadata["error"] is True and result.title.startswith("Could not store")
    assert deleted == list(transport.uploads) and not transport.saved


def test_args_reject_unknown_paper():
    with pytest.raises(ValueError):
        ImageToPdfArgs(asset_id="asset_1", paper="letter")
