"""Owner-only experience review. No model can mark its own work verified."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from memory import experiences
from memory.policy import MemoryAccessDenied
from memory.service import MemoryConflict

router = APIRouter(prefix="/api/memories/experiences", tags=["memories"], dependencies=[Depends(get_workspace)])


class Version(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class Review(Version):
    passed: bool
    notes: str = Field(min_length=10, max_length=3000)


class Revision(Version):
    content: experiences.Procedure


async def call(function, user, **kwargs):
    try:
        return await function(user_id=user["user_id"], workspace_id=user["workspace_id"], **kwargs)
    except MemoryConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except MemoryAccessDenied as exc:
        raise HTTPException(404, "Experience is unavailable") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("")
async def listing(project_id: str | None = None, offset: int = Query(default=0, ge=0, le=10000),
                  user=Depends(get_current_user)):
    return await call(experiences.listing, user, project_id=project_id, offset=offset)


@router.post("/{identity}/review")
async def review(identity: str, body: Review, user=Depends(get_current_user)):
    return await call(experiences.review, user, identity=identity, **body.model_dump())


@router.put("/{identity}")
async def revise(identity: str, body: Revision, user=Depends(get_current_user)):
    return await call(experiences.revise, user, identity=identity, **body.model_dump())


@router.post("/{identity}/enable")
async def enable(identity: str, body: Version, user=Depends(get_current_user)):
    return await call(experiences.enable, user, identity=identity, **body.model_dump())


@router.post("/{identity}/retire")
async def retire(identity: str, body: Version, user=Depends(get_current_user)):
    return await call(experiences.retire, user, identity=identity, **body.model_dump())
