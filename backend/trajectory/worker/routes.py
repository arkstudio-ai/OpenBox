"""Admin trajectory HTTP API served by the trajectory worker (SPEC §8.12).

Paths, parameters, responses, error mapping, no-store headers and audit actions
are the contract of docs/trajectory-rearch/maps/api.md §2. Additive parts:
``expand=refs`` on record detail, ``GET .../blobs/{sha256}``, ``?meta=1`` on
payloads and ``capabilities.refs`` in the session header. No session or
execution mutator is reachable from here.
"""
import hashlib
from datetime import datetime
from functools import wraps
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import and_, or_, select

from trajectory import export as exports, payload as payloads, repository
from trajectory.auth import NoStoreRoute, record_audit, require_trajectory_admin, revalidate_viewer
from trajectory.store.database import trace_session
from trajectory.store.models import TrajectoryExport, TrajectoryMetaAsset, TrajectoryPayload
from trajectory.types import CorruptContent, TrajectoryError

router = APIRouter(prefix="/api/admin/trajectories", tags=["admin-trajectories"], route_class=NoStoreRoute)

#: Every response carrying stored content.
CONTENT_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
SHA256_PATTERN = r"^[0-9a-f]{64}$"


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
    await record_audit(admin["user_id"], f"admin.trajectory.{action}", target_id=session_id, details=details,
                       request=request)


@router.get("/sessions")
@errors
async def sessions(request: Request, admin: dict = Depends(require_trajectory_admin),
    user_id: str | None = None, user_query: str | None = None, q: str | None = None,
    workspace_id: str | None = None, status: str | None = None, recording_status: str | None = None,
    activity_from: datetime | None = None, activity_to: datetime | None = None,
    include_unrecorded: bool = False, cursor: str | None = None, limit: int = Query(50, ge=1, le=200), sort: str = "last_activity_desc"):
    async with trace_session() as db:
        result = await repository.list_sessions(db, user_id=user_id, user_query=user_query, q=q, workspace_id=workspace_id,
            status=status, recording_status=recording_status, activity_from=activity_from, activity_to=activity_to,
            include_unrecorded=include_unrecorded, cursor=cursor, limit=limit, sort=sort)
    await audit(admin, request, "list", details={"user_id": user_id, "workspace_id": workspace_id})
    return result


@router.get("/sessions/{session_id}")
@errors
async def header(session_id: str, request: Request, through_seq: str | None = None,
                 admin: dict = Depends(require_trajectory_admin)):
    async with trace_session() as db:
        result = await repository.get_session_header(db, session_id, through_seq)
    # The worker resolves $ref values on /blobs and records honour expand=refs.
    result["capabilities"] = {**(result.get("capabilities") or {}), "refs": True}
    await audit(admin, request, "view", session_id, {"through_seq": result["through_seq"]})
    return result


@router.get("/sessions/{session_id}/events")
@errors
async def events(session_id: str, after_seq: str = "0", until_seq: str | None = None,
    limit: int = Query(500, ge=1, le=2000), include_data: bool = True,
    admin: dict = Depends(require_trajectory_admin)):
    async with trace_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        return await repository.read_events(db, trajectory, after_seq=after_seq, until_seq=until_seq, limit=limit,
                                            include_data=include_data)


@router.get("/sessions/{session_id}/records")
@errors
async def records(session_id: str, through_seq: str | None = None, before: str | None = None,
    limit: int = Query(100, ge=1, le=500), kind: str | None = None, status: str | None = None,
    agent_id: str | None = None, admin: dict = Depends(require_trajectory_admin)):
    async with trace_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        return await repository.list_records(db, trajectory, through_seq=through_seq, before=before, limit=limit,
            kind=kind, status=status, agent_id=agent_id)


@router.get("/sessions/{session_id}/records/{record_id:path}")
@errors
async def record_detail(session_id: str, record_id: str, through_seq: str | None = None,
                        expand: Literal["full", "refs"] = "full",
                        admin: dict = Depends(require_trajectory_admin)):
    async with trace_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        return await repository.get_record(db, trajectory, record_id, through_seq=through_seq, expand=expand)


@router.get("/sessions/{session_id}/checkpoint")
@errors
async def checkpoint(session_id: str, at_seq: str | None = None, admin: dict = Depends(require_trajectory_admin)):
    async with trace_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        through = repository.watermark(trajectory, at_seq)
        return {"checkpoint": await repository.get_checkpoint(db, trajectory, through), "through_seq": str(through)}


@router.get("/sessions/{session_id}/search")
@errors
async def search_records(session_id: str, q: str = Query(min_length=1, max_length=500),
    through_seq: str | None = None, cursor: str | None = None, limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(require_trajectory_admin)):
    async with trace_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        return await repository.search(db, trajectory, q=q, through_seq=through_seq, cursor=cursor, limit=limit)


