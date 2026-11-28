"""The media library, author trials and publication decisions."""

import hashlib
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import JSON, and_, func, or_, select, type_coerce, update

from assets.service import owned_ready_query
from core.identifier import ascending
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.light_example import LightExample
from db.models.media_template import (
    MediaTemplate as Family, MediaTemplateEdition as Edition, MediaTemplateImport as Import,
    MediaTemplateReview as Review, MediaTemplateSubmission as Submission, MediaTemplateTrial as Trial,
)
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from db.models.user import User
from db.models.user_skill import UserSkill
from db.models.video_job import VideoJob
from db.models.video_template import VideoTemplateEdition
from db.models.workspace import Workspace
from light import examples, taxonomy, video_templates
from light.mode import LightError
from light.production_form import EntryId, RETIRED_VIDEO_TEMPLATES

PREFIX = "mt_"
BUILTIN_PREFIX = "mtb_"
# Saved productions still resolve the titles of retired built-ins.
_BUILTIN_VIDEO = {
    "gallery_wishes": ("画廊里的三个愿望", None),
    "product_story": ("品牌剧情广告", "headphone-ad"),
    "shortcut_story": ("先一步做到", "shortcut-story"),
    "silk_road": ("丝路留香", "silk-road"),
    "soda_story": ("苏打水广告", "soda-story"),
    "twist": ("这单谁买", "crawfish-remake"),
}
BUILTIN_VIDEO = {key: card for key, card in _BUILTIN_VIDEO.items() if key not in RETIRED_VIDEO_TEMPLATES}
_MEDIA_FIELDS = (
    "shop", "brief", "script", "product_name", "poster_title", "dishes", "reference_copy", "notes",
    "keep_original", "promotion_brief", "output_language", "perfect_film", "video_tier", "music_style",
    "beat_music", "redo_narration", "creative_effects",
)


def now():
    return datetime.now(timezone.utc)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    entry_id: EntryId
    asset_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=2, max_length=120)
    recipe: str = Field(min_length=20, max_length=12000)
    revises_edition_id: str | None = Field(default=None, max_length=64)
    _clean = field_validator("title", "recipe")(video_templates._portable)


def missing():
    return LightError("MEDIA_TEMPLATE_NOT_FOUND", "模板、会话或成片不可用。", 404)


def conflict(message: str):
    return LightError("MEDIA_TEMPLATE_STATE", message, 409)


def _edition():
    return select(Edition, Family).join(Family, Family.id == Edition.template_id)


async def owned(db, user_id, workspace_id, edition_id, *, lock=False):
    statement = _edition().where(Edition.id == edition_id, Family.owner_id == user_id,
                                 Family.workspace_id == workspace_id)
    result = (await db.execute(statement.with_for_update(of=Family) if lock else statement)).first()
    if not result:
        raise missing()
    edition, family = result
    if lock:
        # Refresh after taking the family lock to see any concurrent review.
        await db.refresh(edition)
    return edition, family


async def _require_current_draft(db, edition):
    if edition.status != "draft":
        raise conflict("只有草稿可以开始或确认试做。")
    if await db.scalar(select(Edition.id).where(
        Edition.template_id == edition.template_id, Edition.version > edition.version,
    ).limit(1)):
        raise conflict("请使用此模板的最新版本重新试做。")


def _public_query():
    return (_edition().join(User, User.id == Family.owner_id).join(Workspace, Workspace.id == Family.workspace_id)
            .where(Edition.status == "listed", User.is_active.is_(True), User.is_deleted.is_(False),
                   Workspace.is_deleted.is_(False), Edition.preview_key.is_not(None)))


async def public_edition(db, entry_id, edition_id):
    result = (await db.execute(_public_query().where(Edition.id == edition_id, Family.entry_id == entry_id))).first()
    if not result:
        raise missing()
    return result


def pin(edition):
    return {"id": edition.id, "title": edition.title, "version": edition.version, "recipe": edition.recipe}


def _media_matches(entry, mime):
    return mime.startswith("image/" if entry in {"menu", "poster"} else "video/")


