"""Convert one owned IMS result into a reviewed, data-only Skill edition."""
import asyncio
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import exists, func, select
from sqlalchemy.orm import aliased

from assets.service import owned_ready_query
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.session import Session
from db.models.user import User
from db.models.user_skill import UserSkill
from db.models.video_job import VideoJob
from db.models.video_template import VideoTemplateEdition as Edition
from light import example_edit
from light.mode import LightError
from skill import user_library
from skill.archive import validate_skill_zip

PREFIX = "vt_"
AUTHOR_SKILL = Path(__file__).resolve().parents[1] / ".openbox/skills/video-template-author/SKILL.md"
# The pre-existing stage pack stays identical for editions already in use.
STAGE_FILES = ("gallery-open.jpg", "gallery-genie.jpg", "opening.mp4")


def _portable(value: str) -> str:
    if re.search(r"(?i)(?:https?://|file://|/Users/|/Volumes/|\b(?:asset_|session_|oss:)|-----BEGIN |\bBearer\s+|\b(?:token|password|api[_-]?key)\s*[:=])", value):
        raise ValueError("Use creative directions without private file references, links or credentials.")
    return value


class CastImage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    asset_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=150)
    _clean_label = field_validator("label")(_portable)


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    asset_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=2, max_length=88)
    direction: str = Field(min_length=20, max_length=5500)
    reference_pack: Literal["", "gallery-stage"] = ""
    cast_images: list[CastImage] | None = Field(default=None, max_length=6)
    revises_edition_id: str | None = Field(default=None, max_length=64)
    _clean_text = field_validator("title", "direction")(_portable)


def unavailable():
    return LightError("VIDEO_TEMPLATE_NOT_FOUND", "该成片或模板版本不可用。", 404)


def _hash(value: bytes | str) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def _source_query(user_id, workspace_id):
    return (select(FileAsset, VideoJob).join(VideoJob, VideoJob.output_asset_id == FileAsset.id)
            .join(Session, Session.id == VideoJob.session_id)
            .where(FileAsset.user_id == user_id, FileAsset.workspace_id == workspace_id,
                   FileAsset.session_id == VideoJob.session_id, FileAsset.is_deleted.is_(False),
                   FileAsset.status == "ready", FileAsset.mime == "video/mp4", VideoJob.user_id == user_id,
                   VideoJob.kind == "compose", VideoJob.status == "completed", Session.user_id == user_id,
                   Session.workspace_id == workspace_id, Session.is_deleted.is_(False)))


async def _source(db, user_id, workspace_id, asset_id, *, lock=False):
    stmt = _source_query(user_id, workspace_id).where(FileAsset.id == asset_id).order_by(VideoJob.created_at.desc()).limit(1)
    if lock:
        stmt = stmt.with_for_update(of=FileAsset)
    row = (await db.execute(stmt)).first()
    if not row:
        raise unavailable()
    return row


def rhythm(job):
    cut = example_edit.sheet(job.request_data)
    if not cut:
        raise LightError("VIDEO_TEMPLATE_SOURCE_TIMELINE", "成片缺少可读取的剪辑时间线。", 422)
    pattern = {"canvas": cut["canvas"], "seconds": cut.get("seconds"), "cuts": [
        {"seconds": s["sec"], **({"transition": s["transition"]} if s.get("transition") else {})}
        for s in cut["shots"]
    ]}
    captions = cut.get("captions") or {}
    if captions.get("from") is not None and captions.get("to") is not None:
        pattern["caption_rhythm"] = {key: captions[key] for key in ("count", "from", "to", "max_chars")}
    return pattern


async def sources(user_id, workspace_id):
    async with get_db_session() as db:
        rows = (await db.execute(_source_query(user_id, workspace_id).order_by(VideoJob.created_at.desc()).limit(100))).all()
        return [{"asset_id": a.id, "name": a.name} for a, j in rows if example_edit.sheet(j.request_data)]


async def author_preview(user_id, workspace_id, asset_id):
    from core.oss import get_oss
    from light.examples import PREVIEW_SECONDS
    async with get_db_session() as db:
        asset, _ = await _source(db, user_id, workspace_id, asset_id)
    return {"name": asset.name, "url": get_oss().presign_get(asset.oss_key, PREVIEW_SECONDS)}


