"""Assets: browser → OSS direct transfer, plus the resource-centre index.

The backend never carries the bytes. It issues a presigned PUT (the browser
uploads straight to OSS), verifies the object landed, and records the upload
in `file_assets`. The cloud desktop later pulls the object with `obx-file`
(sandbox/assets.py) — solving the tunnel-bandwidth problem the old chunked
base64 upload had. 503 here tells the frontend to fall back to that legacy
sandbox upload (e.g. local docker dev without OSS configured).

The same table backs the resource centre: every row knows its project and
whether a person or the agent produced it, which is exactly the two-level
"project → source" filter the UI offers.
"""
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from api.asset_kinds import KINDS, kind_of
from auth.jwt import decode_asset_download_token
from auth.middleware import get_current_user, get_optional_current_user
from core.config import get_config
from core.identifier import ascending
from core.log import create_logger
from core.oss import OssNotConfigured, get_oss
from db.base import get_db_session
from db.models.file_asset import FileAsset
from project.workspace import asset_sandbox_path

log = create_logger("api.assets")

router = APIRouter(prefix="/api/assets", tags=["assets"])

_MAX_SIZE = 1024 * 1024 * 1024  # 1 GB; OSS handles it, the tunnel never sees it
_UPLOAD_URL_TTL_SECONDS = 6 * 60 * 60
#: Rows one listing request will classify in Python. A personal workspace does
#: not come near it; beyond it the tail is dropped rather than the request.
_SCAN_CAP = 3000


class CreateAssetBody(BaseModel):
    name: str
    mime: str = "application/octet-stream"
    size: int = 0
    session_id: str | None = None
    #: Explicit filing; when absent the session's project is used.
    project_id: str | None = None


class RenameAssetBody(BaseModel):
    name: str


def _clean_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.一-鿿-]", "_", name or "file").strip("._") or "file"
    return cleaned[:200]


def _oss_or_503():
    try:
        return get_oss()
    except OssNotConfigured as e:
        raise HTTPException(503, detail=str(e))


async def _session_project(db, session_id: str | None, user_id: str) -> str | None:
    if not session_id:
        return None
    from db.models.session import Session as SessionRow

    return (
        await db.execute(
            select(SessionRow.project_id).where(
                SessionRow.id == session_id, SessionRow.user_id == user_id
            )
        )
    ).scalar_one_or_none()