async def _pro_source(db, user_id, workspace_id, session_id, asset_id, entry_id):
    from light.production import _views
    from light.production_outputs import advanced_result_messages

    session = await db.scalar(select(Session).where(
        Session.id == session_id, Session.user_id == user_id, Session.workspace_id == workspace_id,
        Session.is_deleted.is_(False), Session.kind == "normal",
    ))
    asset = await db.scalar(owned_ready_query(user_id, workspace_id).where(
        FileAsset.id == asset_id, FileAsset.source == "agent", FileAsset.transient.is_(False),
    ))
    if not session or not asset or not _media_matches(entry_id, asset.mime) or asset.session_id != session.id:
        raise missing()
    # Advanced customization keeps the guided session ID. Only results of
    # an advanced turn can be extracted from those conversations.
    appearance = select(Part).join(Message, Message.id == Part.message_id).where(
        Part.session_id == session.id, Part.user_id == user_id, Part.type == "file",
        Message.role == "assistant", type_coerce(Part.data, JSON)["asset_id"].as_string() == asset.id,
    )
    parts = (await db.scalars(appearance)).all()
    if session.light_production:
        marks = await advanced_result_messages(db, [session], {session.id: parts}, {})
        if (session.light_production.get("media_template_trial") or not marks or
                not any(p.message_id in marks for p in parts)):
            raise missing()
        if (await _views(db, [session]))[0]["status"] != "completed":
            raise conflict("请先在专业版对话里完成作品。")
    if not parts:
        raise conflict("请先把已完成的作品发到这条专业版对话。")
    if entry_id in {"video", "footage_edit"}:
        composed = await db.scalar(select(VideoJob.id).where(
            VideoJob.session_id == session.id, VideoJob.user_id == user_id,
            VideoJob.output_asset_id == asset.id, VideoJob.kind.in_(("compose", "render")),
            VideoJob.status == "completed",
        ).limit(1))
        final_attachment = any((part.data.get("relation") or {}).get("role") in {"final", "result"} for part in parts)
        if not composed and not final_attachment:
            raise conflict("请选择对话中已交付的成片。")
    return asset


async def save(user_id, workspace_id, session_id, body: Draft):
    async with get_db_session() as db:
        source = await _pro_source(db, user_id, workspace_id, session_id, body.asset_id, body.entry_id)
        await db.scalar(select(User.id).where(User.id == user_id).with_for_update())
        if body.revises_edition_id:
            _, family = await owned(db, user_id, workspace_id, body.revises_edition_id, lock=True)
            if body.entry_id != family.entry_id:
                raise conflict("模板版本须沿用原有一级分类。")
        else:
            # Retries (including an Agent tool replay) find the same family.
            identity = "\0".join((user_id, workspace_id, source.id, body.entry_id, body.title))
            key = "mf_" + hashlib.sha256(identity.encode()).hexdigest()[:40]
            family = await db.get(Family, key, with_for_update=True)
            if not family:
                family = Family(id=key, owner_id=user_id, workspace_id=workspace_id, entry_id=body.entry_id)
                db.add(family)
                await db.flush()
        latest = await db.scalar(select(Edition).where(Edition.template_id == family.id)
                                .order_by(Edition.version.desc()).limit(1))
        if latest and (latest.source_asset_id, latest.title, latest.recipe) == (source.id, body.title, body.recipe):
            return item(latest, family, private=True)
        if latest and latest.status == "pending":
            raise conflict("当前版本正在审核，请等待审核结果后再修订。")
        edition = Edition(id=ascending("mt"), template_id=family.id, version=latest.version + 1 if latest else 1,
                          source_session_id=session_id, source_asset_id=source.id, title=body.title,
                          recipe=body.recipe, status="draft", created_at=now())
        db.add(edition)
        await db.flush()
        return item(edition, family, private=True)


