"""Guided production state, saved on the same row as the Agent conversation."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import JSON, func, or_, select, type_coerce, update

from agent.inbox import accept_inbox_item_locked, schedule_inbox_wake
from assets.service import owned_ready_query
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.file_asset import FileAsset
from db.models.light_example import LightExample
from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from db.models.video_job import VideoJob
from light import production_form as forms
from light import examples, production_music, remake_case, shot_editor, silk_road, video_tiers, wan_story
from light.mode import LightError
from session.agent_event_log import prepare_agent_event_write

_IN_FLIGHT = {"queued", "submitting", "in_progress", "generating", "finalizing", "transfer_failed"}
_COMPOSITIONS = {"compose", "render"}
_BUSY = {"working", "blocked"}
_ENTRY_IDS = tuple(forms.TEMPLATES)


def _owned(user_id: str, workspace_id: str, *, include_legacy: bool = False):
    query = select(Session).where(
        Session.user_id == user_id, Session.workspace_id == workspace_id, Session.is_deleted.is_(False),
        Session.kind == "normal", Session.light_entry.in_(_ENTRY_IDS),
    )
    return query if include_legacy else query.where(Session.light_production.is_not(None))


def _not_found():
    return LightError("LIGHT_PRODUCTION_NOT_FOUND", "Production not found", 404)


async def _lock(db, session_id: str, user_id: str, workspace_id: str, *, run_fence=None):
    try:
        row = await prepare_agent_event_write(db, session_id=session_id, user_id=user_id, run_fence=run_fence)
    except LookupError as exc:
        raise _not_found() from exc
    if row.workspace_id != workspace_id or not row.light_production or row.is_deleted:
        raise _not_found()
    return row


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _form(row: Session) -> dict:
    form = row.light_production or forms.initial_form(row.light_entry, "", "")
    if row.light_entry == "video" and "voice_id" in form:
        return {key: value for key, value in form.items() if key != "voice_id"}
    return form


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _chat_tier(config, *, tier=None, model=None):
    if not config.model_tiers.product_chat:
        return None
    return next((row for row in config.model_tiers.chat
                 if row.tier == tier or row.model == model), None)


async def _start_form(user_id: str, workspace_id: str, body: forms.StartForm, lang: str | None, *, save: bool):
    from agent.model_resolve import resolve
    from auth.quota import check_session_quota
    from core.config import get_config
    from light.entries import light_entries
    from light.mode import _workspace_facts, availability
    from store.service import get_store, preview_shop_name

    entry = light_entries(lang).entry(body.entry_id)
    if entry is None:
        raise _not_found()
    state = availability(entry, await _workspace_facts(workspace_id))
    if state != "ok":
        raise LightError("LIGHT_ENTRY_UNAVAILABLE", "Production is unavailable", 409, availability=state)
    config = get_config()
    if save:
        await check_session_quota(user_id, config)
        name = ((await get_store(workspace_id, user_id)) or {}).get("name") or ""
    else:
        name = await preview_shop_name(workspace_id, user_id)
    model, _ = resolve("", config, context="light production")
    chat_tier = _chat_tier(config, model=model)
    form = forms.initial_form(body.entry_id, name[:80], body.brief)
    tiers = video_tiers.available_tiers(config) if body.entry_id in {"video", "footage_edit"} else []
    if tiers:
        form["video_tier"] = tiers[0]
    return form, body.brief or entry.title, model, chat_tier, tiers


async def preview(user_id: str, workspace_id: str, body: forms.StartForm, lang: str | None) -> dict:
    form, title, _, chat_tier, tiers = await _start_form(user_id, workspace_id, body, lang, save=False)
    return {**form, "entry_id": body.entry_id, "title": title, "status": "draft", "available_video_tiers": tiers,
            **({"chat_tier": chat_tier.tier} if chat_tier else {})}


async def create(user_id: str, workspace_id: str, body: forms.StartForm, lang: str | None) -> dict:
    from agent.agent import default_agent_name
    from session.session import create_session

    form, title, model, chat_tier, _ = await _start_form(user_id, workspace_id, body, lang, save=True)
    session = await create_session(
        model=model, variant=chat_tier.variant if chat_tier else None,
        agent=default_agent_name(), title=title, user_id=user_id,
        workspace_id=workspace_id, light_entry=body.entry_id,
        light_production=form,
    )
    return await get(user_id, workspace_id, session.id)


def _media_matches(entry: str, mime: str) -> bool:
    return mime.startswith("image/") if entry in forms.IMAGE_ENTRIES else mime.startswith(("image/", "video/"))


async def _require_reference(db, entry_id: str, example_id: str) -> None:
    if not examples.is_production_reference(await db.get(LightExample, example_id), entry_id):
        raise LightError("LIGHT_PRODUCTION_EXAMPLE_UNAVAILABLE", "This reference example is unavailable.", 409)


async def _validate_fields(db, row: Session, changes: dict) -> None:
    if "chat_tier" in changes:
        from core.config import get_config

        if _chat_tier(get_config(), tier=changes["chat_tier"]) is None:
            raise LightError("LIGHT_CHAT_TIER_UNAVAILABLE", "The selected text model is unavailable.", 409)
    if "template" in changes:
        template = changes["template"]
        if template not in forms.TEMPLATES[row.light_entry]:
            raise LightError("LIGHT_PRODUCTION_TEMPLATE", "Invalid template for this production")
        if (row.light_entry == "video" and template in forms.RETIRED_VIDEO_TEMPLATES
                and template != _form(row)["template"]):
            raise LightError("LIGHT_PRODUCTION_TEMPLATE_RETIRED", "This template is no longer offered.", 409)
    if "video_tier" in changes:
        template = changes.get("template", _form(row)["template"])
        if (row.light_entry != "footage_edit"
                and (row.light_entry != "video" or template not in forms.VIDEO_TIER_TEMPLATES)):
            raise LightError("LIGHT_VIDEO_TIER_UNAVAILABLE", "The selected video model is unavailable.", 409)
    if "poster_title" in changes and row.light_entry != "poster":
        raise LightError("LIGHT_PRODUCTION_POSTER", "Only posters have a headline field")
    if "redo_narration" in changes and row.light_entry != "footage_edit":
        raise LightError("PRODUCTION_NARRATION_STATE", "Narration replacement requires original footage.")
    if "music_style" in changes and row.light_entry != "video":
        raise LightError("PRODUCTION_MUSIC_STATE", "This music setting requires an AI video.")
    if "voice_id" in changes:
        selected = changes["voice_id"]
        if row.light_entry != "footage_edit" or (
                selected and selected != "none" and not selected.startswith(("system:", "voice:"))):
            raise LightError("PRODUCTION_VOICE_UNAVAILABLE", "Choose an available voice.")
    if "dishes" in changes and row.light_entry != "menu":
        raise LightError("LIGHT_PRODUCTION_DISHES", "Only menus have dish rows")
    if "creative_effects" in changes and row.light_entry != "footage_edit":
        raise LightError("LIGHT_PRODUCTION_STATE", "Creative inserts are only available for footage edits")
    if "asset_ids" in changes:
        assets = (await db.scalars(owned_ready_query(row.user_id, row.workspace_id)
                                   .where(FileAsset.id.in_(changes["asset_ids"])))).all()
        if len(assets) != len(changes["asset_ids"]) or any(not _media_matches(row.light_entry, a.mime) for a in assets):
            raise LightError("LIGHT_PRODUCTION_ASSET", "A material is missing or unsupported")


def _apply(row: Session, changes: dict, *, from_agent: bool = False) -> None:
    form = dict(_form(row))
    actual = {key: value for key, value in changes.items()
              if key != "reference_example_id" and form.get(key) != value}
    reference_id = row.light_example_id
    if "reference_example_id" in changes:
        reference_id = changes["reference_example_id"] or None
    elif "template" in actual:
        reference_id = None
    reference_changed = reference_id != row.light_example_id
    if "chat_tier" in changes:
        from core.config import get_config

        tier = _chat_tier(get_config(), tier=changes["chat_tier"])
        if tier and (row.model, row.variant) != (tier.model, tier.variant):
            row.model, row.variant = tier.model, tier.variant
            actual["chat_tier"] = tier.tier
    if not actual and not reference_changed:
        return
    row.light_example_id = reference_id
    form.update(actual)
    if {"asset_ids", "shop", "template", "keep_original"}.intersection(actual):
        form.pop("narration_draft", None)
        form.pop("narration_preview", None)
    elif {"voice_id", "redo_narration"}.intersection(actual):
        form.pop("narration_preview", None)
    if "video_tier" in actual:
        row.video_model, row.video_resolution = video_tiers.resolve_tier(form["video_tier"])
        if (form["template"] == silk_road.TEMPLATE
                and not (form.get("remake_adaptation") or {}).get("world")):
            form["remake_prepared_for"] = None
    form["revision"] += 1
    if reference_changed or any(key != "page" for key in actual):
        form["edited_at"] = _now().isoformat()
    if from_agent:
        form["agent_revision"] = form["revision"]
    row.light_production = form
    row.updated_at = _now()


def _check_revision(form: dict, revision: int):
    if form["revision"] != revision:
        raise LightError("LIGHT_PRODUCTION_CHANGED", "The production changed. Reload its latest draft.", 409)


def _shot_progress(run, jobs, phase):
    prefix = run.get("job_prefix")
    plan = run.get("remake_adaptation") or {}
    if (run.get("action") != "render" or not prefix or not isinstance(plan, dict)
            or plan.get("format") not in {"shot-list-v2", silk_road.FORMAT}
            or phase not in {"wan_shots", "wan_edit", "compose"}):
        return None
    total = wan_story.shot_count(run)
    indices = range(1, total + 1)
    reviewed = {cut["shot"] for cut in run.get("wan_cuts") or [] if cut.get("shot") in indices}
    filmed = set(reviewed)
    retaking = set()
    detail = "wan_quality" if phase == "wan_edit" and run.get("wan_quality") else phase
    final_key = run.get("wan_final_key")
    for job in jobs:
        key = job.idempotency_key or ""
        if not key.startswith(prefix + ":"):
            continue
        if (phase == "compose" and job.kind in _COMPOSITIONS and job.status == "completed" and job.output_asset_id
                and final_key and (key == final_key or key.startswith(final_key + ":retry:"))):
            detail = "wan_delivery"
        if job.kind != "segment":
            continue
        index = (job.request_data or {}).get("shot")
        if index not in indices:
            continue
        if job.status == "completed" and job.output_asset_id:
            filmed.add(index)
        elif job.status in _IN_FLIGHT and key.startswith((f"{prefix}:shot{index}:retake:", f"{prefix}:quality-")):
            retaking.add(index)
    return {"total": total, "filmed": len(filmed), "reviewed": len(reviewed), "retaking": len(retaking), "phase": detail}


def _work_view(row: Session, receipt, jobs: list, output, question: bool) -> dict:
    form = _form(row)
    run = form.get("run") or {}
    action = run.get("action")
    active = any(job.status in _IN_FLIGHT for job in jobs)
    # A settled director cannot advance a grouped take. Retry rejoins its original provider job.
    if run.get("remake_version") in wan_story.VERSIONS and receipt and receipt.state == "settled":
        active = False
    if row.status in {"busy", "compacting", "retry", "queued"} or active or (receipt and receipt.state in {"accepted", "claimed"}):
        if action == "prepare" and receipt and receipt.state in {"accepted", "claimed"}:
            phase = "recognize" if row.light_entry == "menu" else "copy"
        elif (stage := (form.get("run") or {}).get("remake_stage")):
            phase = stage
        elif any(job.kind in _COMPOSITIONS for job in jobs):
            phase = "compose" if row.light_entry == "video" else "edit"
        elif action == "render" or active:
            phase = {"video": "video", "footage_edit": "edit", "menu": "menu", "poster": "poster"}[row.light_entry]
        else:
            phase = "agent"
        progress = _shot_progress(run, jobs, phase)
        return {"status": "working", "phase": phase, **({"shot_progress": progress} if progress else {})}
    if any(((job.result_data or {}).get("submit_error") or {}).get("submission_outcome") == "ambiguous"
           for job in jobs):
        return {"status": "blocked", "phase": "compose"}
    if form.get("proposal") or question:
        return {"status": "review", "phase": "proposal" if form.get("proposal") else "question"}
    if output:
        return {"status": "completed"}
    if action == "render":
        return {"status": "failed", "phase": "render"}
    prepared = (form.get("agent_revision") or 0) > (form.get("run") or {}).get("revision", 0)
    if action == "prepare" and not prepared:
        return {"status": "draft", "phase": "prepare_failed"}
    return {"status": "draft"}


def _asset_view(asset, oss) -> dict:
    thumbnail = None
    if oss:
        if asset.mime.startswith("image/"):
            thumbnail = oss.presign_get(asset.oss_key)
        elif asset.mime.startswith("video/"):
            from tool.footage_analyze import snapshot_url
            thumbnail = snapshot_url(oss, asset.oss_key, 0.0, width=480)
    meta = getattr(asset, "meta", None) or {}
    return {"id": asset.id, "name": asset.name, "mime": asset.mime, "thumbnail": thumbnail,
            **({"width": meta["width"], "height": meta["height"]} if meta.get("width") and meta.get("height") else {})}


def _output_candidates(row: Session, jobs: list, files: list, *, advanced_messages=frozenset()) -> list[str]:
    form = _form(row)
    expected = "image/" if row.light_entry in forms.IMAGE_ENTRIES else "video/"
    edited_at = form.get("edited_at")
    since = datetime.fromisoformat(edited_at) if edited_at else _utc(row.created_at)
    run = form.get("run") or {}
    guided_music = row.light_entry == "video" and run.get("music_style") not in {None, "off"}
    needs_render = run.get("action") == "render" and (
        row.light_entry == "footage_edit" or (row.light_entry == "video" and form["template"]) or guided_music
    )
    results = [
        (_utc(part.created_at), part.data["asset_id"]) for part in files
        if (not needs_render or getattr(part, "message_id", None) in advanced_messages)
        and _utc(part.created_at) >= since and str(part.data.get("mime_type", "")).startswith(expected)
        and part.data.get("asset_id") and (part.data.get("relation") or {}).get("role") in {"final", "result"}
    ]
    results.extend((_utc(job.completed_at or job.updated_at), job.output_asset_id) for job in jobs
                   if row.light_entry not in forms.IMAGE_ENTRIES and job.kind in _COMPOSITIONS and job.status == "completed" and job.output_asset_id
                   and (wan_story.is_complete_cut(run, job) if run.get("remake_version") in wan_story.VERSIONS else
                        (not run.get("remake_version") or job.idempotency_key.startswith(run["job_prefix"] + ":final"))))
    if row.light_entry == "video" and any(shot_editor.is_edit_job(job) for job in jobs):
        seen = [asset_id for _, asset_id in sorted(results, reverse=True)]
        results.extend((_utc(job.completed_at or job.updated_at), job.output_asset_id)
                       for job in shot_editor.output_keys(form, jobs, seen))
    return list(dict.fromkeys(asset_id for _, asset_id in sorted(results, reverse=True)))


async def _views(db, rows: list[Session], *, include_shots: bool = False) -> list[dict]:
    if not rows:
        return []
    from core.oss import OssNotConfigured, get_oss

    ids = [row.id for row in rows]
    active_run_ids = [row.light_production["run"]["id"] for row in rows
                      if row.light_production and row.light_production.get("run")]
    receipts = {item.id: item for item in (await db.scalars(select(AgentInboxItem)
                .where(AgentInboxItem.id.in_(active_run_ids)))).all()} if active_run_ids else {}
    questions = set((await db.scalars(select(QuestionCheckpoint.session_id).where(
        QuestionCheckpoint.session_id.in_(ids), QuestionCheckpoint.status == "pending",
    ))).all())
    jobs_by_session, files_by_session = defaultdict(list), defaultdict(list)
    for job in (await db.scalars(select(VideoJob).where(VideoJob.session_id.in_(ids),
                                                        VideoJob.kind.in_({"segment"} | _COMPOSITIONS)))).all():
        jobs_by_session[job.session_id].append(job)
    for part in (await db.scalars(select(Part).where(Part.session_id.in_(ids), Part.type == "file"))).all():
        files_by_session[part.session_id].append(part)

    from light.production_outputs import advanced_result_messages
    advanced_messages = await advanced_result_messages(db, rows, files_by_session, receipts)

    saved_assets = set()
    output_ids, relevant_jobs = {}, {}
    for row in rows:
        form = _form(row)
        stamp = form.get("edited_at")
        since = datetime.fromisoformat(stamp) if stamp else _utc(row.created_at)
        jobs = [job for job in jobs_by_session[row.id] if job.user_id == row.user_id and _utc(job.created_at) >= since]
        relevant_jobs[row.id] = jobs
        output_ids[row.id] = _output_candidates(row, jobs, files_by_session[row.id], advanced_messages=advanced_messages)
        saved_assets.update(output_ids[row.id])
        saved_assets.update(form.get("asset_ids", ()))
    assets = {a.id: a for a in (await db.scalars(owned_ready_query(rows[0].user_id, rows[0].workspace_id)
                                                .where(FileAsset.id.in_(saved_assets)))).all()} if saved_assets else {}
    try:
        oss = get_oss() if assets else None
    except OssNotConfigured:
        oss = None
    media = {asset_id: _asset_view(asset, oss) for asset_id, asset in assets.items()}
    from core.config import get_config

    config = get_config()
    tiers = video_tiers.available_tiers(config) if include_shots and any(
        row.light_entry in {"video", "footage_edit"} for row in rows) else []
    result = []
    for row in rows:
        form = _form(row)
        output = next((media[key] for key in output_ids[row.id] if key in media), None)
        jobs = relevant_jobs[row.id]
        guide_jobs = [job for job in jobs if not shot_editor.is_edit_job(job)]
        editor = await shot_editor.project(
            db, row, jobs, [key for key in output_ids[row.id] if key in media], oss,
            advanced_turns=advanced_messages,
        ) if include_shots else None
        chat_tier = _chat_tier(config, model=row.model) or _chat_tier(config, model=config.model)
        reference_fields = {}
        if include_shots and row.light_production and row.light_example_id:
            reference = await db.get(LightExample, row.light_example_id)
            reference_fields = {
                "reference_example_id": row.light_example_id,
                "reference_example_title": reference.title if reference else "",
                "reference_example_title_translations": examples.title_translations(reference),
            }
        result.append({
            **{key: value for key, value in form.items() if key not in {
                "run", "edited_at", "remake_adaptation", "remake_prepared_for", "shot_edit_request",
                "shot_frame_drafts", "shot_frame_inputs", "shot_title_edits", "chat_tier",
                "narration_preview", "narration_draft",
            }},
            **_work_view(row, receipts.get((form.get("run") or {}).get("id")), guide_jobs, output,
                         row.id in questions),
            **({"shot_editor": editor} if editor else {}),
            **({"available_video_tiers": tiers} if include_shots and row.light_entry in {"video", "footage_edit"} else {}),
            **({"chat_tier": chat_tier.tier} if include_shots and chat_tier else {}),
            "session_id": row.id, "entry_id": row.light_entry, "title": row.title, "legacy": not row.light_production,
            "updated_at": _utc(row.updated_at).isoformat(), "output": output,
            "materials": [media[key] for key in form["asset_ids"] if key in media],
            **reference_fields,
            **({"reference_copy": remake_case.reference_copy(form),
                "remake_prepared": form.get("remake_prepared_for") == remake_case.input_fingerprint(form)}
               if remake_case.applies(row.light_entry, form) else {}),
            **({"suggested_product": (form.get("remake_adaptation") or {}).get("product", "")}
               if form.get("template") == silk_road.TEMPLATE
               and form.get("remake_prepared_for") == remake_case.input_fingerprint(form) else {}),
        })
    return result


async def get(user_id: str, workspace_id: str, session_id: str) -> dict:
    async with get_db_session() as db:
        row = await db.scalar(_owned(user_id, workspace_id).where(Session.id == session_id))
        if row is None:
            raise _not_found()
        return (await _views(db, [row], include_shots=True))[0]


async def history(user_id: str, workspace_id: str, limit: int, offset: int, *, entry_id: str | None = None) -> dict:
    query = _owned(user_id, workspace_id, include_legacy=True)
    if entry_id is not None:
        query = query.where(Session.light_entry == entry_id)
    if entry_id in {None, "video"}:
        # Older clients create a session as soon as they open the video form.
        # Default values alone do not make a production record.
        form = type_coerce(Session.light_production, JSON)
        query = query.where(or_(
            Session.light_entry != "video", Session.light_production.is_(None), Session.status != "idle",
            func.coalesce(form["revision"].as_integer(), 0) != 1,
            func.coalesce(form["brief"].as_string(), "") != "", Session.light_example_id.is_not(None),
            select(Message.id).where(Message.session_id == Session.id, Message.role == "user").exists(),
            select(VideoJob.id).where(VideoJob.session_id == Session.id, VideoJob.user_id == Session.user_id).exists(),
        ))
    query = query.order_by(Session.updated_at.desc(), Session.id).offset(offset).limit(limit + 1)
    async with get_db_session() as db:
        rows = list((await db.scalars(query)).all())
        return {"items": await _views(db, rows[:limit]), "has_more": len(rows) > limit}


async def edit(user_id: str, workspace_id: str, session_id: str, body: forms.EditForm) -> dict:
    async with get_db_session() as db:
        row = await _lock(db, session_id, user_id, workspace_id)
        _check_revision(row.light_production, body.revision)
        view = (await _views(db, [row]))[0]
        if view["status"] in _BUSY or view["status"] == "completed" or row.light_production.get("proposal"):
            raise LightError("LIGHT_PRODUCTION_STATE", "Finish the current production action before editing", 409)
        changes = body.model_dump(exclude_none=True, exclude={"revision"})
        if row.light_entry == "video":
            # Older mobile clients may still send their saved narration choice.
            changes.pop("voice_id", None)
            if "music_style" in changes:
                changes["beat_music"] = changes["music_style"] != "off"
            elif "beat_music" in changes:
                changes["music_style"] = "auto" if changes["beat_music"] else "off"
        form = row.light_production
        reference_id = changes.get("reference_example_id")
        previous_reference = row.light_example_id
        if reference_id:
            await _require_reference(db, row.light_entry, reference_id)
            if row.light_entry == "video" or row.light_entry in forms.IMAGE_ENTRIES:
                changes["template"] = ""
        stays_remake = remake_case.applies(row.light_entry, {**form, **changes})
        if stays_remake and "script" in changes:
            # Older Apps wrote the editable value as script.
            changes.setdefault("reference_copy", changes.pop("script"))
        if not stays_remake and remake_case.applies(row.light_entry, form):
            changes.setdefault("reference_copy", remake_case.reference_copy(form))
            changes.setdefault("script", "")
            changes["remake_prepared_for"] = None
        await _validate_fields(db, row, changes)
        _apply(row, changes)
        if reference_id and reference_id != previous_reference:
            await db.execute(update(LightExample).where(LightExample.id == reference_id)
                             .values(pick_count=LightExample.pick_count + 1))
    return await get(user_id, workspace_id, session_id)


async def update_from_agent(ctx, *, revision: int, changes: dict, proposal: str | None) -> int:
    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        _check_revision(form, revision)
        if remake_case.applies(row.light_entry, {**form, **changes}) and "script" in changes:
            changes.setdefault("reference_copy", changes.pop("script"))
        run = form.get("run") or {}
        if run.get("action") == "prepare":
            receipt = await db.get(AgentInboxItem, run["id"])
            if receipt and receipt.state == "claimed" and receipt.run_id == ctx.run_id:
                field = "dishes" if row.light_entry == "menu" else "script"
                if proposal is not None or field not in changes:
                    raise LightError("LIGHT_PRODUCTION_PREPARE", f"Save only the prepared {field} with action=update")
                changes = {field: changes[field]}
        await _validate_fields(db, row, changes)
        if proposal is None:
            _apply(row, changes, from_agent=True)
            # A legitimate answer can leave the text or detected dishes unchanged.
            if row.light_production is form:
                row.light_production = {**form, "revision": revision + 1, "agent_revision": revision + 1}
                row.updated_at = _now()
        else:
            row.light_production = {**form, "revision": revision + 1,
                                    "proposal": {"summary": proposal, "changes": changes}}
            row.updated_at = _now()
        return row.light_production["revision"]


async def begin_remake(ctx, adaptation: remake_case.Adaptation | wan_story.Plan | wan_story.ShotPlan | silk_road.Plan | None, *, requested_action: str | None = None):
    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        run = form.get("run") or {}
        receipt = await db.get(AgentInboxItem, run["id"]) if run.get("id") else None
        if (not remake_case.applies(row.light_entry, form) or not run.get("remake_version") or not ctx.run_fence
                or receipt is None or receipt.state != "claimed" or receipt.run_id != ctx.run_id
                or receipt.generation != ctx.run_generation):
            raise LightError("LIGHT_REMAKE_SCOPE", "Open the remake template in the production form to start this action.", 409)
        if error := run.get("remake_error"):
            raise LightError("LIGHT_REMAKE_STOPPED", error, 409)
        template = form.get("template")
        if template == silk_road.TEMPLATE:
            recipe, plan_type = silk_road, silk_road.Plan
        elif template in wan_story.TEMPLATES:
            recipe, plan_type = wan_story, (wan_story.Plan, wan_story.ShotPlan)
        else:
            recipe, plan_type = remake_case, remake_case.Adaptation
        if requested_action and (template not in wan_story.TEMPLATES or requested_action != run["action"]):
            raise LightError("LIGHT_REMAKE_SCOPE", "请按当前 App 操作调用分镜工具。", 409)
        if run["remake_version"] != remake_case.version_for(form):
            raise LightError("LIGHT_REMAKE_CHANGED", "样片已更新，请返回制作页重新生成。", 409)
        fingerprint = remake_case.input_fingerprint(form)
        if run["action"] == "render":
            stored = run.get("remake_adaptation")
            if stored is None and form.get("remake_prepared_for") == fingerprint:
                stored = form.get("remake_adaptation")
            if stored is not None:
                adaptation = wan_story.parse_plan(stored) if template in wan_story.TEMPLATES else plan_type.model_validate(stored)
        if not isinstance(adaptation, plan_type):
            raise LightError("LIGHT_REMAKE_PLAN", "当前表单需要新的分镜，请根据本次素材与文案填入 plan。", 409)
        if run["action"] == "prepare":
            if isinstance(adaptation, silk_road.Plan):
                if name := form.get("product_name"):
                    adaptation = adaptation.model_copy(update={"product": name})
                else:
                    adaptation = adaptation.for_shop(form["shop"])
            changes = {"script": recipe.display_script(adaptation), "reference_copy": remake_case.reference_copy(form),
                       "remake_adaptation": adaptation.model_dump(), "remake_prepared_for": fingerprint}
            _apply(row, changes, from_agent=True)
            if row.light_production is form:
                row.light_production = {**form, "revision": form["revision"] + 1, "agent_revision": form["revision"] + 1}
                row.updated_at = _now()
        elif run["action"] == "render":
            snapshot = adaptation.model_dump()
            run = {**run, "remake_adaptation": snapshot}
            if form.get("template") == wan_story.SODA_TEMPLATE and not run.get("video_model"):
                run.update(video_model=row.video_model, video_resolution=row.video_resolution)
            row.light_production = {**form, "run": run, "remake_adaptation": snapshot,
                                    "remake_prepared_for": fingerprint, "script": recipe.display_script(adaptation),
                                    "reference_copy": remake_case.reference_copy(form)}
            row.updated_at = _now()
        else:
            raise LightError("LIGHT_REMAKE_SCOPE", "This action cannot generate a remake.", 409)
        return row.light_production, run, adaptation


async def remake_stage(ctx, key: str, stage: str | None = None, *, error: str | None = None) -> None:
    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        run = form.get("run") or {}
        if run.get("job_prefix") != key:
            raise LightError("LIGHT_PRODUCTION_CHANGED", "This draft has a newer version.", 409)
        update = {"remake_error": error} if error else {"remake_stage": stage}
        row.light_production = {**form, "run": {**run, **update}}
        row.updated_at = _now()


async def begin_reference_edit(ctx, prefix: str, version: str) -> dict:
    """Recheck older visual decisions under a new review contract without discarding their evidence."""
    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        run = form.get("run") or {}
        if run.get("job_prefix") != prefix or run.get("remake_version") != wan_story.VERSION:
            raise LightError("LIGHT_PRODUCTION_CHANGED", "The saved edit belongs to another version.", 409)
        if run.get("wan_review_version") == version:
            return run
        history = list(run.get("wan_edit_history") or [])
        if run.get("wan_cuts"):
            history.append({"version": run.get("wan_review_version", "initial"),
                            "cuts": run["wan_cuts"], "final_key": run.get("wan_final_key"),
                            **({"quality": run["wan_quality"]} if "wan_quality" in run else {})})
        run = {k: v for k, v in run.items() if k not in {"wan_final_key", "wan_quality", "wan_coverage"}}
        run.update(wan_review_version=version, wan_cuts=[], wan_edit_history=history)
        row.light_production = {**form, "run": run}
        row.updated_at = _now()
        return run


async def save_director_decision(ctx, prefix, index, key, decision):
    """Commit the director's choice before executing it; replay the winner on resume."""
    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        run = form.get("run") or {}
        if (run.get("job_prefix") != prefix or not run.get("wan_review_version")
                or not 1 <= index <= wan_story.shot_count(run)):
            raise LightError("LIGHT_PRODUCTION_CHANGED", "The edit belongs to another production.", 409)
        saved = dict(run.get("director_decisions") or {})
        if key not in saved:
            saved[key] = decision
            row.light_production = {**form, "run": {**run, "director_decisions": saved}}
            row.updated_at = _now()
        return saved[key]


