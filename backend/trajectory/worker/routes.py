"""Admin trajectory HTTP API served by the trajectory worker (SPEC §8.12).

Paths, parameters, responses, error mapping, no-store headers and audit actions
are the contract of docs/trajectory-rearch/maps/api.md §2. Additive parts:
``expand=refs`` on record detail, ``GET .../blobs/{sha256}``, ``?meta=1`` on
payloads and ``capabilities.refs`` in the session header. No session or
execution mutator is reachable from here.

Downloads (payloads, blobs and exports) keep memory bounded: the content is
spooled to a temporary file in chunks with no trace database session open, the
viewer and the content's state are revalidated after that read, and only then
is the file streamed, so a read revoked meanwhile sends no bytes. The stream
runs under a transfer slot, not the read slot that prepared it (``read_limits``).
"""
from datetime import datetime
from functools import wraps
from typing import Literal

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import ORJSONResponse, StreamingResponse
from pydantic import BaseModel

from trajectory import export as exports, payload as payloads, repository
from trajectory.auth import record_audit, require_trajectory_admin, revalidate_viewer
from trajectory.store.database import TraceEngineNotInitialized, trace_read_session, trace_session
from trajectory.types import CorruptContent, TrajectoryError
from trajectory.worker.read_limits import BoundedReadRoute

router = APIRouter(prefix="/api/admin/trajectories", tags=["admin-trajectories"], route_class=BoundedReadRoute)

#: Every response carrying stored content.
CONTENT_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _json_default(value):
    if isinstance(value, (set, frozenset)):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


class TraceJSONResponse(ORJSONResponse):
    """The repository's dicts serialized by orjson, returned as a Response.

    A returned dict goes through FastAPI's ``jsonable_encoder`` (a deep copy) and stdlib ``json.dumps``:
    a 10 MiB event page took about 9 s and 100 MiB on the event loop the worker's ingest shares, past
    any read deadline. orjson serializes the same page in milliseconds.
    """

    def render(self, content) -> bytes:
        return orjson.dumps(content, default=_json_default, option=orjson.OPT_NON_STR_KEYS)


def _json(result, *, status_code: int = 200, headers: dict | None = None) -> TraceJSONResponse:
    return TraceJSONResponse(result, status_code=status_code, headers=headers)
#: The trace database is not open: an embedded worker whose start failed still has these routes mounted.
UNAVAILABLE = "Trajectory database is unavailable"


