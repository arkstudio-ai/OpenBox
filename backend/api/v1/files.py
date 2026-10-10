"""``/v1/files``: upload source material, refresh a download link."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from api.asset_kinds import extension
from api.assets import _clean_name
from api.v1.deps import require_scope
from api.v1.errors import ApiError
from api.v1.ids import internal_id, public_id
from api.v1.public import iso_z
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.file_asset import FileAsset

log = create_logger("api.v1.files")

router = APIRouter(prefix="/files", tags=["files"])

MAX_BYTES = 200 * 1024 * 1024
DOWNLOAD_URL_TTL_SECONDS = 24 * 3600
_CHUNK = 1024 * 1024

#: The contract's material formats, keyed by extension. A recognised
#: extension decides the stored MIME type; browsers and SDKs disagree about
#: ``.mov`` and ``.m4a`` far too often to trust the declared one.
ALLOWED_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "mp4": "video/mp4", "mov": "video/quicktime",
    "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
}
_ALLOWED_MIMES = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "video/mp4": "mp4", "video/quicktime": "mov",
    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
}


def _oss_or_503():
    from core.oss import OssNotConfigured, get_oss

    try:
        return get_oss()
    except OssNotConfigured as exc:
        raise ApiError(503, "STORAGE_UNAVAILABLE", str(exc)) from exc


def resolve_type(filename: str, declared: str | None) -> tuple[str, str]:
    """``(clean name, mime)`` for an accepted upload, else 415."""
    name = _clean_name(filename)
    ext = extension(name)
    mime = ALLOWED_TYPES.get(ext)
    if mime is None:
        declared_mime = (declared or "").split(";")[0].strip().lower()
        by_mime = _ALLOWED_MIMES.get(declared_mime)
        if by_mime is None:
            raise ApiError(
                415, "UNSUPPORTED_MEDIA_TYPE",
                "Accepted formats: jpg, png, webp, mp4, mov, mp3, wav, m4a",
            )
        name = f"{name}.{by_mime}" if not ext else name
        mime = ALLOWED_TYPES[by_mime]
    return name, mime


def file_view(row: FileAsset) -> dict:
    return {
        "id": public_id(row.id),
        "filename": row.name,
        "mime_type": row.mime,
        "size": row.size,
        # Media probing is deferred (plan §10.2).
        "duration_s": None,
        "created_at": iso_z(row.created_at),
    }


@router.post("", status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    identity: dict = Depends(require_scope("files:write")),
):
    """Store one material file (≤ 200 MB) and return its id for ``attachments``."""
    oss = _oss_or_503()
    name, mime = resolve_type(file.filename or "file", file.content_type)
    user_id = identity["user_id"]
    asset_id = ascending("asset")
    key = f"assets/{user_id}/{asset_id}/{name}"

    fd, path = tempfile.mkstemp(prefix="obx-v1-upload-")
    size = 0
    try:
        with os.fdopen(fd, "wb") as spool:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ApiError(413, "PAYLOAD_TOO_LARGE", "File too large (max 200 MB)")
                spool.write(chunk)
        if size == 0:
            raise ApiError(400, "INVALID_REQUEST", "File is empty")
        await oss.put_object_file(key, path, content_type=mime, timeout=600)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    now = datetime.now(timezone.utc)
    row = FileAsset(
        id=asset_id,
        user_id=user_id,
        workspace_id=identity["workspace_id"],
        session_id=None,
        project_id=None,
        name=name,
        oss_key=key,
        mime=mime,
        size=size,
        status="ready",
        source="user",
        created_at=now,
    )
    async with get_db_session() as db:
        db.add(row)
    return file_view(row)


async def _workspace_file(file_id: str, identity: dict) -> FileAsset:
    async with get_db_session() as db:
        row = await db.scalar(select(FileAsset).where(
            FileAsset.id == internal_id(file_id, "asset"),
            FileAsset.workspace_id == identity["workspace_id"],
            FileAsset.is_deleted.is_(False),
        ))
    if row is None or row.status != "ready":
        raise ApiError(404, "NOT_FOUND", "File not found")
    return row


@router.get("/{file_id}/content")
async def file_content(
    file_id: str,
    identity: dict = Depends(require_scope("files:read")),
):
    """``302`` to a signed download URL valid for 24 hours."""
    oss = _oss_or_503()
    row = await _workspace_file(file_id, identity)
    url = oss.presign_get(row.oss_key, expires_sec=DOWNLOAD_URL_TTL_SECONDS, download_name=row.name)
    return RedirectResponse(url=url, status_code=302)
