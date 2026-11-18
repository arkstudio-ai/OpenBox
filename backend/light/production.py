"""Guided production state, saved on the same row as the Agent conversation."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from agent.inbox import accept_inbox_item_locked, schedule_inbox_wake
from assets.service import owned_ready_query
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.file_asset import FileAsset
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from db.models.video_job import VideoJob
from light import production_form as forms
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
    return row.light_production or forms.initial_form(row.light_entry, "", "")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def create(user_id: str, workspace_id: str, body: forms.StartForm, lang: str | None) -> dict:
    from agent.agent import default_agent_name
    from agent.model_resolve import resolve
    from auth.quota import check_session_quota
    from core.config import get_config
    from light.entries import light_entries
    from light.mode import _workspace_facts, availability
    from session.session import create_session
    from store.service import get_store

    entry = light_entries(lang).entry(body.entry_id)
    if entry is None:
        raise _not_found()
    state = availability(entry, await _workspace_facts(workspace_id))
    if state != "ok":
        raise LightError("LIGHT_ENTRY_UNAVAILABLE", "Production is unavailable", 409, availability=state)
    config = get_config()
    await check_session_quota(user_id, config)
    shop = await get_store(workspace_id, user_id)
    name = (shop or {}).get("name") or ""
    model, _ = resolve("", config, context="light production")
    session = await create_session(
        model=model, agent=default_agent_name(), title=body.brief or entry.title, user_id=user_id,
        workspace_id=workspace_id, light_entry=body.entry_id,
        light_production=forms.initial_form(body.entry_id, name[:80], body.brief),
    )
    return await get(user_id, workspace_id, session.id)


def _media_matches(entry: str, mime: str) -> bool:
    return mime.startswith("image/") if entry == "menu" else mime.startswith(("image/", "video/"))


async def _validate_fields(db, row: Session, changes: dict) -> None:
    if "template" in changes and changes["template"] not in forms.TEMPLATES[row.light_entry]:
        raise LightError("LIGHT_PRODUCTION_TEMPLATE", "Invalid template for this production")
    if "dishes" in changes and row.light_entry != "menu":
        raise LightError("LIGHT_PRODUCTION_DISHES", "Only menus have dish rows")
    if "asset_ids" in changes:
        assets = (await db.scalars(owned_ready_query(row.user_id, row.workspace_id)
                                   .where(FileAsset.id.in_(changes["asset_ids"])))).all()
        if len(assets) != len(changes["asset_ids"]) or any(not _media_matches(row.light_entry, a.mime) for a in assets):
            raise LightError("LIGHT_PRODUCTION_ASSET", "A material is missing or unsupported")


def _apply(row: Session, changes: dict, *, from_agent: bool = False) -> None:
    form = dict(row.light_production)
    actual = {key: value for key, value in changes.items() if form.get(key) != value}
    if not actual:
        return
    form.update(actual)
    form["revision"] += 1
    if any(key != "page" for key in actual):
        form["edited_at"] = _now().isoformat()
    if from_agent:
        form["agent_revision"] = form["revision"]
    row.light_production = form
    row.updated_at = _now()


def _check_revision(form: dict, revision: int):
    if form["revision"] != revision:
        raise LightError("LIGHT_PRODUCTION_CHANGED", "The production changed. Reload its latest draft.", 409)


def _work_view(row: Session, receipt, jobs: list, output, question: bool) -> dict:
    form = _form(row)
    action = (form.get("run") or {}).get("action")
    active = any(job.status in _IN_FLIGHT for job in jobs)
    if row.status in {"busy", "compacting", "retry", "queued"} or active or (receipt and receipt.state in {"accepted", "claimed"}):
        if action == "prepare" and receipt and receipt.state in {"accepted", "claimed"}:
            phase = "recognize" if row.light_entry == "menu" else "copy"
        elif any(job.kind in _COMPOSITIONS for job in jobs):
            phase = "compose" if row.light_entry == "video" else "edit"
        elif action == "render" or active:
            phase = {"video": "video", "footage_edit": "edit", "menu": "menu"}[row.light_entry]
        else:
            phase = "agent"
        return {"status": "working", "phase": phase}
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
    return {"id": asset.id, "name": asset.name, "mime": asset.mime, "thumbnail": thumbnail}


def _output_candidates(row: Session, jobs: list, files: list) -> list[str]:
    form = _form(row)
    expected = "image/" if row.light_entry == "menu" else "video/"
    edited_at = form.get("edited_at")
    since = datetime.fromisoformat(edited_at) if edited_at else _utc(row.created_at)
    run = form.get("run") or {}
    needs_render = run.get("action") == "render" and (
        row.light_entry == "footage_edit" or (row.light_entry == "video" and form["template"])
    )
    results = [] if needs_render else [
        (_utc(part.created_at), part.data["asset_id"]) for part in files
        if _utc(part.created_at) >= since and str(part.data.get("mime_type", "")).startswith(expected)
        and part.data.get("asset_id") and (part.data.get("relation") or {}).get("role") in {"final", "result"}
    ]
    results.extend((_utc(job.completed_at or job.updated_at), job.output_asset_id) for job in jobs
                   if row.light_entry != "menu" and job.kind in _COMPOSITIONS and job.status == "completed" and job.output_asset_id)
    return list(dict.fromkeys(asset_id for _, asset_id in sorted(results, reverse=True)))


async def _views(db, rows: list[Session]) -> list[dict]:
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

    saved_assets = set()
    output_ids, relevant_jobs = {}, {}
    for row in rows:
        form = _form(row)
        stamp = form.get("edited_at")
        since = datetime.fromisoformat(stamp) if stamp else _utc(row.created_at)
        jobs = [job for job in jobs_by_session[row.id] if _utc(job.created_at) >= since]
        relevant_jobs[row.id] = jobs
        output_ids[row.id] = _output_candidates(row, jobs, files_by_session[row.id])
        saved_assets.update(output_ids[row.id])
        saved_assets.update(form.get("asset_ids", ()))
    assets = {a.id: a for a in (await db.scalars(owned_ready_query(rows[0].user_id, rows[0].workspace_id)
                                                .where(FileAsset.id.in_(saved_assets)))).all()} if saved_assets else {}
    try:
        oss = get_oss() if assets else None
    except OssNotConfigured:
        oss = None
    media = {asset_id: _asset_view(asset, oss) for asset_id, asset in assets.items()}
    result = []
    for row in rows:
        form = _form(row)
        output = next((media[key] for key in output_ids[row.id] if key in media), None)
        result.append({
            **{key: value for key, value in form.items() if key not in {"run", "edited_at"}},
            **_work_view(row, receipts.get((form.get("run") or {}).get("id")), relevant_jobs[row.id], output,
                         row.id in questions),
            "session_id": row.id, "entry_id": row.light_entry, "title": row.title, "legacy": not row.light_production,
            "updated_at": _utc(row.updated_at).isoformat(), "output": output,
            "materials": [media[key] for key in form["asset_ids"] if key in media],
        })
    return result


async def get(user_id: str, workspace_id: str, session_id: str) -> dict:
    async with get_db_session() as db:
        row = await db.scalar(_owned(user_id, workspace_id).where(Session.id == session_id))
        if row is None:
            raise _not_found()
        return (await _views(db, [row]))[0]


async def history(user_id: str, workspace_id: str, limit: int, offset: int) -> dict:
    async with get_db_session() as db:
        rows = list((await db.scalars(_owned(user_id, workspace_id, include_legacy=True)
                     .order_by(Session.updated_at.desc(), Session.id).offset(offset).limit(limit + 1))).all())
        return {"items": await _views(db, rows[:limit]), "has_more": len(rows) > limit}


async def edit(user_id: str, workspace_id: str, session_id: str, body: forms.EditForm) -> dict:
    async with get_db_session() as db:
        row = await _lock(db, session_id, user_id, workspace_id)
        _check_revision(row.light_production, body.revision)
        view = (await _views(db, [row]))[0]
        if view["status"] in _BUSY or view["status"] == "completed" or row.light_production.get("proposal"):
            raise LightError("LIGHT_PRODUCTION_STATE", "Finish the current production action before editing", 409)
        changes = body.model_dump(exclude_none=True, exclude={"revision"})
        await _validate_fields(db, row, changes)
        _apply(row, changes)
    return await get(user_id, workspace_id, session_id)


async def update_from_agent(ctx, *, revision: int, changes: dict, proposal: str | None) -> int:
    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        _check_revision(form, revision)
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


def _validate_action(row: Session, view: dict, action: str) -> None:
    if view["status"] in _BUSY or (view["status"] == "review" and action not in {"apply", "dismiss"}):
        raise LightError("LIGHT_PRODUCTION_STATE", "The production is already active or awaiting a decision", 409)
    if action == "render":
        if view["status"] == "completed":
            raise LightError("LIGHT_PRODUCTION_STATE", "Edit the result before generating a new version", 409)
        if row.light_entry == "menu" and not any(dish["name"] for dish in view["dishes"]):
            raise LightError("LIGHT_PRODUCTION_DISHES", "Confirm at least one dish before generating")
        if row.light_entry == "footage_edit" and not view["materials"]:
            raise LightError("LIGHT_PRODUCTION_ASSET", "Select at least one original clip or photo")
        if row.light_entry == "video" and not view["script"].strip():
            raise LightError("LIGHT_PRODUCTION_SCRIPT", "Confirm the script before generating")


def _select_remake_model(row: Session, form: dict) -> None:
    if row.light_entry != "video" or not form["template"] or row.video_model:
        return
    from core.config import get_config
    from tool.video_model_card import default_resolution

    config = get_config()
    # The reference remake was validated with native audio on this declared route.
    model = next((item for item in config.video_generation.models if item.id == "wan3.0-video"), None)
    if model:
        row.video_model = model.id
        row.video_resolution = "720p" if "720p" in model.resolutions else default_resolution(config, model.id)


async def action(user_id: str, workspace_id: str, session_id: str, body: forms.FormAction, lang: str | None) -> dict:
    from agent.inbox import InboxAttachmentError

    queued = False
    async with get_db_session() as db:
        row = await _lock(db, session_id, user_id, workspace_id)
        form = row.light_production
        if (form.get("run") or {}).get("client_id") == body.request_id:
            if form["run"]["action"] != body.action:
                raise LightError("LIGHT_PRODUCTION_CHANGED", "Request already accepted for a different action", 409)
            queued = True
        else:
            _check_revision(form, body.revision)
            view = (await _views(db, [row]))[0]
            _validate_action(row, view, body.action)
            if body.action == "edit":
                row.light_production = {**form, "revision": form["revision"] + 1, "page": "review", "run": None,
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
                if body.action == "render":
                    _select_remake_model(row, form)
                prefix = f"light:{row.id}:{form['revision']}"
                existing = [{"id": j.id, "kind": j.kind, "key": j.idempotency_key, "status": j.status,
                             "asset_id": j.output_asset_id} for j in (await db.scalars(select(VideoJob).where(
                                VideoJob.session_id == row.id, VideoJob.idempotency_key.startswith(prefix + ":"),
                             ))).all()]
                prompt = forms.prompt_label(row.light_entry, body.action, lang, bool(form["script"]))
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
                                        "existing": existing}}
                queued = True
            row.updated_at = _now()
    if queued:
        schedule_inbox_wake(session_id, user_id)
    return await get(user_id, workspace_id, session_id)
