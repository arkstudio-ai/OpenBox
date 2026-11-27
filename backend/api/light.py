"""Light mode: the home entries, starting a guided topic chat, recent results, the mode switch.

* ``GET  /api/light/entries`` → the buttons with availability computed for this workspace
* ``POST /api/light/start``   → a real session with the entry's opening card already in it
* ``GET  /api/light/recent``  → latest image/video results grouped by conversation
* ``GET/PUT /api/light/mode`` → ``ui_mode`` (light | pro)
* ``GET/PUT /api/light/language`` → ``ui_language`` (zh-CN | en-US | vi-VN), the stored copy of the App's
  language setting; every request also sends it as ``Accept-Language`` (docs/LIGHT_MODE_I18N_PLAN.md §2.1)
* ``/api/light/autopilot``     → 省心托管: create from a result, list, pause/resume/change, cancel
"""
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from light import mode as light_mode
from light import examples, production, production_form, production_narration, production_voice, shot_editor, shot_frame_edits

router = APIRouter(prefix="/api/light", tags=["light"], dependencies=[Depends(get_workspace)])


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: str = Field(min_length=1, max_length=32)
    project_id: str | None = Field(default=None, max_length=64)
    subtype: str | None = Field(default=None, max_length=32)
    sample: str | None = Field(default=None, max_length=40)


class AutopilotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=96)
    asset_id: str | None = Field(default=None, max_length=96)
    task: Literal["video", "image", "listing", "reviews", "report"] | None = None
    repeat: Literal["daily", "weekdays", "weekly"] = "daily"
    time: str = Field(default="10:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    mode: Literal["make_only", "make_and_publish"] = "make_only"
    credits_cap: Decimal = Field(default=Decimal("10"), gt=0, le=Decimal("500"))
    note: str = Field(default="", max_length=200)


class AutopilotPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    repeat: Literal["daily", "weekdays", "weekly"] | None = None
    time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    credits_cap: Decimal | None = Field(default=None, gt=0, le=Decimal("500"))


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ui_mode: str = Field(min_length=1, max_length=8)


class LanguageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ui_language: str = Field(min_length=1, max_length=8)


def _scope(current_user: dict) -> tuple[str, str]:
    workspace_id = current_user.get("workspace_id")
    if not workspace_id:
        raise HTTPException(400, detail={"code": "WORKSPACE_REQUIRED", "message": "A workspace is required"})
    return current_user["user_id"], workspace_id


def _raise(exc: light_mode.LightError):
    raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc), **exc.extra})


@router.get("/entries")
async def entries(current_user: dict = Depends(get_current_user)):
    user_id, workspace_id = _scope(current_user)
    return await light_mode.entries_view(user_id, workspace_id, lang=current_user.get("ui_language"))


@router.post("/start")
async def start(body: StartBody, current_user: dict = Depends(get_current_user)):
    user_id, workspace_id = _scope(current_user)
    try:
        return await light_mode.start(user_id=user_id, workspace_id=workspace_id, entry_id=body.entry_id,
                                      project_id=body.project_id, subtype=body.subtype, sample=body.sample,
                                      lang=current_user.get("ui_language"))
    except light_mode.LightError as exc:
        _raise(exc)


@router.get("/examples")
async def production_examples(entry_id: production_form.EntryId, current_user: dict = Depends(get_current_user)):
    _scope(current_user)
    items = await examples.production_previews(entry_id)
    if entry_id == "video":
        from light.video_templates import catalogue
        items = await catalogue() + items
    return {"items": items}


@router.get("/examples/{example_id}/preview")
async def example_preview(example_id: str, current_user: dict = Depends(get_current_user)):
    """Fresh links for one example on the pick card (the card's own links last a week)."""
    from light import video_templates
    if example_id.startswith(video_templates.PREFIX):
        try:
            return await video_templates.selected_card(example_id)
        except light_mode.LightError as exc:
            _raise(exc)
    row = await examples.get_active(example_id)
    shown = examples.preview(row) if row is not None else None
    if shown is None:
        raise HTTPException(404, detail={"code": "LIGHT_EXAMPLE_NOT_FOUND", "message": "No such example"})
    return shown


@router.post("/productions", status_code=201)
async def create_production(body: production_form.StartForm, current_user: dict = Depends(get_current_user)):
    user_id, workspace_id = _scope(current_user)
    try:
        return await production.create(user_id, workspace_id, body, current_user.get("ui_language"))
    except light_mode.LightError as exc:
        _raise(exc)


