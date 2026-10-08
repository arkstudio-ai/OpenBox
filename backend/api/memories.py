"""Authenticated personal-memory commands and immutable evidence views."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from memory import service as memory_service
from memory.policy import MemoryAccessDenied

router = APIRouter(prefix="/api/memories", tags=["memories"], dependencies=[Depends(get_workspace)])


class CommandBody(BaseModel):
    expected_revision: int | None = Field(default=None, ge=1)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class CreateNoteBody(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    project_id: str | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class ConfirmBody(CommandBody):
    edited_summary: str | None = Field(default=None, min_length=1, max_length=2000)


class EditNoteBody(CommandBody):
    summary: str = Field(min_length=1, max_length=2000)


class SettingsBody(BaseModel):
    auto_save: bool


class SessionPauseBody(BaseModel):
    paused: bool


class ForgetAllBody(BaseModel):
    project_id: str | None = None
    # Spelled out by the client after the person confirms; never a default.
    confirm: str = Field(pattern="^forget-all$")


class ForgetBody(CommandBody):
    mode: str = "memory"
    source_ids: list[str] = Field(default_factory=list, max_length=100)


def _identity(user):
    return {"user_id": user["user_id"], "workspace_id": user.get("workspace_id")}


async def _call(operation, *, missing="memory not found"):
    try:
        result = await operation
    except memory_service.MemoryConflict as exc:
        raise HTTPException(409, {"code": "MEMORY_REVISION_CONFLICT", "message": str(exc)}) from exc
    except memory_service.MemorySensitiveContent as exc:
        raise HTTPException(422, {"code": "MEMORY_SENSITIVE_CONTENT", "message": str(exc), "kind": exc.kind}) from exc
    except MemoryAccessDenied as exc:
        raise HTTPException(404, "memory or scope not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if result is None or result is False:
        raise HTTPException(404, missing)
    return result


@router.get("")
async def list_memories(type: str | None = None, scope: str | None = None, status: str | None = None,
    project_id: str | None = None, confirmation_status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
    query: Annotated[str | None, Query(max_length=200)] = None, current_user: dict = Depends(get_current_user)):
    """Newest first, a page at a time, optionally narrowed to memories whose text contains ``query``."""
    rows, next_offset = await _call(memory_service.page_memories(**_identity(current_user), project_id=project_id,
        type=type, scope=scope, status=status or ("DEPRECATED" if confirmation_status == "REJECTED" else None),
        confirmation_status=confirmation_status, limit=limit, include_candidates=True,
        include_all_projects=project_id is None, query=query, offset=offset, newest_first=True))
    return {"memories": rows, "next_offset": next_offset}


@router.get("/processing")
async def processing(project_id: str | None = None, current_user: dict = Depends(get_current_user)):
    """Turns still being saved, and ones that could not be, so nothing is lost silently."""
    from memory.jobs import processing_status
    return await _call(processing_status(**_identity(current_user), project_id=project_id))


async def _job_command(command, job_id: str, current_user: dict):
    from db.base import get_db_session
    from memory.policy import resolve_access_scope
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **_identity(current_user))
    done = await command(job_id, user_id=current_user["user_id"], workspace_id=access.workspace_id)
    if not done:
        raise HTTPException(404, "nothing to do for this turn")
    return {"ok": True}


@router.post("/processing/{job_id}/retry")
async def retry_processing(job_id: str, current_user: dict = Depends(get_current_user)):
    from memory.jobs import replay_job
    return await _job_command(replay_job, job_id, current_user)


@router.post("/processing/{job_id}/dismiss")
async def dismiss_processing(job_id: str, current_user: dict = Depends(get_current_user)):
    from memory.jobs import dismiss_job
    return await _job_command(dismiss_job, job_id, current_user)


@router.get("/learned-from/{session_id}")
async def learned_from(session_id: str, current_user: dict = Depends(get_current_user)):
    """Shown before a chat is deleted: deleting it withdraws what was learned from it."""
    return {"count": await _call(memory_service.count_learned_from_session(**_identity(current_user),
                                                                          session_id=session_id))}


@router.get("/recalled/{session_id}")
async def recalled(session_id: str, current_user: dict = Depends(get_current_user)):
    """What each reply in a chat drew on: {user message id: memories}, as they read now."""
    from memory.recalls import for_session
    found = await for_session(**_identity(current_user), session_id=session_id)
    if found is None:
        raise HTTPException(404, "session not found")
    return {"recalls": found}


@router.get("/settings")
async def memory_settings(session_id: str | None = None, current_user: dict = Depends(get_current_user)):
    from memory.settings import get_settings
    return await get_settings(current_user["user_id"], session_id)


@router.put("/settings")
async def update_memory_settings(body: SettingsBody, current_user: dict = Depends(get_current_user)):
    from memory.settings import update_settings
    return await update_settings(current_user["user_id"], auto_save=body.auto_save)


@router.put("/settings/sessions/{session_id}")
async def pause_session(session_id: str, body: SessionPauseBody, current_user: dict = Depends(get_current_user)):
    from memory.settings import update_settings
    try:
        return await update_settings(current_user["user_id"], session_id=session_id, session_paused=body.paused)
    except LookupError as exc:
        raise HTTPException(404, "chat not found") from exc


@router.get("/export", response_class=PlainTextResponse)
async def export_memories(lang: Annotated[str, Query(max_length=16)] = "zh-CN",
                          current_user: dict = Depends(get_current_user)):
    text = await _call(memory_service.export_markdown(**_identity(current_user), lang=lang))
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="memories.md"'})


@router.post("/forget-all")
async def forget_all(body: ForgetAllBody, current_user: dict = Depends(get_current_user)):
    return {"forgotten": await _call(memory_service.forget_all(**_identity(current_user), project_id=body.project_id))}


@router.get("/pending")
async def list_pending(current_user: dict = Depends(get_current_user)):
    rows = await _call(memory_service.search_memories(**_identity(current_user), status="CANDIDATE",
        include_candidates=True, include_all_projects=True, limit=100))
    return {"memories": rows}


@router.post("")
async def create_note(body: CreateNoteBody, current_user: dict = Depends(get_current_user)):
    return await _call(memory_service.create_note(**_identity(current_user), project_id=body.project_id,
        summary=body.summary, request_id=body.request_id))


@router.post("/{memory_id}/confirm")
async def confirm_proposal(memory_id: str, body: ConfirmBody | None = None, current_user: dict = Depends(get_current_user)):
    return await _call(memory_service.confirm_note(**_identity(current_user), proposal_id=memory_id,
        edited_summary=body.edited_summary if body else None, expected_revision=body.expected_revision if body else None,
        request_id=body.request_id if body else None), missing="pending proposal not found")


@router.post("/{memory_id}/reject")
async def reject_proposal(memory_id: str, body: CommandBody | None = None, current_user: dict = Depends(get_current_user)):
    await _call(memory_service.reject_note(**_identity(current_user), proposal_id=memory_id,
        expected_revision=body.expected_revision if body else None, request_id=body.request_id if body else None),
        missing="pending proposal not found")
    return {"ok": True}


@router.patch("/{memory_id}")
async def edit_note(memory_id: str, body: EditNoteBody, current_user: dict = Depends(get_current_user)):
    return await _call(memory_service.edit_note(**_identity(current_user), memory_id=memory_id, summary=body.summary,
        expected_revision=body.expected_revision, request_id=body.request_id))


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, expected_revision: int | None = None, request_id: str | None = None,
                        current_user: dict = Depends(get_current_user)):
    await _call(memory_service.delete_memory(**_identity(current_user), memory_id=memory_id,
                                            expected_revision=expected_revision, request_id=request_id))
    return {"ok": True, "status": "stopped_cleanup_pending", "original_chat_deleted": False}


@router.post("/{memory_id}/forget")
async def forget_memory(memory_id: str, body: ForgetBody, current_user: dict = Depends(get_current_user)):
    return await _call(memory_service.forget_memory(**_identity(current_user), memory_id=memory_id,
        expected_revision=body.expected_revision, request_id=body.request_id, mode=body.mode, source_ids=body.source_ids))


@router.get("/{memory_id}/history")
async def get_history(memory_id: str, current_user: dict = Depends(get_current_user)):
    return {"revisions": await _call(memory_service.get_history(**_identity(current_user), memory_id=memory_id))}


@router.get("/{memory_id}/sources")
async def get_sources(memory_id: str, current_user: dict = Depends(get_current_user)):
    return {"sources": await _call(memory_service.get_sources(**_identity(current_user), memory_id=memory_id))}


@router.get("/{memory_id}/cleanup")
async def get_cleanup(memory_id: str, current_user: dict = Depends(get_current_user)):
    return await _call(memory_service.cleanup_status(**_identity(current_user), memory_id=memory_id))


# Declared last: a single-segment path would otherwise shadow the fixed ones above.
@router.get("/{memory_id}")
async def get_memory(memory_id: str, current_user: dict = Depends(get_current_user)):
    """What the detail view shows: the current revision, re-authorized on every read."""
    return await _call(memory_service.get_memory(**_identity(current_user), memory_id=memory_id))