async def save_reference_cut(ctx, prefix: str, cut: dict) -> None:
    """Checkpoint one reviewed shot even when an earlier shot is still filming."""
    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        run = form.get("run") or {}
        index = cut["shot"]
        if (run.get("job_prefix") != prefix or run.get("remake_version") != wan_story.VERSION
                or (run.get("remake_adaptation") or {}).get("format") not in {"shot-list-v2", silk_road.FORMAT}
                or not cut.get("review_version") or run.get("wan_review_version") != cut["review_version"]
                or not 1 <= index <= wan_story.shot_count(run)):
            raise LightError("LIGHT_PRODUCTION_CHANGED", "The shot belongs to another edit.", 409)
        cuts = {c["shot"]: c for c in run.get("wan_cuts") or []}
        if cuts.get(index) == cut:
            return
        if index in cuts or run.get("wan_quality") or run.get("wan_final_key"):
            raise LightError("LIGHT_PRODUCTION_CHANGED", "The saved shot cannot be replaced.", 409)
        cuts[index] = cut
        row.light_production = {**form, "run": {**run, "wan_cuts": [cuts[i] for i in sorted(cuts)]}}
        row.updated_at = _now()


async def save_soda_visual_review(ctx, prefix: str, *, original: list[dict], cuts: list[dict], report: dict) -> dict:
    """Apply a single visual edit while the final composition is still unsubmitted."""
    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        run = form.get("run") or {}
        if (form.get("template") != wan_story.SODA_TEMPLATE or run.get("job_prefix") != prefix
                or run.get("wan_final_key") or run.get("wan_cuts") != original
                or len(original) != wan_story.shot_count(run)
                or [(c["shot"], c["source"]) for c in original] != [(c["shot"], c["source"]) for c in cuts]):
            raise LightError("LIGHT_PRODUCTION_CHANGED", "The visual edit belongs to another composition.", 409)
        if run.get("soda_visual_review"):
            return run
        run = {**run, "wan_cuts": cuts, "soda_visual_review": report}
        row.light_production = {**form, "run": run}
        row.updated_at = _now()
        return run