@router.get("/sessions/{session_id}/payloads/{payload_id}")
@errors
async def payload(session_id: str, payload_id: str, request: Request, through_seq: str | None = None,
                  meta: bool = False, admin: dict = Depends(require_trajectory_admin)):
    if meta:
        # Availability revalidation without bytes: no content I/O to recheck.
        async with trace_session() as db:
            _, trajectory = await repository.get_trajectory(db, session_id)
            through = repository.watermark(trajectory, through_seq)
            info = await payloads.payload_meta(db, trajectory, payload_id, through_seq=through)
        return JSONResponse(info, headers=CONTENT_HEADERS)
    async with trace_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        through = repository.watermark(trajectory, through_seq)
        row, content = await payloads.read_payload(db, trajectory.id, payload_id, through_seq=through)
        media_type = row.media_type
    await audit(admin, request, "payload", session_id, {"payload_id": payload_id, "through_seq": str(through)})
    await revalidate_viewer(request, admin['user_id'])
    async with trace_session() as db:
        _, current = await repository.get_trajectory(db, session_id)
        row = await payloads.validate_payload(db, current.id, payload_id, through_seq=through)
        # Asset references without a known hash have nothing to compare.
        if row.sha256 is not None and hashlib.sha256(content).hexdigest() != row.sha256:
            raise CorruptContent('Payload changed during download')
    return Response(content, media_type=media_type, headers={**CONTENT_HEADERS,
        "Content-Disposition": f'attachment; filename="{payload_id}"'})


@router.get("/sessions/{session_id}/blobs/{sha256}")
@errors
async def blob(session_id: str, request: Request, sha256: str = Path(pattern=SHA256_PATTERN),
               through_seq: str | None = None, admin: dict = Depends(require_trajectory_admin)):
    """The JSON value of a ``$ref`` visible at ``through_seq`` in this session's trajectory."""
    async with trace_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        through = repository.watermark(trajectory, through_seq)
        content = await payloads.read_blob(db, trajectory, sha256, through_seq=through)
    await revalidate_viewer(request, admin['user_id'])
    async with trace_session() as db:
        _, current = await repository.get_trajectory(db, session_id)
        if await payloads.read_blob(db, current, sha256, through_seq=through) != content:
            raise CorruptContent('Blob changed during download')
    return Response(content, media_type="application/json", headers=CONTENT_HEADERS)


class ExportBody(BaseModel):
    through_seq: str | None = None


@router.post("/sessions/{session_id}/export", status_code=202)
@errors
async def export(session_id: str, body: ExportBody, request: Request, admin: dict = Depends(require_trajectory_admin)):
    # The worker's export service builds pending exports under a lease.
    async with trace_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        row = await exports.create_export(db, trajectory, admin["user_id"],
                                          repository.watermark(trajectory, body.through_seq))
        result = exports.export_status(row, session_id)
    await audit(admin, request, "export", session_id, {"through_seq": result["through_seq"], "export_id": result["export_id"]})
    return result


async def _export(db, session_id, export_id):
    _, trajectory = await repository.get_trajectory(db, session_id)
    row = await db.scalar(select(TrajectoryExport).where(TrajectoryExport.id == export_id,
        TrajectoryExport.trajectory_id == trajectory.id))
    if row is None:
        raise LookupError("Export does not belong to this trajectory")
    return trajectory, row


async def _validate_export_content(db, trajectory, row):
    if row.status != 'completed':
        raise HTTPException(409, detail='Export is not ready')
    if getattr(trajectory, "content_expired_at", None) is not None:
        raise FileNotFoundError("Trajectory content has expired")
    visible = (TrajectoryPayload.trajectory_id == trajectory.id, TrajectoryPayload.first_seq <= row.through_seq)
    deleted = await db.scalar(select(TrajectoryPayload.payload_id).where(*visible,
        TrajectoryPayload.deleted_at > row.created_at).limit(1))
    # Asset references need their live asset; other copies bound to an asset
    # die with a deletion the replica already knows about.
    removed_source = await db.scalar(select(TrajectoryPayload.payload_id).outerjoin(TrajectoryMetaAsset,
        TrajectoryMetaAsset.id == TrajectoryPayload.source_asset_id).where(*visible,
        TrajectoryPayload.source_asset_id.is_not(None), TrajectoryPayload.availability == 'available',
        or_(and_(TrajectoryPayload.storage_kind == 'asset', TrajectoryMetaAsset.id.is_(None)),
            TrajectoryMetaAsset.is_deleted.is_(True), TrajectoryMetaAsset.deleted_at.is_not(None),
            TrajectoryMetaAsset.status == 'deleted')).limit(1))
    if deleted or removed_source:
        raise FileNotFoundError('Export invalidated by explicit content deletion; create a new export')


@router.get("/sessions/{session_id}/exports/{export_id}")
@errors
async def export_info(session_id: str, export_id: str, admin: dict = Depends(require_trajectory_admin)):
    async with trace_session() as db:
        _, row = await _export(db, session_id, export_id)
        return exports.export_status(row, session_id)


@router.get("/sessions/{session_id}/exports/{export_id}/download")
@errors
async def export_download(session_id: str, export_id: str, request: Request,
                          admin: dict = Depends(require_trajectory_admin)):
    async with trace_session() as db:
        trajectory, row = await _export(db, session_id, export_id)
        await _validate_export_content(db, trajectory, row)
        _, content = await exports.read_export(db, trajectory, export_id)
    await audit(admin, request, "download", session_id, {"export_id": export_id})
    await revalidate_viewer(request, admin['user_id'])
    async with trace_session() as db:
        trajectory, row = await _export(db, session_id, export_id)
        await _validate_export_content(db, trajectory, row)
        if hashlib.sha256(content).hexdigest() != row.sha256:
            raise CorruptContent('Export changed during download')
    return Response(content, media_type="application/zip", headers={**CONTENT_HEADERS,
        "Content-Disposition": f'attachment; filename="{export_id}.zip"'})