@router.post("")
async def create_asset(body: CreateAssetBody, current_user: dict = Depends(get_current_user)):
    """Open an upload: record it and hand the browser a presigned PUT URL."""
    oss = _oss_or_503()
    if body.size > _MAX_SIZE:
        raise HTTPException(413, detail="File too large (max 1 GB)")
    if body.size < 0:
        raise HTTPException(422, detail="File size cannot be negative")
    user_id = current_user["user_id"]
    name = _clean_name(body.name)
    asset_id = ascending("asset")
    key = f"assets/{user_id}/{asset_id}/{name}"
    mime = (body.mime or "application/octet-stream")[:128]

    async with get_db_session() as db:
        project_id = body.project_id or await _session_project(db, body.session_id, user_id)
        db.add(
            FileAsset(
                id=asset_id,
                user_id=user_id,
                session_id=body.session_id,
                project_id=project_id,
                name=name,
                oss_key=key,
                mime=mime,
                size=body.size,
                status="pending",
                source="user",
                created_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    return {
        "id": asset_id,
        "name": name,
        "sandboxPath": asset_sandbox_path(
            user_id,
            project_id,
            name,
            asset_id=asset_id,
        ),
        # A 1 GB upload can exceed the old 30-minute URL on a slow connection.
        "putUrl": oss.presign_put(key, mime, expires_sec=_UPLOAD_URL_TTL_SECONDS),
        # The PUT must send exactly what was signed (bossip's hard-won rule).
        "headers": {"Content-Type": mime},
    }


async def _owned_asset(db, asset_id: str, user_id: str) -> FileAsset:
    row = (
        await db.execute(
            select(FileAsset).where(
                FileAsset.id == asset_id,
                FileAsset.user_id == user_id,
                FileAsset.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, detail="Asset not found")
    return row


def _utc(when: datetime) -> datetime:
    """Rows written before the column became timestamptz come back naive."""
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _to_item(row: FileAsset, oss) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "mime": row.mime,
        "size": row.size,
        "kind": kind_of(row.mime, row.name),
        "source": row.source,
        "projectId": row.project_id,
        "sessionId": row.session_id,
        "status": row.status,
        "createdAt": _utc(row.created_at).isoformat(),
        "sandboxPath": asset_sandbox_path(
            row.user_id,
            row.project_id,
            row.name,
            asset_id=row.id,
        ),
        "url": oss.presign_get(row.oss_key),
    }


@router.get("")
async def list_assets(
    project: str = Query("all", description="project id, 'all', or 'none' for unfiled"),
    source: str = Query("all", description="'all' | 'user' | 'agent'"),
    kind: str = Query("all", description=f"'all' | {' | '.join(KINDS)}"),
    q: str = "",
    sort: str = Query("created", description="'created' | 'name' | 'size'"),
    limit: int = Query(60, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """The resource centre's listing: project → source → kind, newest first."""
    oss = _oss_or_503()
    user_id = current_user["user_id"]

    stmt = select(FileAsset).where(
        FileAsset.user_id == user_id,
        FileAsset.is_deleted.is_(False),
        FileAsset.status == "ready",
        # Desktop screenshots are working bytes, not resources.
        FileAsset.transient.is_(False),
    )
    if project == "none":
        stmt = stmt.where(FileAsset.project_id.is_(None))
    elif project != "all":
        stmt = stmt.where(FileAsset.project_id == project)
    if source in ("user", "agent"):
        stmt = stmt.where(FileAsset.source == source)
    needle = q.strip()
    if needle:
        stmt = stmt.where(FileAsset.name.ilike(f"%{needle}%"))

    order = {
        "name": FileAsset.name.asc(),
        "size": FileAsset.size.desc(),
        "created": FileAsset.created_at.desc(),
    }.get(sort, FileAsset.created_at.desc())

    async with get_db_session() as db:
        rows = list((await db.execute(stmt.order_by(order).limit(_SCAN_CAP))).scalars())

    # Kind is derived from mime+name, so it is filtered here rather than in SQL.
    if kind in KINDS:
        rows = [r for r in rows if kind_of(r.mime, r.name) == kind]

    page = rows[offset : offset + limit]
    return {
        "items": [_to_item(r, oss) for r in page],
        "total": len(rows),
        "hasMore": offset + limit < len(rows),
    }


@router.get("/usage")
async def asset_usage(current_user: dict = Depends(get_current_user)):
    """Bytes this account holds in the asset bucket, against its ceiling."""
    user_id = current_user["user_id"]
    async with get_db_session() as db:
        rows = list(
            (
                await db.execute(
                    select(FileAsset.size).where(
                        FileAsset.user_id == user_id,
                        FileAsset.is_deleted.is_(False),
                        FileAsset.status == "ready",
                        FileAsset.transient.is_(False),
                    )
                )
            ).scalars()
        )
    return {
        "used": sum(rows),
        "count": len(rows),
        "quota": get_config().oss_user_quota_bytes,
    }


@router.post("/{asset_id}/complete")
async def complete_asset(asset_id: str, current_user: dict = Depends(get_current_user)):
    """Verify the object actually landed in OSS, then mark the record ready."""
    oss = _oss_or_503()
    async with get_db_session() as db:
        row = await _owned_asset(db, asset_id, current_user["user_id"])
        head = await oss.head(row.oss_key)
        if not head:
            raise HTTPException(409, detail="Object not found in OSS — upload did not complete")
        actual_size = int(head["size"] or 0)
        # Never trust the size declared when the ticket was opened: a caller
        # can PUT a larger object to the same signed key. Enforce the ceiling
        # against the object OSS actually received.
        if actual_size > _MAX_SIZE:
            row.is_deleted = True
            row.deleted_at = datetime.now(timezone.utc)
            await db.commit()
            try:
                await oss.delete(row.oss_key)
            except Exception as e:
                log.warning(f"Oversized OSS object cleanup failed for {row.oss_key}: {e}")
            raise HTTPException(413, detail="File too large (max 1 GB)")
        row.size = actual_size or row.size
        row.status = "ready"
        await db.commit()
        return _to_item(row, oss)


@router.get("/{asset_id}/url")
async def asset_url(asset_id: str, download: bool = False, current_user: dict = Depends(get_current_user)):
    """Fresh presigned GET for previews (they expire; the UI refetches)."""
    oss = _oss_or_503()
    async with get_db_session() as db:
        row = await _owned_asset(db, asset_id, current_user["user_id"])
        if row.status != "ready":
            raise HTTPException(409, detail="Upload not completed")
        return {
            "url": oss.presign_get(row.oss_key, download_name=row.name if download else None),
            "mime": row.mime,
            "name": row.name,
            "size": row.size,
        }


@router.get("/{asset_id}/download")
async def asset_download(
    asset_id: str,
    token: str = "",
    current_user: dict | None = Depends(get_optional_current_user),
):
    """Resolve an authenticated or capability URL to a fresh OSS signature."""
    user_id = str((current_user or {}).get("user_id") or "")
    if not user_id and token:
        payload = decode_asset_download_token(token, asset_id)
        user_id = str((payload or {}).get("sub") or "")
    if not user_id:
        raise HTTPException(401, detail="Not authenticated")
    oss = _oss_or_503()
    async with get_db_session() as db:
        row = await _owned_asset(db, asset_id, user_id)
        if row.status != "ready":
            raise HTTPException(409, detail="Upload not completed")
        url = oss.presign_get(row.oss_key, download_name=row.name)
    return RedirectResponse(url=url, status_code=307)


#: Text preview ceiling. Above this the viewer offers a download instead —
#: nobody reads a 5 MB log in a side pane, and the bytes would cross the API.
_TEXT_PREVIEW_MAX = 256 * 1024


@router.get("/{asset_id}/text")
async def asset_text(asset_id: str, current_user: dict = Depends(get_current_user)):
    """Text body for the preview pane.

    The one case where bytes do pass through the backend: a browser `fetch`
    of the presigned URL is a cross-origin read the bucket does not allow,
    and an <iframe> would download rather than render most text types.
    """
    import httpx

    oss = _oss_or_503()
    async with get_db_session() as db:
        row = await _owned_asset(db, asset_id, current_user["user_id"])
        key, size, mime, name = row.oss_key, row.size, row.mime, row.name
    if size > _TEXT_PREVIEW_MAX:
        raise HTTPException(413, detail="File too large to preview")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(oss.presign_get(key, expires_sec=300))
    if resp.status_code != 200:
        raise HTTPException(502, detail=f"OSS read failed ({resp.status_code})")
    return {
        "name": name,
        "mime": mime,
        "text": resp.content.decode("utf-8", errors="replace"),
        "truncated": False,
    }


@router.patch("/{asset_id}")
async def rename_asset(
    asset_id: str, body: RenameAssetBody, current_user: dict = Depends(get_current_user)
):
    """Rename the resource. The OSS key is immutable — only the label moves."""
    oss = _oss_or_503()
    name = _clean_name(body.name)
    async with get_db_session() as db:
        row = await _owned_asset(db, asset_id, current_user["user_id"])
        row.name = name
        await db.commit()
        return _to_item(row, oss)


@router.delete("/{asset_id}")
async def delete_asset(asset_id: str, current_user: dict = Depends(get_current_user)):
    """Drop the object from OSS and tombstone the row.

    The row survives because message file-parts point at it; a hard delete
    would leave old replies referring to an id that no longer resolves.
    """
    oss = _oss_or_503()
    async with get_db_session() as db:
        row = await _owned_asset(db, asset_id, current_user["user_id"])
        key = row.oss_key
        row.is_deleted = True
        row.deleted_at = datetime.now(timezone.utc)
        await db.commit()
    try:
        await oss.delete(key)
    except Exception as e:
        # The tombstone already hides it; a stranded object is not worth a 500.
        log.warning(f"OSS delete failed for {key}: {e}")
    return {"ok": True}
