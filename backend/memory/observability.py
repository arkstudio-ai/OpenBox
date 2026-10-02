"""Per-attempt bounded diagnostics, with current authorization on every read."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, update

from core.identifier import ascending
from db.base import get_db_session
from db.models.memory_pipeline import MemoryExtractionJob
from db.models.memory_v2 import MemoryDebugRun, MemoryDebugStep, MemoryIndexState, MemoryOutbox
from db.models.session import Session
from memory.index.qdrant import QdrantMemoryIndex
from memory.policy import resolve_access_scope
from memory.redaction import redact_value, text_hash


def _now():
    return datetime.now(timezone.utc)


async def purge_expired_debug_snapshots(*, limit=100):
    """Background-only retention cleanup; ordinary diagnostic GETs remain pure."""
    from db.models.memory_runtime import MemoryReplayPreview
    limit = max(1, min(limit, 500))
    async with get_db_session() as db:
        runs = list((await db.scalars(select(MemoryDebugRun).where(MemoryDebugRun.expires_at <= _now(),
            MemoryDebugRun.status != "EXPIRED").order_by(MemoryDebugRun.expires_at).limit(limit))).all())
        for run in runs:
            run.input_snapshot, run.source_refs, run.status = {}, [], "EXPIRED"
            await db.execute(update(MemoryDebugStep).where(MemoryDebugStep.run_id == run.id).values(
                data={}, status="EXPIRED", reason_code="debug_retention_expired"))
        # A short-lived replay grant also contains its redacted query. Keep
        # claim receipts and hashes, but erase that body after its deadline.
        previews = list((await db.scalars(select(MemoryReplayPreview).where(
            MemoryReplayPreview.expires_at <= _now(), MemoryReplayPreview.grant != {}).order_by(
                MemoryReplayPreview.expires_at).limit(limit))).all())
        for preview in previews:
            preview.grant = {}
    return {"runs_purged": len(runs), "preview_grants_purged": len(previews)}


def capabilities(config, user_id):
    return {name: config.enabled(name, user_id) for name in
            ("v2_write", "auto_extract", "index_sync", "retrieval_v2", "route_jev", "debug_view",
             "debug_replay", "rerank", "wiki", "backfill")}


async def create_debug_run(query, scope, config, *, request_id=None, session_id=None,
                           turn_id=None, parent_run_id=None, input_metadata=None):
    if not config.enabled("debug_view", scope.user_id):
        return None
    run_id = ascending("memoryrun")
    async with get_db_session() as db:
        current = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id,
                                             project_id=scope.project_id)
        if session_id and not await db.scalar(select(Session.id).where(Session.id == session_id,
            *current.predicates(Session), Session.is_deleted.is_(False))):
            return None
        snapshot = {"utterance": query, "truncated": len(query) > config.debug_snapshot_max_chars,
                    "redacted": True, "metadata": input_metadata or {}, "scope": {
                        "workspace_id": scope.workspace_id, "project_id": scope.project_id,
                        "visibility": "PERSONAL", "acl_epoch": scope.acl_epoch}}
        db.add(MemoryDebugRun(id=run_id, request_id=request_id or ascending("memoryrequest"),
            attempt_id=ascending("memoryattempt"), parent_run_id=parent_run_id, session_id=session_id,
            turn_id=turn_id, user_id=scope.user_id, workspace_id=scope.workspace_id, project_id=scope.project_id,
            status="RUNNING", policy_version=config.policy_version, input_hash=text_hash(query),
            input_snapshot=redact_value(snapshot, limit=config.debug_snapshot_max_chars), source_refs=[], usage={},
            created_at=_now(), expires_at=_now() + timedelta(days=config.debug_retention_days)))
    return run_id


async def add_debug_step(run_id, phase, status, *, data=None, usage=None, reason_code=None, duration_ms=None):
    if run_id is None:
        return
    from core.config import get_config
    config = get_config().memory
    async with get_db_session() as db:
        run = await db.get(MemoryDebugRun, run_id)
        if run is None:
            return
        db.add(MemoryDebugStep(id=ascending("memorystep"), run_id=run_id, phase=phase[:32], status=status[:24],
            reason_code=reason_code[:64] if reason_code else None,
            data=redact_value(data or {}, limit=config.debug_snapshot_max_chars),
            usage=redact_value(usage or {}, limit=config.debug_snapshot_max_chars),
            duration_ms=duration_ms, created_at=_now()))


async def finish_debug_run(run_id, status="SUCCEEDED", *, source_refs=None, usage=None):
    if run_id is None:
        return
    async with get_db_session() as db:
        row = await db.get(MemoryDebugRun, run_id)
        if row:
            row.status = status
            row.source_refs = source_refs or []
            row.usage = redact_value(usage or {})


def _run_summary(row):
    return {"id": row.id, "run_id": row.id, "request_id": row.request_id, "attempt_id": row.attempt_id,
            "parent_run_id": row.parent_run_id, "session_id": row.session_id, "turn_id": row.turn_id,
            "project_id": row.project_id, "status": row.status, "policy_version": row.policy_version,
            "created_at": row.created_at.isoformat(), "expires_at": row.expires_at.isoformat(),
            "usage": redact_value(row.usage)}


async def list_debug_runs(scope, config, *, project_id=None, session_id=None, request_id=None,
                          status=None, limit=30, cursor=None, since=None, until=None):
    async with get_db_session() as db:
        current = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        stmt = select(MemoryDebugRun).where(*current.predicates(MemoryDebugRun), MemoryDebugRun.expires_at > _now())
        if session_id:
            stmt = stmt.where(MemoryDebugRun.session_id == session_id)
        if request_id:
            stmt = stmt.where(MemoryDebugRun.request_id == request_id)
        if status:
            values = {"completed": ["completed", "SUCCEEDED", "COMPLETED", "SUCCESS"],
                      "failed": ["FAILED", "DEAD", "failed"], "running": ["RUNNING", "running"],
                      "degraded": ["DEGRADED", "degraded"], "cancelled": ["CANCELLED", "cancelled"]}.get(
                          status.lower(), [status.lower(), status.upper()])
            stmt = stmt.where(MemoryDebugRun.status.in_(values))
        if since:
            stmt = stmt.where(MemoryDebugRun.created_at >= since)
        if until:
            stmt = stmt.where(MemoryDebugRun.created_at <= until)
        if cursor:
            # Wiki diagnostics use content-derived IDs. Chronology therefore
            # cannot be inferred from the ID prefix or its lexical ordering.
            anchor = await db.scalar(stmt.where(MemoryDebugRun.id == cursor))
            if anchor is None:
                return {"runs": [], "next_cursor": None, "capabilities": capabilities(config, scope.user_id)}
            stmt = stmt.where(or_(MemoryDebugRun.created_at < anchor.created_at, and_(
                MemoryDebugRun.created_at == anchor.created_at, MemoryDebugRun.id < anchor.id)))
        rows = list((await db.scalars(stmt.order_by(MemoryDebugRun.created_at.desc(),
            MemoryDebugRun.id.desc()).limit(limit + 1))).all())
        visible = []
        for row in rows[:limit]:
            if row.session_id and not await db.scalar(select(Session.id).where(Session.id == row.session_id,
                *current.predicates(Session), Session.is_deleted.is_(False))):
                continue
            visible.append(_run_summary(row))
        return {"runs": visible, "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
                "capabilities": capabilities(config, scope.user_id)}


def _unavailable_snapshot(value):
    """Retain safe stage counts/state, not identifiers or former source text."""
    if isinstance(value, dict):
        return {key: _unavailable_snapshot(item) for key, item in value.items()
                if key not in {"utterance", "text", "body", "summary", "diff", "items", "candidates", "sources", "source_refs", "input", "task_state"}}
    if isinstance(value, list):
        return []
    return value


async def debug_input_is_current(db, row, scope):
    """Check the original user turn using the pure event projector; GET never seeds or repairs."""
    if not row.turn_id:
        return True
    if not row.session_id:
        return False
    from db.models.agent_event import AgentEvent
    from session.agent_event_log import AgentEventProjectionError, project_agent_events
    original = row
    seen = set()
    for _ in range(16):
        if not original.parent_run_id:
            break
        if original.id in seen:
            return False
        seen.add(original.id)
        original = await db.scalar(select(MemoryDebugRun).where(MemoryDebugRun.id == original.parent_run_id,
            *scope.predicates(MemoryDebugRun), MemoryDebugRun.expires_at > _now()))
        if original is None:
            return False
    else:
        return False
    if original.session_id != row.session_id or original.turn_id != row.turn_id:
        return False
    events = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == row.session_id,
        AgentEvent.user_id == scope.user_id).order_by(AgentEvent.sequence))).all())
    try:
        public = project_agent_events(events)
    except (AgentEventProjectionError, ValueError, TypeError, KeyError):
        return False
    message = next((item for item in public["messages"] if item["id"] == row.turn_id), None)
    if message is None or message.get("role") != "user":
        return False
    texts = []
    for part in message.get("parts", []):
        data = part.get("data") if isinstance(part.get("data"), dict) else part
        if part.get("type") == "text" and not data.get("synthetic"):
            texts.append(data.get("text", ""))
    return text_hash("\n".join(texts)) == original.input_hash


async def read_debug_run(run_id, scope, config):
    from memory.retrieval import authorized_documents
    async with get_db_session() as db:
        current = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id,
                                             include_all_projects=True)
        row = await db.scalar(select(MemoryDebugRun).where(MemoryDebugRun.id == run_id,
            *current.predicates(MemoryDebugRun), MemoryDebugRun.expires_at > _now()))
        if row is None:
            return None
        run_scope = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id,
                                              project_id=row.project_id)
        if row.session_id and not await db.scalar(select(Session.id).where(Session.id == row.session_id,
            *run_scope.predicates(Session), Session.is_deleted.is_(False))):
            return None
        references = row.source_refs or []
        keys = {(entry["kind"], entry["id"]) for entry in references if "kind" in entry and "id" in entry}
        docs = await authorized_documents(db, run_scope, config, only=keys) if keys else []
        available = {(doc.kind, doc.id): doc for doc in docs}
        input_available = await debug_input_is_current(db, row, run_scope)
        bodies_available = input_available and all((entry.get("kind"), entry.get("id")) in available and
            available[(entry["kind"], entry["id"])].revision == entry.get("revision") for entry in references)
        metadata = row.input_snapshot or {}
        run = {**_run_summary(row), "input_hash": row.input_hash,
               "input_snapshot": redact_value(metadata if bodies_available else _unavailable_snapshot(metadata)),
               "source_refs": references if bodies_available else [], "body_available": bodies_available,
               "body_unavailable_reason": None if bodies_available else "input_unavailable_or_changed" if not input_available else "source_unavailable_or_changed"}
        steps = list((await db.scalars(select(MemoryDebugStep).where(MemoryDebugStep.run_id == row.id).order_by(
            MemoryDebugStep.created_at, MemoryDebugStep.id))).all())
        output = [{"id": step.id, "phase": step.phase, "status": step.status, "reason_code": step.reason_code,
                   "duration_ms": step.duration_ms, "created_at": step.created_at.isoformat(),
                   "data": redact_value(step.data if bodies_available else _unavailable_snapshot(step.data)),
                   "usage": redact_value(step.usage)} for step in steps]
        if row.session_id:
            jobs = list((await db.scalars(select(MemoryExtractionJob).where(
                *run_scope.predicates(MemoryExtractionJob), MemoryExtractionJob.session_id == row.session_id,
                MemoryExtractionJob.logical_turn_id == row.turn_id,
                MemoryExtractionJob.created_at >= row.created_at - timedelta(seconds=30)).order_by(
                    MemoryExtractionJob.created_at.desc()).limit(5))).all())
            run["async_jobs"] = [{"id": job.id, "state": job.state, "attempts": job.attempts,
                "last_error": job.last_error, "usage": redact_value(job.usage or {}),
                "result_memory_ids": job.result_memory_ids if bodies_available else []} for job in jobs]
    return {"run": run, "steps": output, "capabilities": capabilities(config, scope.user_id)}


async def memory_health(scope, config):
    from db.models.memory_wiki import MemoryWikiPage, MemoryWikiJob
    instant = _now()
    def age(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0, (instant - value).total_seconds())
    async with get_db_session() as db:
        current = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id,
                                             include_all_projects=True)
        counts, queues = {}, {}
        for name, model, field in (("jobs", MemoryExtractionJob, MemoryExtractionJob.state),
                                   ("outbox", MemoryOutbox, MemoryOutbox.status),
                                   ("index", MemoryIndexState, MemoryIndexState.status),
                                   ("wiki_pages", MemoryWikiPage, MemoryWikiPage.status),
                                   ("wiki_jobs", MemoryWikiJob, MemoryWikiJob.status)):
            result = await db.execute(select(field, func.count()).where(*current.predicates(model)).group_by(field))
            counts[name] = dict(result.all())
            if name in {"jobs", "outbox", "wiki_jobs"}:
                oldest = await db.scalar(select(func.min(model.created_at)).where(*current.predicates(model),
                    field.in_(("PENDING", "RETRY", "RUNNING"))))
                expired = await db.scalar(select(func.count()).select_from(model).where(*current.predicates(model),
                    field == "RUNNING", model.lease_until < instant))
                retries = await db.scalar(select(func.coalesce(func.sum(model.attempts - 1), 0)).where(
                    *current.predicates(model), model.attempts > 1))
                queues[name] = {"oldest_pending_age_seconds": age(oldest), "expired_leases": expired,
                                "retry_attempts": retries}
        deliveries = list((await db.scalars(select(MemoryOutbox).where(*current.predicates(MemoryOutbox),
            MemoryOutbox.delivered_at.is_not(None)).order_by(MemoryOutbox.delivered_at.desc()).limit(20))).all())
        index_usage = [{"object_kind": row.object_kind, "object_id": row.object_id, "revision": row.revision,
                        "operation": row.operation, "generation": row.index_generation,
                        "delivered_at": row.delivered_at.isoformat(), "usage": redact_value((row.payload or {}).get("usage", {}))}
                       for row in deliveries]
        from memory.retrieval import index_lag
        lag = await index_lag(db, current, config.index_generation)
        stage_counts = (await db.execute(select(MemoryDebugStep.phase, MemoryDebugStep.status,
            MemoryDebugStep.reason_code, func.count()).join(MemoryDebugRun,
                MemoryDebugRun.id == MemoryDebugStep.run_id).where(*current.predicates(MemoryDebugRun),
                MemoryDebugRun.expires_at > instant).group_by(MemoryDebugStep.phase,
                MemoryDebugStep.status, MemoryDebugStep.reason_code))).all()
        observations = [{"phase": phase, "status": status, "reason_code": reason, "count": count}
                        for phase, status, reason, count in stage_counts]
    index = await QdrantMemoryIndex(config).health()
    index.pop("points_count", None)  # Global collection counts are not actor telemetry.
    return {"capabilities": capabilities(config, scope.user_id), "index": {**index, "counts": counts["index"], "lag": lag},
            "jobs": {"counts": counts["jobs"], **queues["jobs"]},
            "outbox": {"counts": counts["outbox"], **queues["outbox"], "recent_index_usage": index_usage,
                       "observations": observations, "observation_retention_days": config.debug_retention_days},
            "wiki": {"enabled": config.enabled("wiki", scope.user_id), "page_counts": counts["wiki_pages"],
                     "job_counts": counts["wiki_jobs"], **queues["wiki_jobs"]},
            "models": {"embedding": config.embedding_model, "dimensions": config.embedding_dimensions,
                       "rerank": config.rerank_model, "jev": config.jev_model,
                       "extraction": config.extract_model or "configured_main_model"}}