async def save_wan_edit(ctx, prefix: str, *, cuts: list[dict], final_key: str | None = None) -> dict:
    """Fence the edit alongside the owning receipt so retries reuse the same visual decisions."""
    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        run = form.get("run") or {}
        saved = run.get("wan_cuts") or []
        if (run.get("job_prefix") != prefix or run.get("remake_version") != wan_story.VERSION
                or saved != cuts[:len(saved)] or len(cuts) > len(saved)+1
                or [cut["shot"] for cut in cuts] != list(range(1, len(cuts)+1))
                or (final_key and (len(cuts) != wan_story.shot_count(run) or run.get("wan_final_key") not in (None, final_key)))):
            raise LightError("LIGHT_PRODUCTION_CHANGED", "The saved edit belongs to another version.", 409)
        run = {**run, "wan_cuts": cuts, **({"wan_final_key": final_key} if final_key else {})}
        row.light_production = {**form, "run": run}
        row.updated_at = _now()
        return run


async def save_reference_quality(ctx, prefix: str, quality: dict) -> dict:
    """Persist automatic review evidence and resumable corrections under the same run fence."""
    from light.reference_quality import VERSION, fingerprint

    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        run = form.get("run") or {}
        cuts = run.get("wan_cuts") or []
        if (run.get("job_prefix") != prefix or run.get("remake_version") != wan_story.VERSION
                or quality.get("version") != VERSION
                or [cut["shot"] for cut in cuts] != list(range(1, wan_story.shot_count(run) + 1))
                or quality.get("input_digest") != fingerprint(cuts)):
            raise LightError("LIGHT_PRODUCTION_CHANGED", "The quality review belongs to another edit.", 409)
        previous = run.get("wan_quality") or {}
        if run.get("wan_final_key") and previous.get("cuts") != quality.get("cuts"):
            raise LightError("LIGHT_PRODUCTION_CHANGED", "A submitted composition cannot change its selected takes.", 409)
        run = {**run, "wan_quality": quality}
        row.light_production = {**form, "run": run}
        row.updated_at = _now()
        return run