def _attachment(asset, *, public_key=None, mime=None, name=None):
    from core.oss import OssNotConfigured, get_oss
    key = public_key or (asset.oss_key if asset and not asset.is_deleted else None)
    if not key:
        return None
    mime = mime or asset.mime
    payload = {"mime": mime, "name": name or (asset.name if asset else ""), "media_url": None, "poster_url": None}
    try:
        oss = get_oss()
    except OssNotConfigured:
        return payload
    payload["media_url"] = oss.presign_get(key, examples.PREVIEW_SECONDS)
    process = (f"video/snapshot,t_1000,f_jpg,w_{examples.PREVIEW_WIDTH},m_fast,ar_auto" if mime.startswith("video/")
               else f"image/resize,w_{examples.PREVIEW_WIDTH}/format,jpg/quality,q_85")
    payload["poster_url"] = oss._presign("GET", key, examples.PREVIEW_SECONDS, subresource={"x-oss-process": process})
    return payload


def item(edition, family, *, private=False):
    result = {"id": edition.id, "example_id": edition.id, "family_id": family.id, "entry_id": family.entry_id,
              "title": edition.title, "version": edition.version, "scene": edition.scene,
              "scene_label": taxonomy.taxonomy().scene_label(family.entry_id, edition.scene),
              "status": edition.status, "source": "creator", "created_at": edition.created_at.isoformat()}
    if private:
        return {**result, "recipe": edition.recipe, "source_session_id": edition.source_session_id}
    if edition.preview_key:
        result.update(_attachment(None, public_key=edition.preview_key, mime=edition.preview_mime,
                                  name=edition.title) or {})
    return result


def builtin_card(template_id):
    from light.production_form import TEMPLATES
    if template_id not in _BUILTIN_VIDEO or template_id not in TEMPLATES["video"]:
        raise missing()
    title, file = _BUILTIN_VIDEO[template_id]
    key = BUILTIN_PREFIX + template_id
    return {"id": key, "example_id": key, "entry_id": "video", "title": title, "scene": "story",
            "scene_label": taxonomy.taxonomy().scene_label("video", "story"), "status": "listed", "source": "builtin",
             "mime": "video/mp4", "media_url": f"/api/media-templates/sample/{template_id}/video" if file else None,
            "poster_url": f"/api/media-templates/sample/{template_id}/cover"}


async def catalog(entry_id, scene=None):
    await taxonomy.refresh()
    async with get_db_session() as db:
        query = _public_query().where(Family.entry_id == entry_id)
        if scene:
            query = query.where(Edition.scene == scene)
        created = [item(edition, family) for edition, family in (await db.execute(
            query.order_by(Edition.created_at.desc()))).all()]
        legacy = (await db.scalars(select(LightExample).join(Import, Import.example_id == LightExample.id)
                                  .where(LightExample.entry_id == entry_id, LightExample.status == "active")
                                  .order_by(LightExample.rating.desc(), LightExample.created_at.desc()))).all()
        legacy_cards = []
        for row in legacy:
            if scene and row.scene != scene:
                continue
            if shown := examples.preview(row):
                legacy_cards.append({**shown, "id": row.id, "entry_id": entry_id, "scene": row.scene,
                                     "scene_label": taxonomy.taxonomy().scene_label(entry_id, row.scene),
                                     "tags": row.tags or [], "status": "listed", "source": "example"})
        video_cards = []
        if entry_id == "video" and scene in {None, "story"}:
            rows = (await db.scalars(video_templates._visible().join(
                Import, Import.video_edition_id == VideoTemplateEdition.id
            ).order_by(VideoTemplateEdition.created_at.desc()))).all()
            video_cards = [{**video_templates.card(row), "id": row.id, "entry_id": entry_id, "scene": "story",
                            "scene_label": taxonomy.taxonomy().scene_label("video", "story"),
                            "status": "listed", "source": "legacy_video"} for row in rows]
    builtin = [builtin_card(key) for key in BUILTIN_VIDEO] if entry_id == "video" and scene in {None, "story"} else []
    return builtin + created + video_cards + legacy_cards


