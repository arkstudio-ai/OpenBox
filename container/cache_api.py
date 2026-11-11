"""Authenticated Action Server adapter for the desktop cache journal."""
import asyncio
import os

from fastapi import Request
from fastapi.responses import JSONResponse

from cache_control import CacheError, CacheStore, PROTOCOL


def configured_store(identity=None):
    path = os.environ.get("OPENBOX_CACHE_CONTROL_DB")
    if not path:
        return None
    return CacheStore(path, uid=identity.pw_uid if identity else 0, gid=identity.pw_gid if identity else 0)


def task_fence(request):
    headers = request.headers
    task = headers.get("X-OpenBox-Cache-Task")
    run = headers.get("X-OpenBox-Cache-Run")
    try:
        generation = int(headers.get("X-OpenBox-Cache-Generation", "0"))
    except ValueError:
        raise CacheError("INVALID_CACHE_GENERATION", 400)
    if not task or not run or generation < 1:
        raise CacheError("CACHE_TASK_IDENTITY_REQUIRED", 400)
    return task, generation, run


async def execution_environment(store, request, env):
    if store is None or not request.headers.get("X-OpenBox-Cache-Task"):
        return env
    task, generation, run = task_fence(request)
    admitted = await asyncio.to_thread(store.begin, task, generation, run)
    return {**env, "TMPDIR": admitted["tmpdir"], "TMP": admitted["tmpdir"],
            "TEMP": admitted["tmpdir"], "OPENBOX_CACHE_TASK": task,
            "OPENBOX_PENDING_ROOT": admitted["pending_dir"], "OPENBOX_CACHE_RUN": run}


def mount(app, store, checkpoint, quiescent, get_resource_gate):
    @app.exception_handler(CacheError)
    async def cache_error(request, exc):
        quiescent(request)
        return JSONResponse(status_code=exc.status, content={"detail": exc.code})

    def require():
        if store is None:
            raise CacheError("MANAGED_CACHE_UNAVAILABLE", 503)
        return store

    @app.get("/resource-control/cache/status")
    async def status():
        result = await asyncio.to_thread(require().status)
        gate = get_resource_gate()
        if gate is not None:
            observed = await asyncio.to_thread(gate.status)
            result["resource"] = {key: observed[key] for key in ("journal_id", "control", "blocking_count")}
        else:
            result["resource"] = None
        return result

    @app.post("/resource-control/cache/settle")
    async def settle(request: Request):
        """Exact terminal-task observation from the backend's locked SQL rows."""
        data = await request.json()
        if set(data) != {"task", "generation", "run_id", "resource"}:
            raise CacheError("INVALID_CACHE_SETTLEMENT", 400)

        def apply():
            gate = get_resource_gate()
            if gate is None:
                if data["resource"] is not None:
                    raise CacheError("RESOURCE_JOURNAL_CHANGED")
                return require().recover_terminal(data["task"], data["generation"], data["run_id"])
            # Hold the journal while deciding: a resource handoff cannot race
            # this fixed cache-only maintenance operation.
            with gate.transaction() as db:
                observed = gate._status(db)
                current = {key: observed[key] for key in ("journal_id", "control", "blocking_count")}
                control = current["control"]
                if current != data["resource"] or current["blocking_count"] or control and (
                        control["admission"] != "open" or control["owner_kind"] != "automation"):
                    raise CacheError("RESOURCE_NOT_DRAINED", 423)
                return require().recover_terminal(data["task"], data["generation"], data["run_id"])
        return await asyncio.to_thread(apply)

    @app.post("/resource-control/cache/recovery")
    async def recovery(request: Request):
        """Refresh a presigned PUT for an already frozen pending object only.

        This control-plane endpoint cannot create files, choose source paths,
        release tasks or delete data. Recovery can continue while admission is
        closed; sole-output protection is unaffected.
        """
        data = await request.json()
        if set(data) != {"entry_id", "url", "expires"}:
            raise CacheError("INVALID_CACHE_RECOVERY", 400)
        return await asyncio.to_thread(require().authorize_upload, **data)

    @app.post("/resource-control/cache/ack")
    async def acknowledge(request: Request):
        data = await request.json()
        if set(data) != {"entry_id", "size", "etag"}:
            raise CacheError("INVALID_CACHE_ACK", 400)
        return await asyncio.to_thread(require().acknowledge_archive, **data)

    @app.post("/resource-control/cache/assign")
    async def assign_recovery(request: Request):
        data = await request.json()
        if set(data) != {"entry_id", "oss_key", "mime"}:
            raise CacheError("INVALID_RECOVERY_ASSIGNMENT", 400)
        return await asyncio.to_thread(require().assign_recovery, **data)

    @app.post("/cache/{action}")
    async def command(action: str, request: Request):
        current = require()
        await checkpoint(request)
        task, generation, run = task_fence(request)
        if int(request.headers.get("content-length", "0")) > 16384:
            raise CacheError("CACHE_REQUEST_TOO_LARGE", 413)
        data = await request.json()
        methods = {"admit": current.begin, "asset-claim": current.claim_asset,
                   "asset-fetch": current.fetch_asset,
                   "asset-ready": current.asset_ready, "pending-prepare": current.prepare_pending,
                   "pending-adopt": current.adopt_pending,
                   "pending-register": current.register_pending, "release": current.release}
        methods["pending-upload"] = current.upload_pending
        methods["pending-ack"] = current.acknowledge_task_archive
        method = methods.get(action)
        if method is None or not isinstance(data, dict):
            raise CacheError("INVALID_CACHE_OPERATION", 400)
        # Reject caller attempts to replace the request's fenced identity.
        if {"task", "generation", "run_id"} & data.keys():
            raise CacheError("INVALID_CACHE_OPERATION", 400)
        try:
            result = await asyncio.to_thread(method, task, generation, run, **data)
        except TypeError:
            raise CacheError("INVALID_CACHE_OPERATION", 400)
        if action == "release" and result.get("released"):
            await asyncio.to_thread(current.index_released_temp)
            await asyncio.to_thread(current.index_incoming, task)
            result["collection"] = await asyncio.to_thread(current.collect, immediate=True)
        quiescent(request)
        return result
