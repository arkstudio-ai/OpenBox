"""Transaction-bound job events using submission-time ownership."""
from __future__ import annotations

import hashlib

from trajectory import enabled, record
from trajectory.producers import activity_context, paused_in_tx
from trajectory.types import canonical

CONTEXT_KEY = "_trajectory_context"
_TERMINAL = {"completed", "failed", "cancelled", "published", "draft", "expired", "unknown", "timed_out"}


async def record_job_in_tx(db, job, *, submitted=False, session_id: str | None = None):
    """No latest-session lookup: a callback follows its saved root and call.

    Facts wait for the job row's commit. A callback for a deleted session is
    dropped by the worker, which keeps the session's tombstone. Job writes hold
    no session row lock, so nothing is recorded while the session's recording
    is paused and not resumed yet (SPEC §5.6).
    """
    if not enabled(job.user_id):
        return None
    field = "request_data" if hasattr(job, "request_data") else "details"
    metadata = getattr(job, field) or {}
    saved = metadata.get(CONTEXT_KEY)
    source_session = (saved or {}).get("source_session_id") or session_id or getattr(job, "session_id", None)
    if not source_session:
        return None
    context = await activity_context(db, job.user_id, source_session, saved=saved)
    if context is None or await paused_in_tx(db, context):
        return None
    if not saved:
        setattr(job, field, {**metadata, CONTEXT_KEY: context.to_dict()})
        if not submitted:
            await record("baseline.captured", {
                "origin": "preexisting_job", "preexisting": True,
                "jobs": [{"job_id": job.id, "status": job.status,
                          "kind": getattr(job, "kind", getattr(job, "platform", None))}],
            }, context=context, db=db, event_id=f"job_adopt:{job.id}")
    kind = "job.submitted" if submitted else "job.finished" if job.status in _TERMINAL else "job.progress"
    data = {"job_id": job.id, "job_type": getattr(job, "kind", getattr(job, "platform", None)),
            "status": "completed" if job.status in {"published", "draft"} else job.status,
            "result_state": job.status,
            "model": getattr(job, "model", None), "error": getattr(job, "error", None),
            "output_asset_id": getattr(job, "output_asset_id", None),
            "provider_task_id": getattr(job, "provider_task_id", None),
            "provider_request_ids": metadata.get("_trajectory_request_ids", [])}
    if submitted:
        data["input"] = {key: value for key, value in metadata.items() if not key.startswith("_trajectory_")}
        data["prompt"] = getattr(job, "prompt", getattr(job, "title", None))
    else:
        data["result"] = getattr(job, "result_data", None) or {
            "item_id": getattr(job, "item_id", None), "video_id": getattr(job, "video_id", None),
            "details": {key: value for key, value in metadata.items() if not key.startswith("_trajectory_")}}
    try:
        digest = hashlib.sha256(canonical(data)).hexdigest()[:24]
    except (TypeError, ValueError):
        # Business values the canonical encoding rejects still get a stable id.
        digest = hashlib.sha256(repr(sorted(data.items(), key=lambda item: item[0])).encode()).hexdigest()[:24]
    await record(kind, data, context=context, db=db, event_id=f"job:{job.id}:{digest}")
    if not submitted and job.status in _TERMINAL and context.run_id:
        from db.models.question import SessionExecution
        execution = await db.get(SessionExecution, source_session)
        if execution is None or execution.run_id != context.run_id:
            await record("operation.late_result", {
                "job_id": job.id, "original_run_id": context.run_id,
                "status": data["status"], "result": data.get("result"),
            }, db=db, context=context, event_id=f"job_late:{job.id}:{digest}")
    return context
