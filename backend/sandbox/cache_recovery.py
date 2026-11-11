"""Refresh pending upload grants and reconcile provably terminal cache owners.

Desktop disk checks run locally. This bounded five-minute backend sweep only
supplies ownership evidence and fresh OSS grants; it never starts a desktop,
downloads an asset, guesses an expired lease's outcome, or sends arbitrary rm.
"""
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from core.log import create_logger
from sandbox.managed_cache import enabled

log = create_logger("sandbox.cache_recovery")


async def sweep():
    if not enabled():
        return
    from db.base import get_db_session
    from db.models.cloud_desktop import CloudDesktop
    from db.repository.cloud_desktop_repo import cloud_desktop_repo
    async with get_db_session() as db:
        ids = list(await db.scalars(select(CloudDesktop.id).where(CloudDesktop.is_deleted.is_(False),
            CloudDesktop.pool_state == "assigned", CloudDesktop.tunnel_state == "up").order_by(CloudDesktop.id).limit(128)))
    for record_id in ids:
        record = await cloud_desktop_repo.get(record_id)
        try:
            await recover_desktop(record)
        except Exception as exc:
            # No raw HTTP exception: it may include signed credentials.
            log.warning("Cache recovery deferred for %s: %s", record_id, type(exc).__name__)


async def terminal_locked(db, task, workspace_id):
    from db.models.agent_driver import AgentDriverState
    from db.models.session import Session
    from db.models.question import SessionExecution, QuestionCheckpoint
    from db.models.video_job import VideoJob
    from db.models.agent_inbox import AgentInboxItem
    from db.models.external_effect import ExternalEffect
    # Session -> Driver is the same lock order as reserve/release. Keep these
    # locks until the remote exact-generation settlement finishes.
    session = await db.scalar(select(Session).where(Session.id == task["id"],
        Session.workspace_id == workspace_id).with_for_update())
    if session is None or session.status not in {"idle", "error"}:
        return False
    driver = await db.scalar(select(AgentDriverState).where(AgentDriverState.session_id == session.id).with_for_update())
    if driver is None or driver.generation < task["generation"] or driver.phase != "idle" or driver.run_id is not None:
        return False
    execution = await db.get(SessionExecution, session.id)
    if execution and (execution.resume_pending or execution.run_id is not None):
        return False
    if await db.scalar(select(QuestionCheckpoint.id).where(QuestionCheckpoint.session_id == session.id,
                                                        QuestionCheckpoint.status == "pending").limit(1)):
        return False
    if await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == session.id,
                       AgentInboxItem.state.in_(("accepted", "claimed"))).limit(1)):
        return False
    if await db.scalar(select(VideoJob.id).where(VideoJob.session_id == session.id,
                      VideoJob.status.not_in(("completed", "failed", "canceled", "cancelled"))).limit(1)):
        return False
    if await db.scalar(select(ExternalEffect.id).where(ExternalEffect.session_id == session.id,
                      ExternalEffect.state.not_in(("succeeded", "failed", "canceled", "cancelled", "superseded", "manual_review"))).limit(1)):
        return False
    return True