async def start_trial(user_id, workspace_id, edition_id, lang):
    from light import production
    from light.production_form import StartForm

    async with get_db_session() as db:
        edition, family = await owned(db, user_id, workspace_id, edition_id)
        await _require_current_draft(db, edition)
        body = StartForm(entry_id=family.entry_id)
        pinned = pin(edition)

    async def bind(db, row):
        current, _ = await owned(db, user_id, workspace_id, edition_id, lock=True)
        await _require_current_draft(db, current)
        db.add(Trial(session_id=row.id, edition_id=edition_id))

    return await production.create(user_id, workspace_id, body, lang, trial=pinned, on_created=bind)


async def _trial_evidence(db, family, edition, session_id):
    from light import production

    row = await production._lock(db, session_id, family.owner_id, family.workspace_id)
    trial = await db.get(Trial, session_id, with_for_update=True)
    form = row.light_production
    if (not trial or trial.edition_id != edition.id or row.light_entry != family.entry_id
            or not form.get("media_template_trial") or (form.get("media_template") or {}).get("id") != edition.id):
        raise missing()
    # Advanced turns and post-production repair can also attach media to a
    # session. Only a completed render requested by the guided form counts.
    run = form.get("run") or {}
    if run.get("action") != "render" or run.get("revision") != form["revision"]:
        raise conflict("请在精简版使用本版本生成完整成品。")
    view = (await production._views(db, [row]))[0]
    if view["status"] != "completed" or not view.get("output"):
        raise conflict("请先在精简版完成试做。")
    asset = await db.scalar(owned_ready_query(family.owner_id, family.workspace_id).where(
        FileAsset.id == view["output"]["id"], FileAsset.source == "agent",
    ))
    if not asset or not _media_matches(family.entry_id, asset.mime):
        raise conflict("试做的成品已不可用。")
    # A later advanced result cannot replace the guided render as trial evidence.
    guided_view = (await production._views(db, [row], guided_only=True))[0]
    if (guided_view.get("output") or {}).get("id") != asset.id:
        raise conflict("请在精简版确认本次生成的成品。")
    snapshot = {"fields": {key: form[key] for key in _MEDIA_FIELDS if key in form}, "revision": form["revision"],
                "asset_ids": [a["id"] for a in view["materials"]]}
    return trial, asset, snapshot


async def confirm(user_id, workspace_id, edition_id, session_id, output_id):
    async with get_db_session() as db:
        edition, family = await owned(db, user_id, workspace_id, edition_id, lock=True)
        await _require_current_draft(db, edition)
        trial, asset, snapshot = await _trial_evidence(db, family, edition, session_id)
        if asset.id != output_id:
            raise conflict("成品已变化，请刷新后重新确认。")
        trial.output_asset_id, trial.snapshot, trial.confirmed_at = asset.id, snapshot, now()
    return {"session_id": session_id, "output_asset_id": output_id, "confirmed": True}


async def _copy_evidence(asset, edition_id):
    from core.oss import get_oss

    key = f"media-templates/{edition_id}/evidence/{asset.id}"
    copied = await get_oss().copy(asset.oss_key, key)
    if not copied or copied["size"] != asset.size:
        raise LightError("MEDIA_TEMPLATE_PREVIEW", "试做资料保存失败，可以重试提交。", 503)
    return {"key": key, "mime": asset.mime, "name": asset.name}