@router.get("/productions")
async def production_history(
    limit: int = Query(24, ge=1, le=50),
    offset: int = Query(0, ge=0),
    entry_id: production_form.EntryId | None = None,
    current_user: dict = Depends(get_current_user),
):
    return await production.history(*_scope(current_user), limit, offset, entry_id=entry_id)


@router.get("/productions/preview")
async def production_preview(entry_id: production_form.EntryId, brief: str = Query("", max_length=300),
                             current_user: dict = Depends(get_current_user)):
    try:
        return await production.preview(*_scope(current_user), production_form.StartForm(entry_id=entry_id, brief=brief),
                                        current_user.get("ui_language"))
    except light_mode.LightError as exc:
        _raise(exc)


@router.get("/productions/{session_id}")
async def get_production(session_id: str, current_user: dict = Depends(get_current_user)):
    try:
        return await production.get(*_scope(current_user), session_id)
    except light_mode.LightError as exc:
        _raise(exc)


@router.patch("/productions/{session_id}")
async def edit_production(session_id: str, body: production_form.EditForm,
                          current_user: dict = Depends(get_current_user)):
    try:
        return await production.edit(*_scope(current_user), session_id, body)
    except light_mode.LightError as exc:
        _raise(exc)


@router.post("/productions/{session_id}/actions")
async def production_action(session_id: str, body: production_form.FormAction,
                            current_user: dict = Depends(get_current_user)):
    try:
        return await production.action(*_scope(current_user), session_id, body, current_user.get("ui_language"))
    except light_mode.LightError as exc:
        _raise(exc)


class AuditionBody(BaseModel):
    voice_id: str = Field(min_length=1, max_length=64)


@router.get("/production-voices")
async def production_voices(current_user: dict = Depends(get_current_user)):
    user, _ = _scope(current_user)
    return await production_voice.catalogue(user, current_user.get("ui_language"))


@router.post("/production-voices")
async def production_voice_create(
    name: str = Form(min_length=1, max_length=32),
    request_id: str = Form(pattern=r"^[a-f0-9]{16,40}$"),
    audio: UploadFile = File(),
    consent: bool = Form(default=False),
    current_user: dict = Depends(get_current_user),
):
    user, _ = _scope(current_user)
    if not consent:
        _raise(light_mode.LightError("PRODUCTION_VOICE_CONSENT", "Confirm permission to use this voice."))
    formats = {"wav": "audio/wav", "m4a": "audio/mp4", "mp3": "audio/mpeg"}
    extension = (audio.filename or "").lower().rpartition(".")[2]
    sound = await audio.read(production_voice.MAX_SAMPLE_BYTES + 1)
    if not name.strip() or extension not in formats or not 0 < len(sound) <= production_voice.MAX_SAMPLE_BYTES:
        _raise(light_mode.LightError("PRODUCTION_VOICE_SAMPLE", "Choose a named WAV, M4A or MP3 up to 10 MB."))
    format_mime = formats[extension]
    try:
        return await production_voice.create(user, name.strip(), sound, format_mime, request_id,
                                             current_user.get("ui_language"))
    except light_mode.LightError as exc:
        _raise(exc)


@router.post("/production-voices/audition")
async def production_voice_audition(body: AuditionBody, current_user: dict = Depends(get_current_user)):
    user, workspace = _scope(current_user)
    try:
        return await production_voice.audition(user, workspace, body.voice_id, current_user.get("ui_language"))
    except light_mode.LightError as exc:
        _raise(exc)


@router.get("/productions/{session_id}/narration")
async def production_speech(session_id: str, current_user: dict = Depends(get_current_user)):
    try:
        return await production_narration.detect(*_scope(current_user), session_id)
    except light_mode.LightError as exc:
        _raise(exc)


@router.post("/productions/{session_id}/narration/draft")
async def production_narration_draft(
    session_id: str, body: production_narration.DraftRequest, current_user: dict = Depends(get_current_user),
):
    try:
        return await production_narration.draft(*_scope(current_user), session_id, body,
                                                current_user.get("ui_language") or "zh-CN")
    except light_mode.LightError as exc:
        _raise(exc)


@router.patch("/productions/{session_id}/narration/draft")
async def production_narration_edit(
    session_id: str, body: production_narration.PreviewRequest, current_user: dict = Depends(get_current_user),
):
    try:
        return await production_narration.edit_lines(*_scope(current_user), session_id, body,
                                                     current_user.get("ui_language") or "zh-CN")
    except light_mode.LightError as exc:
        _raise(exc)


