"""Project CRUD.

A project is the directory sessions run in, so these routes are what let a user
pick up an existing body of work instead of starting every conversation in an
empty folder.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from core.log import create_logger
from project import brief, workspace

log = create_logger("api.projects")

router = APIRouter(dependencies=[Depends(get_workspace)])
# Project briefs live at /api/projects/{id}/brief (the design's public path);
# the project CRUD above is mounted by main.py under /api/agent/project.
brief_router = APIRouter(prefix="/api/projects", tags=["projects"], dependencies=[Depends(get_workspace)])


class CreateProjectBody(BaseModel):
    name: str
    slug: str | None = None
    description: str | None = None


class UpdateProjectBody(BaseModel):
    name: str


@router.get("/project")
async def list_projects(current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    workspace_id = current_user["workspace_id"]
    # Guarantees the picker is never empty, even for a brand new account.
    await workspace.ensure_default_project(user_id, workspace_id)
    projects = await workspace.list_projects(workspace_id)
    counts = await workspace.session_counts(workspace_id, user_id=user_id)
    out = []
    for p in projects:
        p.session_count = counts.get(p.id, 0)
        out.append(p.to_dict())
    return out


@router.post("/project")
async def create_project(
    body: CreateProjectBody,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["user_id"]
    try:
        project = await workspace.create_project(
            user_id, current_user["workspace_id"], body.name,
            slug=body.slug, description=body.description
        )
    except workspace.ProjectError as e:
        raise HTTPException(400, str(e))

    # Best effort: the directory is also created on the path that starts a run,
    # so a sandbox that is down right now does not block creating the project.
    try:
        from sandbox import sandbox_manager
        client = await sandbox_manager.get_client_any(
            user_id=user_id, workspace_id=current_user["workspace_id"]
        )
        if client:
            await workspace.ensure_directory(client, project.slug)
    except Exception as e:
        log.debug(f"Deferred directory creation for {project.slug}: {e}")

    return project.to_dict()


@router.get("/project/{project_id}")
async def get_project(project_id: str, current_user: dict = Depends(get_current_user)):
    project = await workspace.get_project(
        project_id, current_user["user_id"], current_user["workspace_id"]
    )
    if not project:
        raise HTTPException(404, "Project not found")
    counts = await workspace.session_counts(current_user["workspace_id"], user_id=current_user["user_id"])
    project.session_count = counts.get(project.id, 0)
    return project.to_dict()


@router.patch("/project/{project_id}")
async def update_project(
    project_id: str,
    body: UpdateProjectBody,
    current_user: dict = Depends(get_current_user),
):
    try:
        project = await workspace.rename_project(
            project_id,
            current_user["user_id"],
            current_user["workspace_id"],
            body.name,
        )
    except workspace.ProjectError as e:
        raise HTTPException(400, str(e))
    return project.to_dict()


@router.delete("/project/{project_id}")
async def delete_project(project_id: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    client = None
    try:
        from sandbox import sandbox_manager
        client = await sandbox_manager.get_client_any(
            user_id=user_id, workspace_id=current_user["workspace_id"]
        )
    except Exception as e:
        log.debug(f"No sandbox available while deleting {project_id}: {e}")
    try:
        await workspace.delete_project(
            project_id, user_id, current_user["workspace_id"], sandbox=client
        )
    except workspace.ProjectError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class BriefBody(BaseModel):
    # The service enforces the 6,000-character limit with its own error code;
    # this bound only caps the request size.
    content: str = Field(max_length=4 * brief.MAX_BRIEF_CHARS)
    expected_revision: int | None = Field(default=None, ge=0)


def _brief_error(exc: brief.ProjectBriefError) -> HTTPException:
    detail = {"code": exc.code, "message": str(exc)}
    if isinstance(exc, brief.ProjectBriefNotFound):
        return HTTPException(404, detail)
    if isinstance(exc, brief.ProjectBriefConflict):
        return HTTPException(409, {**detail, "current_revision": exc.current_revision})
    if isinstance(exc, brief.ProjectBriefTooLong):
        return HTTPException(422, {**detail, "max_chars": brief.MAX_BRIEF_CHARS})
    if isinstance(exc, brief.ProjectBriefSensitiveContent):
        return HTTPException(422, {**detail, "kind": exc.kind})
    return HTTPException(422, detail)


@brief_router.get("/{project_id}/brief")
async def get_project_brief(project_id: str, current_user: dict = Depends(get_current_user)):
    """The caller's brief for their own project; revision 0 means none was written yet."""
    try:
        current = await brief.get_brief(user_id=current_user["user_id"],
                                        workspace_id=current_user["workspace_id"], project_id=project_id)
    except brief.ProjectBriefError as exc:
        raise _brief_error(exc) from exc
    return current or {"id": None, "project_id": project_id, "workspace_id": current_user["workspace_id"],
                       "content": "", "revision": 0, "updated_by": None, "created_at": None, "updated_at": None}


@brief_router.put("/{project_id}/brief")
async def put_project_brief(project_id: str, body: BriefBody, current_user: dict = Depends(get_current_user)):
    """Replace the brief; ``expected_revision`` (0 for a new brief) guards against lost edits."""
    try:
        return await brief.update_brief(user_id=current_user["user_id"], workspace_id=current_user["workspace_id"],
                                        project_id=project_id, content=body.content,
                                        expected_revision=body.expected_revision, updated_by="user")
    except brief.ProjectBriefError as exc:
        raise _brief_error(exc) from exc