def _validate_action(row: Session, view: dict, action: str) -> None:
    if view["status"] in _BUSY or (view["status"] == "review" and action not in {"apply", "dismiss"}):
        raise LightError("LIGHT_PRODUCTION_STATE", "The production is already active or awaiting a decision", 409)
    if (view.get("shot_editor") or {}).get("active"):
        raise LightError("LIGHT_PRODUCTION_STATE", "Finish the current fragment action before continuing", 409)
    if (action in {"prepare", "render"} and view.get("template") == silk_road.TEMPLATE
            and not view["materials"]):
        raise LightError("LIGHT_PRODUCTION_ASSET", "先选择一张菜品照片或一段能看清菜品的实拍。")
    if action == "render":
        if view["status"] == "completed":
            raise LightError("LIGHT_PRODUCTION_STATE", "Edit the result before generating a new version", 409)
        form = _form(row)
        if (form.get("template") == silk_road.TEMPLATE and form.get("video_tier")
                and not (form.get("remake_adaptation") or {}).get("world")):
            raise LightError("LIGHT_VIDEO_FRAME_PLAN", "Prepare the current storyboard before filming.", 409)
        if row.light_entry == "poster" and not _form(row).get("poster_title", "").strip():
            raise LightError("LIGHT_PRODUCTION_POSTER", "Confirm the headline before generating the poster")
        if row.light_entry == "menu" and not any(dish["name"] for dish in view["dishes"]):
            raise LightError("LIGHT_PRODUCTION_DISHES", "Confirm at least one dish before generating")
        if row.light_entry == "footage_edit" and not view["materials"]:
            raise LightError("LIGHT_PRODUCTION_ASSET", "Select at least one original clip or photo")
        if row.light_entry == "video" and not remake_case.applies(row.light_entry, view) and not view["script"].strip():
            raise LightError("LIGHT_PRODUCTION_SCRIPT", "Confirm the script before generating")


