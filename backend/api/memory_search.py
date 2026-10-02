"""Explicit authenticated memory search; scope is never model supplied."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from core.config import get_config
from db.base import get_db_session
from memory.policy import MemoryAccessDenied, resolve_access_scope

router = APIRouter(prefix='/api/memories', tags=['memories'], dependencies=[Depends(get_workspace)])


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    project_id: str | None = None
    limit: int | None = Field(default=None, ge=1, le=50)
    force_rerank: bool = False


@router.post('/search')
async def search(body: SearchBody, current_user: dict = Depends(get_current_user)):
    try:
        async with get_db_session() as db:
            scope = await resolve_access_scope(db, user_id=current_user['user_id'], workspace_id=current_user.get('workspace_id'),
                                              project_id=body.project_id)
        from memory.orchestrator import run_memory_context
        bundle = await run_memory_context(body.query, scope, get_config().memory, steps=['retrieval'], force_memory=True,
                                          limit=body.limit, force_rerank=body.force_rerank)
    except MemoryAccessDenied as exc:
        raise HTTPException(404, 'memory scope not found') from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Internal intermediate candidates are for the ACL-revalidated debug
    # store; only the final, freshly checked context crosses this endpoint.
    return {key: value for key, value in bundle.items() if key != 'candidates'}