@router.post("/productions/{session_id}/narration/preview")
async def production_narration_preview(
    session_id: str, body: production_narration.PreviewRequest, current_user: dict = Depends(get_current_user),
):
    try:
        return await production_narration.preview(*_scope(current_user), session_id, body,
                                                  current_user.get("ui_language") or "zh-CN")
    except light_mode.LightError as exc:
        _raise(exc)


@router.post("/productions/{session_id}/shot-actions")
async def production_shot_action(session_id: str, body: shot_editor.ShotAction,
                                 current_user: dict = Depends(get_current_user)):
    try:
        return await shot_editor.action(*_scope(current_user), session_id, body)
    except light_mode.LightError as exc:
        _raise(exc)


@router.post("/productions/{session_id}/shot-frames", status_code=202)
async def production_shot_frame(session_id: str, body: shot_frame_edits.FrameRequest,
                                current_user: dict = Depends(get_current_user)):
    try:
        return await shot_frame_edits.submit(*_scope(current_user), session_id, body)
    except light_mode.LightError as exc:
        _raise(exc)


@router.get("/recent")
async def recent(limit: int = Query(8, ge=1, le=30), current_user: dict = Depends(get_current_user)):
    user_id, workspace_id = _scope(current_user)
    return await light_mode.recent(user_id, workspace_id, limit=limit)


@router.get("/mode")
async def get_mode(current_user: dict = Depends(get_current_user)):
    return {"ui_mode": await light_mode.ui_mode(current_user["user_id"])}


@router.put("/mode")
async def put_mode(body: ModeBody, current_user: dict = Depends(get_current_user)):
    try:
        return {"ui_mode": await light_mode.set_ui_mode(current_user["user_id"], body.ui_mode)}
    except light_mode.LightError as exc:
        _raise(exc)


@router.get("/language")
async def get_language(current_user: dict = Depends(get_current_user)):
    from core.ui_language import ui_language_for
    return {"ui_language": await ui_language_for(current_user["user_id"])}


@router.put("/language")
async def put_language(body: LanguageBody, current_user: dict = Depends(get_current_user)):
    from core.ui_language import set_ui_language
    try:
        return {"ui_language": await set_ui_language(current_user["user_id"], body.ui_language)}
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "LIGHT_LANGUAGE_INVALID", "message": str(exc)})


@router.get("/autopilot")
async def autopilot_list(current_user: dict = Depends(get_current_user)):
    from light import autopilot
    user_id, workspace_id = _scope(current_user)
    return await autopilot.list_view(user_id, workspace_id)


@router.post("/autopilot")
async def autopilot_create(body: AutopilotBody, current_user: dict = Depends(get_current_user)):
    from light import autopilot
    user_id, workspace_id = _scope(current_user)
    try:
        return await autopilot.create(user_id=user_id, workspace_id=workspace_id, session_id=body.session_id,
                                      asset_id=body.asset_id, task=body.task, repeat=body.repeat, time=body.time,
                                      mode=body.mode, cap=body.credits_cap, note=body.note)
    except light_mode.LightError as exc:
        _raise(exc)
    except ValueError as exc:  # cron quota / validation
        raise HTTPException(400, detail={"code": "LIGHT_AUTOPILOT_REFUSED", "message": str(exc)[:300]})


@router.patch("/autopilot/{job_id}")
async def autopilot_patch(job_id: str, body: AutopilotPatch, current_user: dict = Depends(get_current_user)):
    from light import autopilot
    user_id, workspace_id = _scope(current_user)
    try:
        view = None
        if body.repeat or body.time or body.credits_cap:
            view = await autopilot.update(user_id, workspace_id, job_id, repeat=body.repeat, time=body.time,
                                          cap=body.credits_cap)
        if body.enabled is not None:
            view = await autopilot.set_enabled(user_id, workspace_id, job_id, body.enabled)
        return view or autopilot.view(await autopilot._job(user_id, workspace_id, job_id))
    except light_mode.LightError as exc:
        _raise(exc)


@router.delete("/autopilot/{job_id}")
async def autopilot_cancel(job_id: str, current_user: dict = Depends(get_current_user)):
    from light import autopilot
    user_id, workspace_id = _scope(current_user)
    try:
        return await autopilot.cancel(user_id, workspace_id, job_id)
    except light_mode.LightError as exc:
        _raise(exc)