async def analyze(user_id, workspace_id, asset_id):
    from core.markdown import parse_frontmatter
    from light import example_card
    from tool.tool import ToolContext
    async with get_db_session() as db:
        asset, job = await _source(db, user_id, workspace_id, asset_id)
    output = {"asset_id": asset.id, "oss_key": asset.oss_key, "mime": asset.mime}
    _, guidance = parse_frontmatter(AUTHOR_SKILL.read_text(encoding="utf-8"))
    sample = SimpleNamespace(id=asset.id, entry_id="video", outputs=[output], industry="other", shop_type="other",
                             scene=None, source={}, recipe=guidance, notes="")
    try:
        result = await example_card.read(
            sample, ToolContext(user_id=user_id, workspace_id=workspace_id, session_id=job.session_id),
            edit_sheet=example_edit.sheet(job.request_data),
        )
    except Exception as exc:
        raise LightError("VIDEO_TEMPLATE_ANALYSIS", "成片解析暂未完成，可重试或手写创作说明。", 503) from exc
    return {"direction": result["card"], "rhythm": rhythm(job)}


def _portrait(raw: bytes) -> bytes:
    from PIL import Image, ImageOps
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"} or source.width * source.height > 36_000_000:
                raise ValueError()
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            image.save(out, "JPEG", quality=94)
            return out.getvalue()
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        raise LightError("VIDEO_TEMPLATE_CAST_IMAGE", "角色参考图无法读取，请选择 JPG、PNG 或 WebP 图片。", 422) from exc


async def _cast_files(db, user_id, workspace_id, images):
    from core.oss import get_oss
    files, cast = {}, []
    for index, image in enumerate(images, 1):
        asset = await db.scalar(owned_ready_query(user_id, workspace_id).where(FileAsset.id == image.asset_id))
        if not asset:
            raise unavailable()
        if asset.mime not in {"image/jpeg", "image/png", "image/webp"} or not 0 < asset.size <= 10 * 1024 * 1024:
            raise LightError("VIDEO_TEMPLATE_CAST_IMAGE", "角色参考图支持 10 MB 以内的 JPG、PNG 或 WebP。", 422)
        source = await get_oss().get_object(asset.oss_key)
        if len(source) != asset.size:
            raise LightError("VIDEO_TEMPLATE_CAST_IMAGE", "参考图已更改，请重新选择。", 409)
        data = await asyncio.to_thread(_portrait, source)
        name = f"cast-{index}.jpg"
        cast.append({"file": name, "label": image.label, "sha256": _hash(data)})
        files["references/" + name] = data
    return cast, files


def package(name, manifest, images=None):
    data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    md = (f"---\nname: {name}\ndescription: {json.dumps(manifest['title'], ensure_ascii=False)}\n---\n\n"
          f"# {manifest['title']}\n\n{manifest['direction']}\n\n"
          "使用本次提供的商品图、品牌和文案。节奏和可选的共享参考见 template.json。\n")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        for path, value in (("SKILL.md", md), ("template.json", data), *(images or {}).items()):
            info = zipfile.ZipInfo(path)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, value.encode() if isinstance(value, str) else value)
    raw = archive.getvalue()
    validate_skill_zip(raw)
    return raw


def ref(row):
    return {"template_id": row.template_id, "edition_id": row.id, "version": row.version, "sha256": row.digest}


def _status(row, skill):
    return {"template_ref": ref(row), "source_asset_id": row.source_asset_id,
            "title": row.manifest["title"], "direction": row.manifest["direction"],
            "rhythm": row.manifest["rhythm"], "reference_pack": row.manifest["reference_pack"],
            "cast": row.manifest.get("cast", []), "status": skill.listing if skill.status == "published" else skill.status,
            "catalog_id": "community:" + skill.id}


