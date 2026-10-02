"""Authenticated personal-memory commands and immutable evidence views."""
from fastapi import APIRouter, Depends, HTTPException, Query
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
    limit: int = Query(50, ge=1, le=100), current_user: dict = Depends(get_current_user)):
    rows = await _call(memory_service.search_memories(**_identity(current_user), project_id=project_id,
        type=type, scope=scope, status=status or ("DEPRECATED" if confirmation_status == "REJECTED" else None),
        confirmation_status=confirmation_status, limit=limit, include_candidates=True,
        include_all_projects=project_id is None))
    return {"memories": rows}


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