def _select_remake_model(row: Session, form: dict) -> None:
    if row.light_entry == "video" and form["template"] in forms.VIDEO_TIER_TEMPLATES and form.get("video_tier"):
        row.video_model, row.video_resolution = video_tiers.resolve_tier(form["video_tier"])
        return
    exact_remake = remake_case.applies(row.light_entry, form)
    if row.light_entry != "video" or not form["template"] or (row.video_model and not exact_remake):
        return
    from core.config import get_config
    from tool.video_model_card import default_resolution

    config = get_config()
    soda_uses_wan = form["template"] == wan_story.SODA_TEMPLATE and row.video_model == wan_story.MODEL
    if exact_remake and (form["template"] in {wan_story.TEMPLATE, silk_road.TEMPLATE} or soda_uses_wan):
        resolution = (row.video_resolution or "720p") if soda_uses_wan else wan_story.RESOLUTION
        if form["template"] == silk_road.TEMPLATE:
            resolution = silk_road.RESOLUTION
        model = next((item for item in config.video_generation.models
                      if item.id == wan_story.MODEL and item.channel == "dashscope"), None)
        if model is None or resolution not in model.resolutions:
            raise LightError("LIGHT_REMAKE_MODEL", f"Wan 3.0 {resolution} is unavailable for this template.", 409)
        row.video_model, row.video_resolution = model.id, resolution
        return
    if exact_remake:
        model = next((item for item in config.video_generation.models
                      if item.id == remake_case.MODEL and item.channel == "runninghub"), None)
        if model is None or remake_case.RESOLUTION not in model.resolutions:
            raise LightError("LIGHT_REMAKE_MODEL", "This template needs RunningHub MiniMax H3 Turbo at 768p.", 409)
        row.video_model, row.video_resolution = model.id, remake_case.RESOLUTION
        return
    model = next((item for item in config.video_generation.models if item.id == "wan3.0-video"), None)
    if model:
        row.video_model = model.id
        row.video_resolution = "720p" if "720p" in model.resolutions else default_resolution(config, model.id)



