"""Pin an OpenBox shot to a live ComfyUI worker before its input uploads."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid

import httpx
from sqlalchemy import select, text

from core.config import get_config
from db.base import get_db_session
from db.models.video_job import VideoJob
from tool.video_comfyui import client, configured_endpoint
from tool.video_providers import VideoRequestError, provider_route_fingerprint


class NoAvailableWorker(VideoRequestError):
    """No prompt has been sent when the pool cannot choose a worker."""


def worker_route(route, name):
    provider = get_config().provider.get(name)
    if provider is None or not provider.options.get("h3_workbench_worker"):
        raise VideoRequestError("ComfyUI 执行机配置不可用。")
    endpoint, token = configured_endpoint(provider)
    return replace(route, provider=name, base_url=endpoint, api_key=token,
                   tls_ca_file=provider.options.get("tls_ca_file"), comfy_workers=(), workbench=True)


def owner(route, request):
    name = (request or {}).get("comfy_worker")
    return worker_route(route, name) if name else route


async def _queue(route):
    try:
        async with client(route, 4) as connection:
            response = await connection.get("/queue")
            response.raise_for_status()
            queue = response.json()
            return {row[1] for key in ("queue_pending", "queue_running") for row in queue[key]}
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
        return None


def shot_cost(data):
    # Relative GPU work from the shot length, canvas and sampling steps.
    from tool.video_comfyui import MEGAPIXELS
    return ((float(data.get("duration") or 5) + 2) * MEGAPIXELS.get(data.get("resolution"), 0.69)
            * (1.2 if data.get("h3_profile") == "poster" else 1))


async def reserve(route, job_id):
    workers = [worker_route(route, name) for name in route.comfy_workers]
    async with get_db_session() as db:
        # All API workers see the same reservations, including uploads which
        # have not appeared in a ComfyUI queue yet.
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(1847732481)"))
        elif db.get_bind().dialect.name == "sqlite":
            await db.execute(text("BEGIN IMMEDIATE"))
        job = await db.get(VideoJob, job_id, with_for_update=True)
        if job.request_data.get("comfy_worker"):
            return owner(route, job.request_data), job.request_data["comfy_prompt_id"]
        queues = await asyncio.gather(*(_queue(worker) for worker in workers))
        costs = {w.provider: {pid: None for pid in q} for w, q in zip(workers, queues) if q is not None}
        if not costs:
            raise NoAvailableWorker("当前无法读取 ComfyUI 执行机的队列，镜头尚未提交。")
        rows = (await db.scalars(select(VideoJob).where(
            VideoJob.kind == "segment", VideoJob.model == job.model,
            VideoJob.status.in_(("submitting", "queued", "in_progress")),
            VideoJob.updated_at > datetime.now(timezone.utc) - timedelta(days=1),
        ))).all()
        now = datetime.now(timezone.utc)
        for row in rows:
            data = row.request_data or {}
            loads = costs.get(data.get("comfy_worker"))
            pid = data.get("comfy_prompt_id")
            age = now - row.updated_at.replace(tzinfo=row.updated_at.tzinfo or timezone.utc)
            uploading = row.status == "submitting" and age < timedelta(minutes=10)
            posting = row.status == "queued" and age < timedelta(seconds=10)
            if loads is not None and pid and (pid in loads or uploading or posting):
                loads[pid] = shot_cost(data)
        # Rotate equal-load choices so isolated single-shot requests use both subscriptions.
        uid = uuid.uuid4()
        workloads = {name: round(sum(6 if cost is None else cost for cost in tasks.values()), 3)
                     for name, tasks in costs.items()}
        available = list(workloads)
        offset = uid.int % len(available)
        selected = min(available[offset:] + available[:offset], key=workloads.__getitem__)
        worker = next(w for w in workers if w.provider == selected)
        pid = str(uid)
        job.request_data = {**job.request_data, "comfy_worker": selected, "comfy_prompt_id": pid,
                            "comfy_dispatch_load": workloads,
                            "provider_route_fingerprint": provider_route_fingerprint(worker)}
        return worker, pid