async def save(user_id, workspace_id, body):
    async with get_db_session() as db:
        # A new cut of the same template can have a different source asset.
        # Serialize the author's version allocation across both source rows.
        await db.scalar(select(User.id).where(User.id == user_id).with_for_update(key_share=True))
        asset, job = await _source(db, user_id, workspace_id, body.asset_id, lock=True)
        manifest = {"format": 1, "title": body.title, "direction": body.direction,
                    "reference_pack": body.reference_pack, "rhythm": rhythm(job)}
        key = _hash(f"{user_id}/{workspace_id}/{asset.id}")[:32]
        family = key
        parent = None
        if body.revises_edition_id:
            parent = await db.scalar(select(Edition).join(UserSkill, UserSkill.id == Edition.skill_id)
                      .where(Edition.id == body.revises_edition_id, UserSkill.owner_id == user_id,
                             UserSkill.workspace_id == workspace_id))
            if not parent:
                raise unavailable()
            family = parent.template_id
        if parent and body.cast_images is None and parent.manifest.get("cast"):
            previous = await db.get(UserSkill, parent.skill_id)
            archive = (previous.published_archive_data if previous.published_archive_sha256 == parent.digest
                       else previous.archive_data)
            if not archive or _hash(archive) != parent.digest:
                raise LightError("VIDEO_TEMPLATE_CHANGED", "请重新选择此版的角色参考图。", 409)
            cast = parent.manifest["cast"]
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                images = {"references/" + p["file"]: z.read("references/" + p["file"]) for p in cast}
        else:
            cast, images = await _cast_files(db, user_id, workspace_id, body.cast_images or [])
        if cast:
            manifest["cast"] = cast
        source_identity = family if family == key else family + "/" + key
        edition_id = PREFIX + _hash(source_identity + json.dumps(manifest, ensure_ascii=False, sort_keys=True))[:40]
        existing = await db.get(Edition, edition_id)
        if existing is not None:
            return _status(existing, await db.get(UserSkill, existing.skill_id))
        slug = "video-template-" + edition_id[len(PREFIX):]
        raw = package(slug, manifest, images)
        owned = await user_library.upsert_personal_snapshot(user_id, {"name": slug, "description": body.title,
                  "files": ["SKILL.md", "template.json", *images], "tags": ["video-template"]}, raw, workspace_id=workspace_id)
        version = 1 + (await db.scalar(select(func.max(Edition.version)).where(Edition.template_id == family)) or 0)
        edition = Edition(id=edition_id, template_id=family, version=version, skill_id=owned["id"],
                          source_asset_id=asset.id, digest=_hash(raw), manifest=manifest,
                          created_at=datetime.now(timezone.utc))
        db.add(edition)
        await db.flush()
        from db.models.media_template import MediaTemplateImport
        db.add(MediaTemplateImport(id=edition.id, video_edition_id=edition.id))
        return _status(edition, await db.get(UserSkill, edition.skill_id))


async def publish(user_id, workspace_id, edition_id, digest, *, admin=False, share_cast=False):
    from core.config import get_config
    from core.oss import get_oss
    async with get_db_session() as db:
        row = await db.scalar(select(Edition).where(Edition.id == edition_id).with_for_update())
        if row is None:
            raise unavailable()
        skill = await db.get(UserSkill, row.skill_id)
        if skill.owner_id != user_id or skill.workspace_id != workspace_id:
            raise unavailable()
        if digest != row.digest or skill.archive_sha256 != row.digest:
            raise LightError("VIDEO_TEMPLATE_CHANGED", "请刷新后核对要发布的版本。", 409)
        if skill.status == "published" and skill.published_archive_sha256 == digest and row.preview_key:
            return _status(row, skill)
        cast = row.manifest.get("cast") or []
        if cast and not share_cast:
            raise LightError("VIDEO_TEMPLATE_CAST_CONSENT", "请确认分享所选的角色参考图。", 422)
        asset, _ = await _source(db, user_id, workspace_id, row.source_asset_id)
        key = f"video-templates/{row.id}/preview.mp4"
        if cast:
            with zipfile.ZipFile(io.BytesIO(skill.archive_data)) as archive:
                for portrait in cast:
                    name = portrait["file"]
                    data = archive.read("references/" + name)
                    if _hash(data) != portrait["sha256"]:
                        raise LightError("VIDEO_TEMPLATE_CHANGED", "角色参考图与此版本不一致。", 409)
                    await get_oss().put_object(f"video-templates/{row.id}/references/{name}", data,
                                               content_type="image/jpeg", forbid_overwrite=True)
        if not row.preview_key:
            copied = await get_oss().copy(asset.oss_key, key)
            if not copied or copied["size"] != asset.size:
                raise LightError("VIDEO_TEMPLATE_PREVIEW", "样片复制失败，可以重试本次提交。", 503)
            row.preview_key = key
        # The signed preview and reviewed Skill move together in this transaction.
        await db.refresh(skill, with_for_update=True)
        if skill.archive_sha256 != digest:
            raise LightError("VIDEO_TEMPLATE_CHANGED", "请刷新后核对要发布的版本。", 409)
        await user_library.publish_locked_snapshot(
            db, skill, review_required=get_config().skill_store_review, publisher_is_admin=admin,
        )
        return _status(row, skill)


