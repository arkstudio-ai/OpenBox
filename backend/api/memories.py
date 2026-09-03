"""User memory management for a settings UI.

The agent-facing surface is the `creator_context` tool; this router is the
user-authoritative path: list what the agent knows, confirm or reject parked
proposals, and hand-maintain USER_NOTE entries. Ownership is enforced per
query (rows of another user read as 404, never 403).
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from core.log import create_logger
from memory import service as memory_service

log = create_logger("api.memories")

router = APIRouter(
    prefix="/api/memories", tags=["memories"], dependencies=[Depends(get_workspace)]
)


class CreateNoteBody(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    project_id: str | None = None


class ConfirmBody(BaseModel):
    edited_summary: str | None = Field(default=None, max_length=2000)


class EditNoteBody(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)


@router.get("")
async def list_memories(
    type: str | None = None,
    scope: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    rows = await memory_service.search_memories(
        user_id=current_user["user_id"],
        workspace_id=current_user.get("workspace_id"),
        type=type,
        scope=scope,
        status=status,
        limit=limit,
    )
    if status is None:
        rows = [row for row in rows if row["status"] != "DEPRECATED"]
    return {"memories": rows}


@router.get("/pending")
async def list_pending(current_user: dict = Depends(get_current_user)):
    rows = await memory_service.search_memories(
        user_id=current_user["user_id"],
        workspace_id=current_user.get("workspace_id"),
        type=memory_service.PENDING_NOTE_TYPE,
        status="CANDIDATE",
        limit=100,
    )
    return {"memories": rows}


@router.post("")
async def create_note(body: CreateNoteBody, current_user: dict = Depends(get_current_user)):
    row = await memory_service.create_note(
        user_id=current_user["user_id"],
        workspace_id=current_user.get("workspace_id"),
        project_id=body.project_id,
        summary=body.summary,
    )
    return row


@router.post("/{memory_id}/confirm")
async def confirm_proposal(
    memory_id: str, body: ConfirmBody | None = None, current_user: dict = Depends(get_current_user)
):
    row = await memory_service.confirm_note(
        user_id=current_user["user_id"],
        workspace_id=current_user.get("workspace_id"),
        proposal_id=memory_id,
        edited_summary=body.edited_summary if body else None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="pending proposal not found")
    return row


@router.post("/{memory_id}/reject")
async def reject_proposal(memory_id: str, current_user: dict = Depends(get_current_user)):
    ok = await memory_service.reject_note(
        user_id=current_user["user_id"],
        workspace_id=current_user.get("workspace_id"),
        proposal_id=memory_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="pending proposal not found")
    return {"ok": True}


@router.patch("/{memory_id}")
async def edit_note(
    memory_id: str, body: EditNoteBody, current_user: dict = Depends(get_current_user)
):
    row = await memory_service.edit_note(
        user_id=current_user["user_id"], workspace_id=current_user.get("workspace_id"),
        memory_id=memory_id, summary=body.summary
    )
    if row is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return row


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, current_user: dict = Depends(get_current_user)):
    ok = await memory_service.delete_memory(
        user_id=current_user["user_id"],
        workspace_id=current_user.get("workspace_id"), memory_id=memory_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="memory not found")
    return {"ok": True}