def errors(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except TraceEngineNotInitialized as exc:
            raise HTTPException(503, detail=UNAVAILABLE) from exc
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


class SpooledResponse(StreamingResponse):
    """Streams spooled content (``trajectory.payload.Spooled``) and releases it however the response ends,
    or unsent through ``close()`` when the read limits refuse the transfer."""

    def __init__(self, spooled, *, media_type: str, headers: dict):
        super().__init__(spooled.chunks(), media_type=media_type,
                         headers={**headers, "Content-Length": str(spooled.size)})
        self.spooled = spooled

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.close()

    def close(self) -> None:
        self.spooled.close()


@router.get("/sessions")
@errors
async def sessions(request: Request, admin: dict = Depends(require_trajectory_admin),
    user_id: str | None = None, user_query: str | None = None, q: str | None = None,
    workspace_id: str | None = None, status: str | None = None, recording_status: str | None = None,
    activity_from: datetime | None = None, activity_to: datetime | None = None,
    include_unrecorded: bool = False, cursor: str | None = None, limit: int = Query(50, ge=1, le=200), sort: str = "last_activity_desc"):
    async with trace_read_session() as db:
        result = await repository.list_sessions(db, user_id=user_id, user_query=user_query, q=q, workspace_id=workspace_id,
            status=status, recording_status=recording_status, activity_from=activity_from, activity_to=activity_to,
            include_unrecorded=include_unrecorded, cursor=cursor, limit=limit, sort=sort)
    await audit(admin, request, "list", details={"user_id": user_id, "workspace_id": workspace_id})
    return _json(result)


@router.get("/sessions/{session_id}")
@errors
async def header(session_id: str, request: Request, through_seq: str | None = None,
                 admin: dict = Depends(require_trajectory_admin)):
    async with trace_read_session() as db:
        result = await repository.get_session_header(db, session_id, through_seq)
    await audit(admin, request, "view", session_id, {"through_seq": result["through_seq"]})
    return _json(result)


@router.get("/sessions/{session_id}/events")
@errors
async def events(session_id: str, after_seq: str = "0", until_seq: str | None = None,
    limit: int = Query(500, ge=1, le=2000), include_data: bool = True,
    admin: dict = Depends(require_trajectory_admin)):
    async with trace_read_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        return _json(await repository.read_events(db, trajectory, after_seq=after_seq, until_seq=until_seq,
                                                  limit=limit, include_data=include_data))


@router.get("/sessions/{session_id}/records")
@errors
async def records(session_id: str, through_seq: str | None = None, before: str | None = None,
    limit: int = Query(100, ge=1, le=500), kind: str | None = None, status: str | None = None,
    agent_id: str | None = None, admin: dict = Depends(require_trajectory_admin)):
    async with trace_read_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        return _json(await repository.list_records(db, trajectory, through_seq=through_seq, before=before,
                                                   limit=limit, kind=kind, status=status, agent_id=agent_id))


@router.get("/sessions/{session_id}/records/{record_id:path}")
@errors
async def record_detail(session_id: str, record_id: str, through_seq: str | None = None,
                        expand: Literal["full", "refs"] = "full",
                        admin: dict = Depends(require_trajectory_admin)):
    async with trace_read_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        return _json(await repository.get_record(db, trajectory, record_id, through_seq=through_seq, expand=expand))


@router.get("/sessions/{session_id}/checkpoint")
@errors
async def checkpoint(session_id: str, at_seq: str | None = None, admin: dict = Depends(require_trajectory_admin)):
    async with trace_read_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        through = repository.watermark(trajectory, at_seq)
        return _json({"checkpoint": await repository.get_checkpoint(db, trajectory, through),
                      "through_seq": str(through)})


@router.get("/sessions/{session_id}/search")
@errors
async def search_records(session_id: str, q: str = Query(min_length=1, max_length=500),
    through_seq: str | None = None, cursor: str | None = None, limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(require_trajectory_admin)):
    async with trace_read_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        return _json(await repository.search(db, trajectory, q=q, through_seq=through_seq, cursor=cursor, limit=limit))


@router.get("/sessions/{session_id}/payloads/{payload_id}")
@errors
async def payload(session_id: str, payload_id: str, request: Request, through_seq: str | None = None,
                  meta: bool = False, admin: dict = Depends(require_trajectory_admin)):
    if meta:
        # Availability revalidation without bytes: no content I/O to recheck.
        async with trace_read_session() as db:
            _, trajectory = await repository.get_trajectory(db, session_id)
            through = repository.watermark(trajectory, through_seq)
            info = await payloads.payload_meta(db, trajectory, payload_id, through_seq=through)
        return _json(info, headers=CONTENT_HEADERS)
    async with trace_read_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        through = repository.watermark(trajectory, through_seq)
        row = await payloads.validate_payload(db, trajectory.id, payload_id, through_seq=through)
        media_type = row.media_type
    spooled = await payloads.spool_payload(row)
    try:
        await audit(admin, request, "payload", session_id, {"payload_id": payload_id, "through_seq": str(through)})
        await revalidate_viewer(request, admin['user_id'])
        async with trace_read_session() as db:
            _, current = await repository.get_trajectory(db, session_id)
            row = await payloads.validate_payload(db, current.id, payload_id, through_seq=through)
            # Asset references without a known hash have nothing to compare.
            if row.sha256 is not None and spooled.sha256 != row.sha256:
                raise CorruptContent('Payload changed during download')
    except BaseException:
        spooled.close()
        raise
    return SpooledResponse(spooled, media_type=media_type, headers={**CONTENT_HEADERS,
        "Content-Disposition": f'attachment; filename="{payload_id}"'})


@router.get("/sessions/{session_id}/blobs/{sha256}")
@errors
async def blob(session_id: str, sha256: str, request: Request, through_seq: str | None = None,
               admin: dict = Depends(require_trajectory_admin)):
    """The JSON value of a ``$ref`` visible at ``through_seq`` in this session's trajectory.

    A malformed digest names no blob: 404, as for an unknown one (``payload.visible_blob``).
    """
    async with trace_read_session() as db:
        _, trajectory = await repository.get_trajectory(db, session_id)
        through = repository.watermark(trajectory, through_seq)
        row = await payloads.visible_blob(db, trajectory, sha256, through_seq=through)
    # Spooling verifies the content against its address, so only the state can change meanwhile.
    spooled = await payloads.spool_blob(row)
    try:
        await revalidate_viewer(request, admin['user_id'])
        async with trace_read_session() as db:
            _, current = await repository.get_trajectory(db, session_id)
            await payloads.visible_blob(db, current, sha256, through_seq=through)
    except BaseException:
        spooled.close()
        raise
    return SpooledResponse(spooled, media_type="application/json", headers=CONTENT_HEADERS)


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
    return _json(result, status_code=202)


async def _export(db, session_id, export_id):
    _, trajectory = await repository.get_trajectory(db, session_id)
    return trajectory, await exports.get_export(db, trajectory, export_id)


@router.get("/sessions/{session_id}/exports/{export_id}")
@errors
async def export_info(session_id: str, export_id: str, admin: dict = Depends(require_trajectory_admin)):
    async with trace_read_session() as db:
        _, row = await _export(db, session_id, export_id)
        return _json(exports.export_status(row, session_id))


@router.get("/sessions/{session_id}/exports/{export_id}/download")
@errors
async def export_download(session_id: str, export_id: str, request: Request,
                          admin: dict = Depends(require_trajectory_admin)):
    # export.validate_export is the one validation: 409 not ready, 410 content deleted or expired since.
    async with trace_read_session() as db:
        trajectory, row = await _export(db, session_id, export_id)
        await exports.validate_export(db, trajectory, row)
    spooled = await payloads.spool_object(None, row.storage_key, sha256=row.sha256, mismatch="Export digest mismatch")
    try:
        await audit(admin, request, "download", session_id, {"export_id": export_id})
        await revalidate_viewer(request, admin['user_id'])
        async with trace_read_session() as db:
            trajectory, current = await _export(db, session_id, export_id)
            await exports.validate_export(db, trajectory, current)
            if current.sha256 != spooled.sha256:
                raise CorruptContent('Export changed during download')
    except BaseException:
        spooled.close()
        raise
    return SpooledResponse(spooled, media_type="application/zip", headers={**CONTENT_HEADERS,
        "Content-Disposition": f'attachment; filename="{export_id}.zip"'})