def _visible():
    # Pending or rejected edits leave the current edition selectable. Once a
    # newer edition reached the shelf, withdrawing it must not revive V1.
    newer, newer_skill = aliased(Edition), aliased(UserSkill)
    superseded = exists(select(newer.id).join(newer_skill, newer_skill.id == newer.skill_id).where(
        newer.template_id == Edition.template_id, newer.version > Edition.version,
        newer_skill.published_archive_sha256 == newer.digest,
        newer_skill.listing.in_([user_library.LISTING_LISTED, user_library.LISTING_DELISTED]),
        newer.preview_key.is_not(None),
    ))
    return (select(Edition).join(UserSkill, UserSkill.id == Edition.skill_id).join(User, User.id == UserSkill.owner_id)
            .where(user_library.store_visible(), User.is_active.is_(True), User.is_deleted.is_(False),
                   UserSkill.published_archive_sha256 == Edition.digest, Edition.preview_key.is_not(None),
                   ~superseded))


async def resolve(db, edition_id):
    row = await db.scalar(_visible().where(Edition.id == edition_id))
    if row is None:
        raise unavailable()
    return row


def card(row):
    from core.oss import get_oss
    from light.examples import PREVIEW_SECONDS, PREVIEW_WIDTH
    oss = get_oss()
    return {"example_id": row.id, "title": row.manifest["title"], "mime": "video/mp4",
            "template_ref": ref(row), "scene": "community", "scene_label": "创作者模板", "tags": [],
            "media_url": oss.presign_get(row.preview_key, PREVIEW_SECONDS),
            "poster_url": oss._presign("GET", row.preview_key, PREVIEW_SECONDS,
                subresource={"x-oss-process": f"video/snapshot,t_1000,f_jpg,w_{PREVIEW_WIDTH},m_fast,ar_auto"})}


async def catalogue():
    async with get_db_session() as db:
        rows = (await db.scalars(_visible().order_by(Edition.created_at.desc()))).all()
    return [card(row) for row in rows]


async def selected_card(edition_id):
    async with get_db_session() as db:
        return card(await resolve(db, edition_id))


async def owned(user_id, workspace_id):
    async with get_db_session() as db:
        rows = await db.execute(select(Edition, UserSkill).join(UserSkill, UserSkill.id == Edition.skill_id)
                                .where(UserSkill.owner_id == user_id, UserSkill.workspace_id == workspace_id)
                                .order_by(Edition.created_at.desc()).limit(100))
        return [_status(row, skill) for row, skill in rows]


async def references(edition_id, ctx):
    from core.oss import get_oss
    from tool.video_template import _asset
    async with get_db_session() as db:
        session = await db.get(Session, ctx.session_id)
        form = (session.light_production or {}) if session else {}
        pin = form.get("template_ref") or {}
        if (not session or session.user_id != ctx.user_id or session.workspace_id != ctx.workspace_id
                or session.is_deleted or pin.get("edition_id") != edition_id):
            raise unavailable()
        # Both values were written by resolve() at selection, under the
        # production row lock. The buyer can resume after author withdrawal.
        recipe = form.get("template_recipe")
        if not recipe:
            raise unavailable()
    result = {"template_ref": pin, "rhythm": recipe["rhythm"], "references": {}}
    if recipe["reference_pack"] == "gallery-stage":
        for name in STAGE_FILES:
            result["references"][name] = await _asset(name, ctx, ctx.project_id, oss=get_oss())
    for portrait in recipe.get("cast", []):
        name = portrait["file"]
        oss = get_oss()
        raw = await oss.get_object(f"video-templates/{edition_id}/references/{name}")
        if _hash(raw) != portrait["sha256"]:
            raise LightError("VIDEO_TEMPLATE_CHANGED", "角色参考图与选定版本不一致。", 409)
        asset = await _asset(name, ctx, ctx.project_id, oss=oss, data=raw)
        result["references"][name] = asset
        result.setdefault("cast", []).append({"label": portrait["label"], "asset_id": asset})
    return result


async def is_legacy_skill(skill_id: str) -> bool:
    async with get_db_session() as db:
        return bool(await db.scalar(select(Edition.id).where(Edition.skill_id == skill_id)))


async def legacy_skill_names(user_id: str, workspace_id: str | None) -> set[str]:
    """Names of the author's imported media packages, including live install keys."""
    scope = [UserSkill.workspace_id == workspace_id] if workspace_id else []
    async with get_db_session() as db:
        rows = (await db.execute(
            select(UserSkill.id, UserSkill.name, UserSkill.install_dir)
            .join(Edition, Edition.skill_id == UserSkill.id)
            .where(UserSkill.owner_id == user_id, *scope)
        )).all()
    return {key for row in rows for key in row if key}