async def submit(user_id, workspace_id, edition_id, session_id, scene):
    from core.oss import get_oss
    from notifications.inbox import add_inbox

    async with get_db_session() as db:
        edition, family = await owned(db, user_id, workspace_id, edition_id, lock=True)
        if edition.status == "pending":
            saved = await db.get(Submission, edition.id)
            if saved and saved.trial_session_id == session_id and edition.scene == scene:
                return item(edition, family, private=True)
        await _require_current_draft(db, edition)
        if scene not in (await taxonomy.refresh(force=True)).scene_ids(family.entry_id):
            raise LightError("MEDIA_TEMPLATE_CATEGORY", "请选择该制作类型下的二级分类。", 422)
        trial, asset, snapshot = await _trial_evidence(db, family, edition, session_id)
        if not trial.confirmed_at or trial.output_asset_id != asset.id or trial.snapshot != snapshot:
            raise conflict("请重新确认本次试做。")
        source = await db.scalar(owned_ready_query(user_id, workspace_id).where(FileAsset.id == edition.source_asset_id)
                                 .with_for_update())
        if not source:
            raise conflict("原作已不可用。")
        # Public previews are independent of the author's private workspace asset.
        extension = source.name.rsplit(".", 1)[-1].lower() if "." in source.name else ""
        key = f"media-templates/{edition.id}/preview" + (f".{extension}" if extension.isalnum() else "")
        copied = await get_oss().copy(source.oss_key, key)
        if not copied or copied["size"] != source.size:
            raise LightError("MEDIA_TEMPLATE_PREVIEW", "样片保存失败，可以重试提交。", 503)
        evidence = {"result": await _copy_evidence(asset, edition.id)}
        material_ids = snapshot["asset_ids"]
        materials = (await db.scalars(owned_ready_query(user_id, workspace_id).where(
            FileAsset.id.in_(material_ids),
        ))).all()
        if len(materials) != len(material_ids):
            raise conflict("请重新确认完整试做素材。")
        by_id = {material.id: material for material in materials}
        evidence["materials"] = [await _copy_evidence(by_id[asset_id], edition.id) for asset_id in material_ids]
        await taxonomy.lock_edit(db)
        if scene not in (await taxonomy.for_write(db)).scene_ids(family.entry_id):
            raise LightError("MEDIA_TEMPLATE_CATEGORY", "请选择该制作类型下的二级分类。", 422)
        trial.snapshot = {**snapshot, "evidence": evidence}
        edition.preview_key, edition.preview_mime = key, source.mime
        edition.scene, edition.status, edition.submitted_at = scene, "pending", now()
        db.add(Submission(edition_id=edition.id, trial_session_id=trial.session_id))
        reviewers = (await db.execute(select(User.id, User.default_workspace_id).where(
            User.role == "admin", User.is_active.is_(True), User.is_deleted.is_(False),
        ))).all()
        for reviewer, workspace in reviewers:
            if workspace:
                await add_inbox(db, workspace_id=workspace, user_id=reviewer, kind="media_template_pending",
                                title="有媒体模板待审核", body=f"《{edition.title}》v{edition.version}",
                                source_key=f"media-template:pending:{edition.id}",
                                link={"kind": "admin_media_templates"})
        return item(edition, family, private=True)


async def mine(user_id, workspace_id):
    async with get_db_session() as db:
        rows = (await db.execute(_edition().where(Family.owner_id == user_id, Family.workspace_id == workspace_id)
                                .order_by(Edition.created_at.desc()).limit(100))).all()
        ids = [edition.id for edition, _ in rows]
        trials = (await db.execute(select(Trial, Session).join(Session, Session.id == Trial.session_id)
                                   .where(Trial.edition_id.in_(ids), Session.is_deleted.is_(False))
                                   .order_by(Session.created_at.desc()))).all()
        reasons = dict((await db.execute(select(Review.edition_id, Review.reason)
                                       .where(Review.edition_id.in_(ids)).order_by(Review.created_at))).all())
        latest = {}
        for edition, family in rows:
            latest[family.id] = max(latest.get(family.id, 0), edition.version)
        creator = [{**item(edition, family, private=True), "reason": reasons.get(edition.id),
                    "can_trial": edition.status == "draft" and edition.version == latest[family.id],
                 "trials": [{"session_id": session.id, "confirmed": bool(trial.confirmed_at),
                             "output_asset_id": trial.output_asset_id} for trial, session in trials
                            if trial.edition_id == edition.id]}
                for edition, family in rows]
        legacy = (await db.execute(
            select(VideoTemplateEdition, UserSkill, FileAsset.session_id)
            .join(Import, Import.video_edition_id == VideoTemplateEdition.id)
            .join(UserSkill, UserSkill.id == VideoTemplateEdition.skill_id)
            .join(FileAsset, FileAsset.id == VideoTemplateEdition.source_asset_id)
            .where(UserSkill.owner_id == user_id, UserSkill.workspace_id == workspace_id)
            .order_by(VideoTemplateEdition.created_at.desc()).limit(100)
        )).all()
        visible = set()
        if legacy:
            visible = set((await db.scalars(video_templates._visible().with_only_columns(VideoTemplateEdition.id)
                                            .where(VideoTemplateEdition.id.in_([row.id for row, _, _ in legacy])))).all())
        return creator + [{
            "id": row.id, "entry_id": "video", "title": row.manifest["title"], "version": row.version,
            "status": legacy_status(skill, visible=row.id in visible),
            "source": "legacy_video", "source_session_id": source,
            "source_asset_id": row.source_asset_id, "recipe": row.manifest.get("direction"),
            "reason": skill.listing_note,
        } for row, skill, source in legacy]


