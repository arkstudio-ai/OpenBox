"""Fixed-watermark data-only exports; persisted jobs survive worker restart."""
import asyncio
import hashlib
import io
import json
import zipfile
from uuid import uuid4
from sqlalchemy import select
from db.base import get_db_session
from db.models.trajectory import SessionTrajectory, TrajectoryExport, TrajectoryPayload
from trajectory.auth import assert_admin
from trajectory.payload import download_bytes, read_payload, upload_bytes
from trajectory.repository import read_events, state_at
from trajectory.projector import statistics
from trajectory.types import canonical, now, iso

_tasks = set()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def create_export(db, trajectory, viewer_id: str, through_seq: int):
    timestamp = now()
    row = TrajectoryExport(id=f"exp_{uuid4().hex}", trajectory_id=trajectory.id,
        viewer_id=viewer_id, through_seq=through_seq, status="pending", created_at=timestamp, updated_at=timestamp)
    db.add(row)
    await db.flush()
    return row


def export_status(row, session_id):
    return {"export_id": row.id, "status": row.status, "through_seq": str(row.through_seq),
        "error": row.error, "download_url": f"/api/admin/trajectories/sessions/{session_id}/exports/{row.id}/download" if row.status == "completed" else None}


async def build_export(export_id: str):
    task = asyncio.current_task()
    _tasks.add(task)
    try:
        await _build_export(export_id)
    finally:
        _tasks.discard(task)


async def _build_export(export_id: str):
    # No external execution; the only writes are the export's own job/result.
    try:
        async with get_db_session() as db:
            row = await db.get(TrajectoryExport, export_id)
            if row is None or row.status in {"completed", "deleted"}:
                return
            row.status = "running"
            row.updated_at = now()
            viewer_id, trajectory_id, through = row.viewer_id, row.trajectory_id, row.through_seq
        await assert_admin(viewer_id)
        manifest = {"format": "openbox.session-trajectory", "version": 1, "projector_version": 1,
                    "trajectory_id": trajectory_id, "through_seq": str(through),
                    "created_at": iso(now()), "files": [], "missing_payloads": []}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            event_lines = io.BytesIO()
            async with get_db_session() as db:
                trajectory = await db.get(SessionTrajectory, trajectory_id)
                if trajectory is None or trajectory.deleted_at is not None:
                    raise LookupError("Trajectory was deleted")
                manifest.update(user_id=trajectory.user_id, session_id=trajectory.session_id,
                    coverage_start=iso(trajectory.started_at))
                after = 0
                while after < through:
                    page = await read_events(db, trajectory, after_seq=after, until_seq=through, limit=1000, include_data=False)
                    for event in page["events"]:
                        event_lines.write(canonical(event) + b"\n")
                    after = int(page["through_seq"])
                event_bytes = event_lines.getvalue()
                archive.writestr("events.jsonl", event_bytes)
                manifest["files"].append({"path": "events.jsonl", "sha256": hashlib.sha256(event_bytes).hexdigest(), "size_bytes": len(event_bytes)})
                state = await state_at(db, trajectory, through)
                summary = canonical(statistics(state))
                archive.writestr("statistics.json", summary)
                manifest["files"].append({"path": "statistics.json", "sha256": hashlib.sha256(summary).hexdigest(), "size_bytes": len(summary)})
                payloads = (await db.scalars(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory_id,
                    TrajectoryPayload.first_seq <= through))).all()
                for payload in payloads:
                    try:
                        _, content = await read_payload(db, trajectory_id, payload.payload_id, through_seq=through)
                    except (FileNotFoundError, LookupError) as exc:
                        manifest["missing_payloads"].append({"payload_id": payload.payload_id, "availability": payload.availability,
                                                            "reason": type(exc).__name__})
                        continue
                    path = f"payloads/{payload.payload_id}"
                    archive.writestr(path, content)
                    manifest["files"].append({"path": path, "payload_id": payload.payload_id, "sha256": payload.sha256,
                                             "media_type": payload.media_type, "size_bytes": len(content)})
                gaps = [{"record_id": row["record_id"], "seq": row["start_seq"], "data": row["data"]}
                        for row in state["records"].values() if row["kind"] == "gap"]
                manifest["recording_status"] = trajectory.recording_status
                manifest["gaps"] = gaps
                manifest["complete"] = not manifest["missing_payloads"] and not state["unsupported_events"] and not gaps
                manifest["unsupported_events"] = state["unsupported_events"]
            archive.writestr("manifest.json", canonical(manifest))
        await assert_admin(viewer_id)
        content = buffer.getvalue()
        sha = hashlib.sha256(content).hexdigest()
        key = f"trajectories/{trajectory_id}/exports/{export_id}/{sha}.zip"
        await upload_bytes(key, content, "application/zip")
        if hashlib.sha256(await download_bytes(key)).hexdigest() != sha:
            raise ValueError("Export digest mismatch")
        discarded = False
        async with get_db_session() as db:
            trajectory = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.id == trajectory_id).with_for_update())
            row = await db.scalar(select(TrajectoryExport).where(TrajectoryExport.id == export_id).with_for_update())
            if row is not None and row.status != "deleted" and trajectory is not None and trajectory.deleted_at is None:
                row.status, row.storage_key, row.sha256, row.updated_at = "completed", key, sha, now()
            else:
                discarded = True
        if discarded:
            from trajectory.payload import get_storage
            await get_storage().delete(key)
    except Exception as exc:
        async with get_db_session() as db:
            row = await db.get(TrajectoryExport, export_id)
            if row is not None and row.status not in {"deleted", "completed"}:
                row.status, row.error, row.updated_at = "failed", type(exc).__name__, now()


async def resume_exports():
    """Application startup hook; restart only unfinished durable export jobs."""
    async with get_db_session() as db:
        ids = (await db.scalars(select(TrajectoryExport.id).where(TrajectoryExport.status.in_(["pending", "running"])))).all()
    for export_id in ids:
        _spawn(build_export(export_id))


async def stop_exports():
    """Stop in-process work; durable pending/running rows resume on startup."""
    tasks = [task for task in list(_tasks) if task is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