async def recover_desktop(record):
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.cloud_desktop import CloudDesktop
    from db.models.file_asset import FileAsset
    from db.repository.cloud_desktop_repo import _snapshot_conditions
    from sandbox.channel import route_for_record
    from sandbox.assets import _use_internal_oss
    if not record or record["pool_state"] != "assigned" or record["tunnel_state"] != "up":
        return
    host, port, key = route_for_record(record)
    async with httpx.AsyncClient(base_url=f"http://{host}:{port}", headers={"X-API-Key": key},
                                 trust_env=False, timeout=20) as client:
        response = await client.get("/resource-control/cache/status")
        if response.status_code == 404:
            return
        response.raise_for_status()
        status = response.json()
        if status.get("protocol") != "managed_cache_v1":
            raise RuntimeError("Unexpected cache protocol")
        if status["space"]["available_fraction"] < .10:
            log.warning("Desktop %s remains below 10%% free; cache counts=%s", record["desktop_id"], status["counts"])
        if status["counts"].get("quarantined"):
            log.warning("Desktop %s has protected cache files requiring review: %s", record["desktop_id"], status["counts"]["quarantined"])
        conditions = _snapshot_conditions(record)
        if conditions is None:
            raise RuntimeError("Missing desktop binding")
        for task in status.get("tasks", [])[:32]:
            async with get_db_session() as db:
                current = await db.scalar(select(CloudDesktop.id).where(*conditions))
                if not current:
                    return
                if await terminal_locked(db, task, record["workspace_id"]):
                    if not await db.scalar(select(CloudDesktop.id).where(*conditions).with_for_update()):
                        return
                    result = await client.post("/resource-control/cache/settle", json={"task": task["id"],
                        "generation": task["generation"], "run_id": task["run_id"], "resource": status.get("resource")})
                    if result.status_code not in (409, 423):
                        result.raise_for_status()
        oss = get_oss()
        for pending in status.get("pending", [])[:32]:
            if pending["state"] not in {"discovered", "pending", "uploading", "archived"}:
                continue
            async with get_db_session() as db:
                if not await db.scalar(select(CloudDesktop.id).where(*conditions).with_for_update()):
                    return
                asset = await db.get(FileAsset, pending["id"])
                if pending["state"] == "discovered":
                    from db.models.session import Session
                    import mimetypes
                    import posixpath
                    session = await db.get(Session, pending["task_id"])
                    if session is None or session.workspace_id != record["workspace_id"]:
                        continue
                    name = posixpath.basename(pending["path"])
                    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    key = f"recovery/{session.user_id}/{record['desktop_id']}/{pending['id']}/{name}"
                    if asset is None:
                        asset = FileAsset(id=pending["id"], user_id=session.user_id, workspace_id=session.workspace_id,
                            session_id=session.id, project_id=session.project_id, name=name, mime=mime, oss_key=key,
                            size=pending["size"], status="pending", source="agent", transient=False,
                            description="任务结束或中断后恢复的本地文件", created_at=datetime.now(timezone.utc))
                        db.add(asset)
                        await db.commit()
                        if not await db.scalar(select(CloudDesktop.id).where(*conditions).with_for_update()):
                            return
                    if asset.oss_key != key or asset.size != pending["size"]:
                        continue
                    assigned = await client.post("/resource-control/cache/assign", json={"entry_id": asset.id, "oss_key": key, "mime": mime})
                    assigned.raise_for_status()
                    pending = {**pending, "oss_key": key, "mime": mime, "state": "pending"}
                if (asset is None or asset.is_deleted or asset.workspace_id != record["workspace_id"]
                        or asset.session_id != pending["task_id"] or asset.oss_key != pending["oss_key"]
                        or asset.size != pending["size"]):
                    continue
                if pending["state"] == "archived":
                    head = await oss.head(asset.oss_key)
                    if head and head["size"] == asset.size and head.get("etag", "").lower() == pending["md5"]:
                        asset.status = "ready"
                        await db.commit()
                        if not await db.scalar(select(CloudDesktop.id).where(*conditions).with_for_update()):
                            return
                        receipt = await client.post("/resource-control/cache/ack", json={"entry_id": asset.id,
                            "size": asset.size, "etag": pending["md5"]})
                        receipt.raise_for_status()
                else:
                    # Bound to the existing tenant asset; no invented recovery
                    # owner and no unindexed object that a user cannot find.
                    result = await client.post("/resource-control/cache/recovery", json={"entry_id": asset.id,
                        "url": oss.presign_put(asset.oss_key, pending["mime"], expires_sec=1800, internal=_use_internal_oss(oss)),
                        "expires": time.time() + 1700})
                    result.raise_for_status()
