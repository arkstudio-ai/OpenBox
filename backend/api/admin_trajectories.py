"""Platform-admin-only observation. No session/execution mutators imported."""
from datetime import datetime
from functools import wraps
import hashlib
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import or_, select
from audit import record as audit_record
from db.base import get_db_session
from db.models.trajectory import TrajectoryExport, TrajectoryPayload
from trajectory.auth import NoStoreRoute, require_trajectory_admin, revalidate_viewer
from trajectory.export import build_export, create_export, export_status
from trajectory.payload import download_bytes, read_payload, validate_payload
from trajectory.repository import (get_checkpoint, get_record, get_session_header, get_trajectory,
    list_records, list_sessions, read_events, search, watermark)
from trajectory.types import CorruptContent, TrajectoryError

router = APIRouter(prefix="/api/admin/trajectories", tags=["admin-trajectories"], route_class=NoStoreRoute)


def errors(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except FileNotFoundError as exc:
            raise HTTPException(410, detail={"code": "trajectory_content_deleted", "message": str(exc)}) from exc
        except LookupError as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except CorruptContent as exc:
            raise HTTPException(409, detail={"code": exc.code, "message": str(exc)}) from exc
        except TrajectoryError as exc:
            raise HTTPException(400, detail={"code": exc.code, "message": str(exc)}) from exc
    return wrapped


async def audit(admin, request, action, session_id=None, details=None):
    await audit_record(admin["user_id"], None, f"admin.trajectory.{action}", "trajectory", session_id, details, request)


@router.get("/sessions")
@errors
async def sessions(request: Request, admin: dict = Depends(require_trajectory_admin),
    user_id: str | None = None, user_query: str | None = None, q: str | None = None,
    workspace_id: str | None = None, status: str | None = None, recording_status: str | None = None,
    activity_from: datetime | None = None, activity_to: datetime | None = None,
    include_unrecorded: bool = False, cursor: str | None = None, limit: int = Query(50, ge=1, le=200), sort: str = "last_activity_desc"):
    async with get_db_session() as db:
        result = await list_sessions(db, user_id=user_id, user_query=user_query, q=q, workspace_id=workspace_id,
            status=status, recording_status=recording_status, activity_from=activity_from, activity_to=activity_to,
            include_unrecorded=include_unrecorded, cursor=cursor, limit=limit, sort=sort)
    await audit(admin, request, "list", details={"user_id": user_id, "workspace_id": workspace_id})
    return result


@router.get("/sessions/{session_id}")
@errors
async def header(session_id: str, request: Request, through_seq: str | None = None,
                 admin: dict = Depends(require_trajectory_admin)):
    async with get_db_session() as db:
        result = await get_session_header(db, session_id, through_seq)
    await audit(admin, request, "view", session_id, {"through_seq": result["through_seq"]})
    return result


@router.get("/sessions/{session_id}/events")
@errors
async def events(session_id: str, after_seq: str = "0", until_seq: str | None = None,
    limit: int = Query(500, ge=1, le=2000), include_data: bool = True,
    admin: dict = Depends(require_trajectory_admin)):
    async with get_db_session() as db:
        _, trajectory = await get_trajectory(db, session_id)
        return await read_events(db, trajectory, after_seq=after_seq, until_seq=until_seq, limit=limit, include_data=include_data)


@router.get("/sessions/{session_id}/records")
@errors
async def records(session_id: str, through_seq: str | None = None, before: str | None = None,
    limit: int = Query(100, ge=1, le=500), kind: str | None = None, status: str | None = None,
    agent_id: str | None = None, admin: dict = Depends(require_trajectory_admin)):
    async with get_db_session() as db:
        _, trajectory = await get_trajectory(db, session_id)
        return await list_records(db, trajectory, through_seq=through_seq, before=before, limit=limit,
            kind=kind, status=status, agent_id=agent_id)


@router.get("/sessions/{session_id}/records/{record_id:path}")
@errors
async def record_detail(session_id: str, record_id: str, through_seq: str | None = None,
                        admin: dict = Depends(require_trajectory_admin)):
    async with get_db_session() as db:
        _, trajectory = await get_trajectory(db, session_id)
        return await get_record(db, trajectory, record_id, through_seq=through_seq)


@router.get("/sessions/{session_id}/checkpoint")
@errors
async def checkpoint(session_id: str, at_seq: str | None = None, admin: dict = Depends(require_trajectory_admin)):
    async with get_db_session() as db:
        _, trajectory = await get_trajectory(db, session_id)
        through = watermark(trajectory, at_seq)
        return {"checkpoint": await get_checkpoint(db, trajectory, through), "through_seq": str(through)}


@router.get("/sessions/{session_id}/search")
@errors
async def search_records(session_id: str, q: str = Query(min_length=1, max_length=500),
    through_seq: str | None = None, cursor: str | None = None, limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(require_trajectory_admin)):
    async with get_db_session() as db:
        _, trajectory = await get_trajectory(db, session_id)
        return await search(db, trajectory, q=q, through_seq=through_seq, cursor=cursor, limit=limit)


@router.get("/sessions/{session_id}/payloads/{payload_id}")
@errors
async def payload(session_id: str, payload_id: str, request: Request, through_seq: str | None = None,
                  admin: dict = Depends(require_trajectory_admin)):
    async with get_db_session() as db:
        _, trajectory = await get_trajectory(db, session_id)
        through = watermark(trajectory, through_seq)
        row, content = await read_payload(db, trajectory.id, payload_id, through_seq=through)
        media_type = row.media_type
    await audit(admin, request, "payload", session_id, {"payload_id": payload_id, "through_seq": str(through)})
    await revalidate_viewer(request, admin['user_id'])
    async with get_db_session() as db:
        _, current = await get_trajectory(db, session_id)
        row = await validate_payload(db, current.id, payload_id, through_seq=through)
        if hashlib.sha256(content).hexdigest() != row.sha256:
            raise CorruptContent('Payload changed during download')
    return Response(content, media_type=media_type, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{payload_id}"'})


class ExportBody(BaseModel):
    through_seq: str | None = None


@router.post("/sessions/{session_id}/export", status_code=202)
@errors
async def export(session_id: str, body: ExportBody, request: Request, background: BackgroundTasks,
                 admin: dict = Depends(require_trajectory_admin)):
    async with get_db_session() as db:
        _, trajectory = await get_trajectory(db, session_id)
        row = await create_export(db, trajectory, admin["user_id"], watermark(trajectory, body.through_seq))
        result = export_status(row, session_id)
    await audit(admin, request, "export", session_id, {"through_seq": result["through_seq"], "export_id": row.id})
    background.add_task(build_export, row.id)
    return result


async def _export(db, session_id, export_id):
    _, trajectory = await get_trajectory(db, session_id)
    row = await db.scalar(select(TrajectoryExport).where(TrajectoryExport.id == export_id,
        TrajectoryExport.trajectory_id == trajectory.id))
    if row is None:
        raise LookupError("Export does not belong to this trajectory")
    return trajectory, row


async def _validate_export_content(db, trajectory, row):
    if row.status != 'completed':
        raise HTTPException(409, detail='Export is not ready')
    deleted = await db.scalar(select(TrajectoryPayload.payload_id).where(TrajectoryPayload.trajectory_id == trajectory.id,
        TrajectoryPayload.first_seq <= row.through_seq, TrajectoryPayload.deleted_at > row.created_at).limit(1))
    from db.models.file_asset import FileAsset
    removed_source = await db.scalar(select(TrajectoryPayload.payload_id).outerjoin(FileAsset, FileAsset.id == TrajectoryPayload.source_asset_id).where(
        TrajectoryPayload.trajectory_id == trajectory.id, TrajectoryPayload.first_seq <= row.through_seq,
        TrajectoryPayload.source_asset_id.is_not(None), TrajectoryPayload.availability == 'available',
        or_(FileAsset.id.is_(None), FileAsset.is_deleted.is_(True), FileAsset.deleted_at.is_not(None), FileAsset.status == 'deleted')).limit(1))
    if deleted or removed_source:
        raise FileNotFoundError('Export invalidated by explicit content deletion; create a new export')


@router.get("/sessions/{session_id}/exports/{export_id}")
@errors
async def export_info(session_id: str, export_id: str, admin: dict = Depends(require_trajectory_admin)):
    async with get_db_session() as db:
        _, row = await _export(db, session_id, export_id)
        return export_status(row, session_id)


@router.get("/sessions/{session_id}/exports/{export_id}/download")
@errors
async def export_download(session_id: str, export_id: str, request: Request,
                          admin: dict = Depends(require_trajectory_admin)):
    async with get_db_session() as db:
        trajectory, row = await _export(db, session_id, export_id)
        await _validate_export_content(db, trajectory, row)
        content = await download_bytes(row.storage_key)
        if hashlib.sha256(content).hexdigest() != row.sha256:
            raise CorruptContent("Export digest mismatch")
    await audit(admin, request, "download", session_id, {"export_id": export_id})
    await revalidate_viewer(request, admin['user_id'])
    async with get_db_session() as db:
        trajectory, row = await _export(db, session_id, export_id)
        await _validate_export_content(db, trajectory, row)
        if hashlib.sha256(content).hexdigest() != row.sha256:
            raise CorruptContent('Export changed during download')
    return Response(content, media_type="application/zip", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{export_id}.zip"'})
