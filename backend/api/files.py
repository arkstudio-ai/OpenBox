import base64
import re
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from auth.middleware import get_current_user
from sandbox import provider
from models.container import ListFilesRequest
from project.workspace import asset_sandbox_path, user_directory

_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
_UPLOAD_CHUNK = 48 * 1024  # keeps each base64 shell command well under ARG_MAX

router = APIRouter(
    prefix="/api/containers/{container_id}/files",
    tags=["files"],
    dependencies=[Depends(get_current_user)],
)


def _tenant_path(user_id: str, path: str) -> str:
    """Confine file-browser forwarding to the authenticated tenant namespace."""
    root = PurePosixPath(user_directory(user_id))
    requested = PurePosixPath(path or str(root))
    if ".." in requested.parts:
        raise HTTPException(status_code=403, detail="Path is outside your workspace")
    if str(requested) == "/workspace":
        return str(root)
    if requested != root and root not in requested.parents:
        raise HTTPException(status_code=403, detail="Path is outside your workspace")
    return str(requested)


async def _file_scope_root(
    user_id: str,
    *,
    session_id: str = "",
    project_id: str = "",
) -> str:
    """Resolve a browser-visible file scope to one owned project root.

    A session is authoritative when supplied.  The optional project id is only
    a consistency assertion, mirroring the terminal handshake; clients cannot
    use it to move a session-scoped search into a different project.
    """
    selected_project_id = project_id.strip()
    if session_id:
        from db.base import get_db_session
        from db.models.session import Session as SessionRow
        from sqlalchemy import select

        async with get_db_session() as db:
            owned_project_id = (
                await db.execute(
                    select(SessionRow.project_id).where(
                        SessionRow.id == session_id,
                        SessionRow.user_id == user_id,
                        SessionRow.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
        if not owned_project_id:
            raise HTTPException(status_code=404, detail="Session not found")
        if selected_project_id and selected_project_id != owned_project_id:
            raise HTTPException(
                status_code=403,
                detail="Session does not belong to the selected project",
            )
        selected_project_id = owned_project_id

    if not selected_project_id:
        return user_directory(user_id)

    from project.workspace import get_project, workdir_for_identity

    if await get_project(selected_project_id, user_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return await workdir_for_identity(user_id, selected_project_id)


def _project_relative_hits(root: str, files: list, needle: str) -> list[str]:
    """Return only in-root paths, relative to the selected project.

    Action Server paths are physical POSIX paths.  They are useful for the
    follow-up API request but must not become labels or pasted prompt text.
    """
    base = PurePosixPath(root)
    hits: list[str] = []
    for value in files:
        raw = str(value)
        candidate = PurePosixPath(raw)
        if ".." in candidate.parts:
            continue
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            relative = candidate.relative_to(base).as_posix()
        except ValueError:
            continue
        if not relative or relative == ".":
            continue
        if needle and needle not in relative.lower():
            continue
        if any(
            segment in {"node_modules", ".git", ".openbox"}
            for segment in PurePosixPath(relative).parts
        ):
            continue
        hits.append(relative)
    return hits


async def _upload_project(user_id: str, session_id: str | None) -> str:
    if session_id:
        from db.base import get_db_session
        from db.models.session import Session as SessionRow
        from sqlalchemy import select

        async with get_db_session() as db:
            project_id = (await db.execute(
                select(SessionRow.project_id).where(
                    SessionRow.id == session_id,
                    SessionRow.user_id == user_id,
                    SessionRow.is_deleted.is_(False),
                )
            )).scalar_one_or_none()
        if project_id:
            return project_id
        raise HTTPException(status_code=404, detail="Session not found")
    from project.workspace import ensure_default_project

    return (await ensure_default_project(user_id)).id


@router.post("/list")
async def list_files(
    container_id: str,
    req: ListFilesRequest,
    current_user: dict = Depends(get_current_user),
):
    try:
        payload = req.model_dump()
        payload["path"] = _tenant_path(current_user["user_id"], req.path)
        resp = await provider.forward_to_container(
            container_id, "POST", "/list_files", user_id=current_user["user_id"], json=payload
        )
        return resp.json()
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload")
async def upload_file(
    container_id: str,
    file: UploadFile,
    session_id: str | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Upload one attachment into the sandbox (frontend-v2 composer).

    Lands in the session's tenant/project namespace via chunked base64 through
    the container's /execute — the action server has no binary endpoint.
    """
    raw = await file.read()
    if len(raw) > _UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 8 MB)")

    name = (
        re.sub(r"[^\w.一-鿿-]", "_", file.filename or "file").strip("._")
        or "file"
    )[:200]
    user_id = current_user["user_id"]
    project_id = await _upload_project(user_id, session_id)
    dest = asset_sandbox_path(user_id, project_id, name)
    dest_dir = str(PurePosixPath(dest).parent)

    async def run(cmd: str):
        resp = await provider.forward_to_container(
            container_id, "POST", "/execute",
            user_id=current_user["user_id"],
            json={"command": cmd, "timeout": 30},
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
        if data.get("exit_code") not in (0, None):
            raise HTTPException(status_code=500, detail=data.get("stderr") or "upload failed")

    try:
        await run(f"mkdir -p '{dest_dir}' && : > '{dest}'")
        for i in range(0, len(raw), _UPLOAD_CHUNK):
            b64 = base64.b64encode(raw[i : i + _UPLOAD_CHUNK]).decode()
            await run(f"printf %s {b64} | base64 -d >> '{dest}'")
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"path": dest, "name": name, "size": len(raw)}


@router.get("/search")
async def search_files(
    container_id: str,
    q: str = "",
    limit: int = 30,
    path: str = "/workspace",
    session_id: str = "",
    project_id: str = "",
    current_user: dict = Depends(get_current_user),
):
    """Fuzzy file lookup for the composer's @-mention menu.

    Globs the sandbox once and filters by substring here — the action server
    has no search endpoint, and a single glob is cheaper than walking /list.
    """
    try:
        user_id = current_user["user_id"]
        root = await _file_scope_root(
            user_id,
            session_id=session_id,
            project_id=project_id,
        )
        path = root if not path or path == "/workspace" else _tenant_path(user_id, path)
        requested = PurePosixPath(path)
        scoped_root = PurePosixPath(root)
        if requested != scoped_root and scoped_root not in requested.parents:
            raise HTTPException(status_code=403, detail="Path is outside the selected project")
        resp = await provider.forward_to_container(
            container_id, "POST", "/glob",
            user_id=user_id,
            json={"pattern": "**/*", "path": path},
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
        files = data.get("files") or data.get("matches") or []
        if isinstance(files, dict):
            files = files.get("files", [])
        needle = q.strip().lower()
        hits = _project_relative_hits(root, files, needle)
        return {"files": hits[:limit], "total": len(hits)}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/content")
async def file_content(
    container_id: str,
    path: str,
    current_user: dict = Depends(get_current_user),
):
    """Raw file content for the workbench file viewer (frontend-v2).

    The container's /read_file returns cat -n numbered lines; strip the
    numbering here so the viewer gets the file as-is.
    """
    try:
        path = _tenant_path(current_user["user_id"], path)
        resp = await provider.forward_to_container(
            container_id, "POST", "/read_file",
            user_id=current_user["user_id"],
            json={"path": path, "offset": 0, "limit": 5000},
        )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
        numbered = data.get("content", "")
        raw = "\n".join(
            line.split("\t", 1)[1] if "\t" in line else line
            for line in numbered.split("\n")
        )
        total = data.get("total_lines") or 0
        end = data.get("end_line") or 0
        return {"path": path, "content": raw, "total_lines": total, "truncated": end < total}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system_info")
async def system_info(container_id: str, current_user: dict = Depends(get_current_user)):
    try:
        resp = await provider.forward_to_container(
            container_id, "GET", "/system_info", user_id=current_user["user_id"]
        )
        return resp.json()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