def legacy_status(skill, *, visible=True):
    if skill.status == "withdrawn" or skill.listing == "delisted":
        return "withdrawn"
    if skill.status != "published":
        return "draft"
    return "superseded" if skill.listing == "listed" and not visible else skill.listing



async def review_detail(edition_id):
    async with get_db_session() as db:
        found = (await db.execute(_edition().where(Edition.id == edition_id, Edition.status != "draft"))).first()
        if not found:
            raise missing()
        edition, family = found
        submission = await db.get(Submission, edition.id)
        trial = await db.get(Trial, submission.trial_session_id)
        snapshot = trial.snapshot
        evidence = snapshot["evidence"]

        def display(asset):
            return _attachment(None, public_key=asset["key"], mime=asset["mime"], name=asset["name"])

        return {**item(edition, family, private=True), "owner_id": family.owner_id,
                "original": _attachment(None, public_key=edition.preview_key, mime=edition.preview_mime,
                                        name=edition.title),
                "input": snapshot["fields"],
                "materials": [display(material) for material in evidence["materials"]],
                "trial_result": display(evidence["result"]),
                "trial_session_id": trial.session_id,
                "decisions": [{"decision": review.decision, "reviewer_id": review.reviewer_id, "reason": review.reason,
                               "created_at": review.created_at.isoformat()}
                              for review in (await db.scalars(select(Review).where(Review.edition_id == edition.id)
                                                             .order_by(Review.created_at))).all()]}


async def decide(edition_id, reviewer_id, decision, reason):
    from notifications.inbox import add_inbox

    async with get_db_session() as db:
        found = (await db.execute(_edition().where(Edition.id == edition_id).with_for_update(of=Family))).first()
        if not found:
            raise missing()
        edition, family = found
        await db.refresh(edition)
        expected = "listed" if decision == "withdrawn" else "pending"
        if edition.status != expected:
            raise conflict("模板审核状态已变化，请刷新。")
        submission = await db.get(Submission, edition.id)
        trial = await db.get(Trial, submission.trial_session_id)
        if decision == "listed":
            if not trial.output_asset_id or not (trial.snapshot or {}).get("evidence") or not edition.preview_key:
                raise conflict("审核所需的成品已不可用。")
            await db.execute(update(Edition).where(Edition.template_id == edition.template_id, Edition.status == "listed")
                             .values(status="superseded"))
        edition.status = decision
        db.add(Review(id=ascending("mtr"), edition_id=edition.id, trial_session_id=trial.session_id,
                      reviewer_id=reviewer_id, decision=decision, reason=reason, created_at=now()))
        message = {"listed": "模板已上架", "rejected": "模板未通过审核", "withdrawn": "模板已下架"}[decision]
        await add_inbox(db, workspace_id=family.workspace_id, user_id=family.owner_id, kind="media_template_review",
                        title=message, body=f"《{edition.title}》v{edition.version}" + (f"。{reason}" if reason else ""),
                        source_key=f"media-template:{decision}:{edition.id}",
                        link={"kind": "media_templates", "workspaceId": family.workspace_id, "jobId": edition.id})
        return item(edition, family, private=True)