async def action(user_id: str, workspace_id: str, session_id: str, body: forms.FormAction, lang: str | None) -> dict:
    from agent.inbox import InboxAttachmentError

    queued = False
    async with get_db_session() as db:
        row = await _lock(db, session_id, user_id, workspace_id)
        form = _form(row)
        if (form.get("run") or {}).get("client_id") == body.request_id:
            if form["run"]["action"] != body.action:
                raise LightError("LIGHT_PRODUCTION_CHANGED", "Request already accepted for a different action", 409)
            queued = True
        else:
            _check_revision(form, body.revision)
            view = (await _views(db, [row], include_shots=body.action == "edit"))[0]
            _validate_action(row, view, body.action)
            if body.action == "render" and row.light_entry == "footage_edit":
                from light.production_narration import check_confirmation

                check_confirmation(form, body.narration_preview_id, lang or "zh-CN")
            if body.action == "edit":
                page = "style" if row.light_entry == "footage_edit" else "review"
                row.light_production = {**form, "revision": form["revision"] + 1, "page": page, "run": None,
                                        "edited_at": _now().isoformat()}
            elif body.action in {"apply", "dismiss"}:
                proposal = form.get("proposal")
                if not proposal:
                    raise LightError("LIGHT_PRODUCTION_STATE", "There is no pending proposal", 409)
                row.light_production = {**form, "revision": form["revision"] + 1, "proposal": None}
                if body.action == "apply":
                    await _validate_fields(db, row, proposal["changes"])
                    _apply(row, proposal["changes"], from_agent=True)
            else:
                from core.config import get_config

                if row.light_example_id:
                    await _require_reference(db, row.light_entry, row.light_example_id)
                config = get_config()
                if config.model_tiers.product_chat and not _chat_tier(config, model=row.model):
                    tier = _chat_tier(config, model=config.model)
                    row.model, row.variant = tier.model, tier.variant
                creative_motion = False
                if body.action == "render":
                    _select_remake_model(row, form)
                    video_tier = form.get("video_tier") or "fast"
                    creative_motion = (
                        row.light_entry == "footage_edit" and form.get("creative_effects") is True
                        and video_tier in video_tiers.available_tiers(config)
                    )
                    if creative_motion:
                        row.video_model, row.video_resolution = video_tiers.resolve_tier(video_tier)
                prefix = f"light:{row.id}:{form['revision']}"
                exact_remake = remake_case.applies(row.light_entry, form)
                version = remake_case.version_for(form) if exact_remake else None
                if exact_remake:
                    prefix = f"{prefix}:{version}"
                existing = [{"id": j.id, "kind": j.kind, "key": j.idempotency_key, "status": j.status,
                             "asset_id": j.output_asset_id} for j in (await db.scalars(select(VideoJob).where(
                                VideoJob.session_id == row.id, VideoJob.idempotency_key.startswith(prefix + ":"),
                             ))).all()]
                if exact_remake and body.action == "render":
                    from tool.tool import ToolContext
                    from tool.video_production import remaining_daily_submits

                    existing_shots = {job["key"].removeprefix(prefix + ":").partition(":")[0] for job in existing
                                      if job["kind"] == "segment" and job["key"].startswith(prefix + ":shot")
                                      and job["status"] not in {"failed", "cancelled"}}
                    total = (wan_story.shot_count({"remake_adaptation": form.get("remake_adaptation")})
                             if version == wan_story.VERSION else len(remake_case.manifest()["shots"]))
                    required = max(0, total - len(existing_shots))
                    remaining = await remaining_daily_submits(ToolContext(user_id=user_id))
                    if remaining is not None and remaining < required:
                        raise LightError("LIGHT_REMAKE_VIDEO_LIMIT",
                                         f"The remake needs {required} video submissions; {remaining} remain in 24 hours.",
                                         429, needed=required, remaining=remaining)
                reference = remake_case.reference_copy(form) if exact_remake else ""
                prompt = forms.prompt_label(row.light_entry, body.action, lang, bool(form["script"]))
                if reference:
                    prompt += "\n\n" + reference
                try:
                    receipt = await accept_inbox_item_locked(
                        db, row, delivery="followup", prompt=prompt, attachments=form["asset_ids"],
                        client_id=f"light:{body.request_id}", agent=row.agent, model=row.model, variant=row.variant,
                        video_model=row.video_model, video_resolution=row.video_resolution,
                        origin="human", origin_ref={"actor_user_id": user_id, "entrypoint": "light_production",
                                                    "action": body.action},
                    )
                except InboxAttachmentError as exc:
                    raise LightError("LIGHT_PRODUCTION_ASSET", "A selected material is no longer available") from exc
                row.light_production = {**form, "run": {"id": receipt.id, "action": body.action,
                                        "client_id": body.request_id, "revision": form["revision"], "job_prefix": prefix,
                                        "existing": existing,
                                        **({"confirmed_narration": {"shots": form["narration_draft"]["shots"],
                                                                     "preview": form["narration_preview"]}}
                                           if (body.action == "render" and row.light_entry == "footage_edit"
                                               and form.get("redo_narration")) else {}),
                                        **({"creative_motion": True} if creative_motion else {}),
                                        **({"video_model": row.video_model, "video_resolution": row.video_resolution,
                                            "music_style": production_music.preference(form),
                                            "music_template": form["template"]}
                                           if body.action == "render" and row.light_entry == "video" else {})}}
                if exact_remake:
                    prior = form.get("run") or {}
                    preserved = prior.get("remake_adaptation") if prior.get("job_prefix") == prefix else None
                    if form.get("template") == silk_road.TEMPLATE and body.action == "render":
                        resuming = prior.get("action") == "render" and prior.get("job_prefix") == prefix
                        controls = prior.get("silk_controls") if resuming else {
                            "beat_music": form.get("beat_music", True),
                            "align_handoff": True,
                        }
                    else:
                        controls = None
                    row.light_production = {**row.light_production, "run": {
                        **row.light_production["run"], "remake_version": version, "remake_adaptation": preserved,
                        **({"silk_controls": controls} if controls is not None else {}),
                        **({k: prior[k] for k in ("wan_cuts", "wan_final_key", "wan_review_version", "wan_edit_history", "wan_quality", "wan_coverage", "soda_visual_review", "shot_inputs") if k in prior}
                           if version == wan_story.VERSION and prior.get("job_prefix") == prefix else {}),
                    }}
                queued = True
            row.updated_at = _now()
    if queued:
        schedule_inbox_wake(session_id, user_id)
    return await get(user_id, workspace_id, session_id)