async def admin_list(entry_id, scene, status, query, limit, offset):
    """Merge the shared index, with filtering and paging before detail loading."""
    await taxonomy.refresh()
    async with get_db_session() as db:
        def filtered(stmt, entry, category, state, title, timestamp):
            if entry_id:
                stmt = stmt.where(entry == entry_id)
            if scene:
                stmt = stmt.where(category == scene)
            if status:
                stmt = stmt.where(state == status)
            if query:
                stmt = stmt.where(title.icontains(query, autoescape=True))
            return stmt.order_by(timestamp.desc()).limit(limit + offset + 1)

        from sqlalchemy import case, literal
        creator = filtered(_edition().where(Edition.status != "draft"), Family.entry_id, Edition.scene,
                           Edition.status, Edition.title, Edition.created_at)
        legacy_status = case((LightExample.status == "active", "listed"), else_="withdrawn")
        historical = filtered(select(LightExample, legacy_status)
                              .join(Import, Import.example_id == LightExample.id), LightExample.entry_id,
                              LightExample.scene, legacy_status, LightExample.title, LightExample.created_at)
        video_status = case(
            (or_(UserSkill.status == "withdrawn", UserSkill.listing == "delisted"), "withdrawn"),
            (and_(UserSkill.listing == "listed", VideoTemplateEdition.id.not_in(
                video_templates._visible().with_only_columns(VideoTemplateEdition.id)
            )), "superseded"), else_=UserSkill.listing,
        )
        old_video = filtered(select(VideoTemplateEdition, UserSkill, video_status)
                             .join(Import, Import.video_edition_id == VideoTemplateEdition.id)
                             .join(UserSkill, UserSkill.id == VideoTemplateEdition.skill_id)
                             .where(UserSkill.status.in_(("published", "withdrawn"))), literal("video"), literal("story"),
                             video_status, type_coerce(VideoTemplateEdition.manifest, JSON)["title"].as_string(),
                             VideoTemplateEdition.created_at)
        created = [item(edition, family) for edition, family in (await db.execute(creator)).all()]
        history = [{"id": row.id, "entry_id": row.entry_id, "scene": row.scene, "title": row.title,
                    "scene_label": taxonomy.taxonomy().scene_label(row.entry_id, row.scene), "source": "example",
                    "status": state, "created_at": row.created_at.isoformat()}
                   for row, state in (await db.execute(historical)).all()]
        videos = [{"id": row.id, "entry_id": "video", "scene": "story", "title": row.manifest["title"],
                   "scene_label": taxonomy.taxonomy().scene_label("video", "story"), "source": "legacy_video",
                   "status": state, "created_at": row.created_at.isoformat(), "version": row.version}
                  for row, _skill, state in (await db.execute(old_video)).all()]
    builtins = []
    if entry_id in {None, "video"} and scene in {None, "story"} and status in {None, "listed"}:
        builtins = [builtin_card(key) for key in BUILTIN_VIDEO]
        if query:
            builtins = [row for row in builtins if query.casefold() in row["title"].casefold()]
    results = sorted(created + history + videos, key=lambda row: (row["created_at"], row["id"]), reverse=True) + builtins
    return {"items": results[offset:offset + limit], "has_more": len(results) > offset + limit}


async def legacy_detail(edition_id):
    async with get_db_session() as db:
        result = (await db.execute(select(VideoTemplateEdition, UserSkill)
                                  .join(UserSkill, UserSkill.id == VideoTemplateEdition.skill_id)
                                  .join(Import, Import.video_edition_id == VideoTemplateEdition.id)
                                  .where(VideoTemplateEdition.id == edition_id, UserSkill.status.in_(("published", "withdrawn"))))).first()
        if not result:
            raise missing()
        edition, skill = result
        asset = await db.get(FileAsset, edition.source_asset_id)
        return {"id": edition.id, "title": edition.manifest["title"], "version": edition.version,
                "recipe": edition.manifest.get("direction"), "status": legacy_status(skill), "owner_id": skill.owner_id,
                "source": "legacy_video", "family_id": edition.template_id, "catalog_id": "community:" + skill.id, "original": _attachment(
                    asset, public_key=edition.preview_key, mime=asset.mime if asset else "video/mp4"),
                "input": None, "materials": [], "trial_result": None, "decisions": []}
