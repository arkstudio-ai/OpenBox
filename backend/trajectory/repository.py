"""Trajectory reads against the trace database (SPEC 8.9).

Every read works at a fixed watermark H <= committed_seq. Events come from
hot rows and archived segments, gap and tail checked. Records, summaries and
search documents are written by the projection worker and serve reads at
H == projected_seq; any other H, or a cache written past H, falls back to a
checkpoint plus replay. Session identity comes from the metadata replicas
(``trajectory_meta_*``): no read touches the business database.
"""
from datetime import datetime
from types import SimpleNamespace
import asyncio
import base64
import hashlib
import json

from sqlalchemy import and_, case, func, or_, select

from trajectory.config import enabled, integer, selected_user_ids
from trajectory.payload import (LruCache, Resolver, ensure_payload_rows, expand_all, expand_pages, json_blob, reference,
    require_content)
from trajectory.projector import agents, contribution, empty_state, reduce, statistics
from trajectory.segments import load_segment_lines
from trajectory.storage import get_blob_store
from trajectory.store.models import (SessionTrajectory, TrajectoryCheckpoint, TrajectoryEvent, TrajectoryMetaSession,
    TrajectoryMetaUser, TrajectoryMetaWorkspace, TrajectoryRecord, TrajectoryRecordEvent, TrajectorySegment,
    TrajectorySessionSummary)
from trajectory.types import (PROJECTOR_VERSION, CorruptContent, TrajectoryError, canonical, digest, iso, now,
    sequence)

#: Keys the projection worker keeps next to the summary statistics so a head
#: read returns the same state as a replay; _metadata strips them.
HIDDEN_STATISTICS = ("unsupported_events", "coverage_start", "unsupported_events_count")
#: Unsupported events a state lists, the first ones by seq; the summary statistics also count all of them.
UNSUPPORTED_EVENTS_LIMIT = 100
#: A replay gives the event loop back this often.
REPLAY_YIELD_EVENTS = 200
RECORD_ID_WIDTH = 256


def record_key(record_id: str) -> str:
    """The primary-key form of a record id; ids wider than the column keep a prefix plus their digest."""
    if len(record_id) <= RECORD_ID_WIDTH:
        return record_id
    return f"{record_id[:RECORD_ID_WIDTH - 65]}#{hashlib.sha256(record_id.encode()).hexdigest()}"


def cursor_encode(value: list | dict) -> str:
    return base64.urlsafe_b64encode(canonical(value)).decode().rstrip("=")


def cursor_decode(value: str):
    try:
        if len(value) > 8192:
            raise ValueError("Oversized cursor")
        parsed = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(parsed, list) or len(parsed) != 3:
            raise ValueError("Invalid cursor shape")
        return parsed
    except (ValueError, TypeError) as exc:
        raise TrajectoryError("Invalid trajectory cursor") from exc


def _session_from_trajectory(trajectory: SessionTrajectory) -> SimpleNamespace:
    # The metadata replica lags the business write by up to one sync interval.
    return SimpleNamespace(id=trajectory.session_id, user_id=trajectory.user_id, workspace_id=trajectory.workspace_id,
        project_id=None, parent_id=None, kind=None, title=None, status=None, model=None, agent=None,
        is_deleted=False, deleted_at=None, created_at=trajectory.started_at, updated_at=trajectory.last_activity_at)


async def get_trajectory(db, session_id: str, *, optional=False):
    """(session metadata, live trajectory or None); LookupError for a missing or deleted session."""
    session = await db.get(TrajectoryMetaSession, session_id)
    if session is not None and (session.is_deleted or session.deleted_at is not None):
        raise LookupError("Session not found")
    query = select(SessionTrajectory).where(SessionTrajectory.session_id == session_id,
                                            SessionTrajectory.deleted_at.is_(None))
    if session is not None:
        query = query.where(SessionTrajectory.user_id == session.user_id)
    trajectory = await db.scalar(query)
    if session is None:
        if trajectory is None:
            raise LookupError("Session not found")
        session = _session_from_trajectory(trajectory)
    if trajectory is None and not optional:
        raise LookupError("Session has not started recording")
    return session, trajectory


def watermark(trajectory, requested=None) -> int:
    head = trajectory.committed_seq if trajectory is not None else 0
    return head if requested is None else sequence(requested, maximum=head)


# -- Events: hot rows and archived segments --

_segment_cache: LruCache | None = None


def segment_cache() -> LruCache:
    """Verified segment JSONL keyed by (storage key, sha256), within TRAJECTORY_SEGMENT_CACHE_BYTES."""
    global _segment_cache
    if _segment_cache is None:
        _segment_cache = LruCache(integer("TRAJECTORY_SEGMENT_CACHE_BYTES", 32 * 1024 * 1024))
    return _segment_cache


def reset_segment_cache() -> None:
    global _segment_cache
    _segment_cache = None


def _stored(row: TrajectoryEvent) -> dict:
    return {"event_id": row.event_id, "trajectory_id": row.trajectory_id, "seq": row.seq, "type": row.type,
            "version": row.version, "user_id": row.user_id, "session_id": row.session_id,
            "source_session_id": row.source_session_id, "request_id": row.request_id, "call_id": row.call_id,
            "agent_id": row.agent_id, "context": row.context, "data": row.data, "hints": row.hints,
            "content_hash": row.content_hash, "occurred_at": row.occurred_at, "recorded_at": row.recorded_at}


def event_dict(stored: dict) -> dict:
    """The API form of a stored event (hot row or segment line)."""
    return {"event_id": stored["event_id"], "trajectory_id": stored["trajectory_id"],
            "user_id": stored["user_id"], "session_id": stored["session_id"],
            **(stored["context"] or {}), "source_session_id": stored["source_session_id"],
            "seq": str(stored["seq"]), "type": stored["type"], "version": stored["version"],
            "occurred_at": iso(stored["occurred_at"]), "recorded_at": iso(stored["recorded_at"]), "data": stored["data"]}


async def _segment_lines(segment: TrajectorySegment, blob_store):
    cache = segment_cache()
    key = (segment.storage_key, segment.sha256)
    lines = cache.get(key)
    if lines is None:
        lines = await load_segment_lines(blob_store if blob_store is not None else get_blob_store(), segment)
        cache.put(key, lines, lines.size)
    else:
        from trajectory.read_budget import current_read_budget
        budget = current_read_budget()
        if budget is not None:
            budget.consume(lines.size)
    return lines


async def _archived(db, trajectory_id: str, seqs_from: int, seqs_to: int, blob_store, keep=None,
                    only: set[int] | None = None) -> list[dict]:
    """Segment rows with seqs_from <= seq <= seqs_to that keep accepts; with ``only``, just those seqs
    (segments holding none of them are not downloaded)."""
    if seqs_to < seqs_from or (only is not None and not only):
        return []
    segments = (await db.scalars(select(TrajectorySegment).where(TrajectorySegment.trajectory_id == trajectory_id,
        TrajectorySegment.to_seq >= seqs_from, TrajectorySegment.from_seq <= seqs_to)
        .order_by(TrajectorySegment.from_seq))).all()
    rows = []
    for segment in segments:
        if only is None:
            candidates = (await _segment_lines(segment, blob_store)).rows(seqs_from, seqs_to)
        else:
            low, high = max(seqs_from, segment.from_seq), min(seqs_to, segment.to_seq)
            wanted = sorted(seq for seq in only if low <= seq <= high)
            if not wanted:
                continue
            lines = await _segment_lines(segment, blob_store)
            candidates = [row for seq in wanted for row in lines.rows(seq, seq)]
        for row in candidates:
            if row["trajectory_id"] != trajectory_id:
                raise CorruptContent("Trajectory segment holds events of another trajectory")
            if keep is None or keep(row):
                rows.append(row)
    return rows


async def stored_events(db, trajectory, after: int, until: int, limit: int, *, blob_store=None) -> list[dict]:
    """Stored rows with after < seq <= until in seq order, at most limit.

    Hot rows are read before segments: archival writes a segment row and
    deletes the rows it covers in one transaction, so whatever the hot read
    missed is visible to the segment read that follows it.
    """
    # Nothing past after + limit can be returned: bounding the hot read keeps a
    # read of an archived range from loading hot rows it would drop.
    upper = min(until, after + limit)
    hot = [_stored(row) for row in (await db.scalars(select(TrajectoryEvent).where(
        TrajectoryEvent.trajectory_id == trajectory.id, TrajectoryEvent.seq > after, TrajectoryEvent.seq <= upper)
        .order_by(TrajectoryEvent.seq).limit(limit))).all()]
    first_hot = hot[0]["seq"] if hot else upper + 1
    archived = await _archived(db, trajectory.id, after + 1, first_hot - 1, blob_store)
    return (archived + hot)[:limit]


async def read_events(db, trajectory, *, after_seq=0, until_seq=None, limit=500, include_data=True, blob_store=None):
    until = watermark(trajectory, until_seq)
    after = sequence(after_seq, maximum=until)
    require_content(trajectory)
    rows = await stored_events(db, trajectory, after, until, limit + 1, blob_store=blob_store)
    has_more = len(rows) > limit
    events = []
    expected = after + 1
    for row in rows[:limit]:
        if row["seq"] != expected:
            raise CorruptContent(f"Trajectory sequence gap before {row['seq']}")
        expected += 1
        events.append(event_dict(row))
    if not has_more and expected - 1 != until:
        raise CorruptContent("Committed trajectory tail is missing")
    if include_data and events:
        values = await expand_all(db, trajectory.id, [event["data"] for event in events], through_seq=until,
                                  blob_store=blob_store)
        for event, value in zip(events, values):
            event["data"] = value
    return {"events": events, "from_seq": str(after + 1) if events else str(after),
            "through_seq": events[-1]["seq"] if events else str(after), "until_seq": str(until),
            "has_more": has_more, "committed_seq": str(trajectory.committed_seq)}


# -- Reduction input (shared by the projection worker and historical replay) --

STREAM_EVENTS = frozenset({"request.delta", "tool.output", "request.usage"})
#: Reference kinds kept inside the state a reducer works on. The projector only
#: copies message bodies; everything it compares or appends to is expanded.
KEPT_KINDS = frozenset({"message"})
_roles = LruCache(100_000)


async def reduction_events(resolver: Resolver, rows: list[dict]) -> list[tuple[dict, dict | None]]:
    """(event, hints) pairs the projector folds like the original events.

    References stay in place and previews come from hints, except where the
    projector reads a value itself: whole ``$payload`` data, stream chunks,
    tool outputs (appended to), committed messages and parts (their role,
    type and finish), and a request input's system prompt, tools and
    system-role messages (compared with the previous system record).
    """
    events = [event_dict(row) for row in rows]
    datas = await resolver.expand_payloads([event["data"] for event in events])
    full, inputs = [], []
    for index, (event, data) in enumerate(zip(events, datas)):
        if not isinstance(data, dict):
            continue
        if event["type"] in STREAM_EVENTS:
            full.append((index, None, data))
        elif event["type"].startswith("tool.") and "output" in data:
            full.append((index, "output", data["output"]))
        elif event["type"] in {"message.committed", "part.committed"}:
            full.extend((index, key, data[key]) for key in ("message", "part") if key in data)
        elif event["type"] == "request.prepared" and "input" in data:
            inputs.append((index, "input", data["input"]))

    def apply(slots, values):
        for (index, key, _), value in zip(slots, values):
            datas[index] = value if key is None else {**datas[index], key: value}
    apply(full, await resolver.expand_refs([slot[2] for slot in full]))
    apply(inputs, await resolver.expand_refs([slot[2] for slot in inputs], skip_kinds=KEPT_KINDS))

    # Without a system/instructions field the projector filters system and
    # developer messages by role, which a message reference hides.
    lookups = []
    for index, _, _ in inputs:
        actual = datas[index]["input"]
        if isinstance(actual, dict) and actual.get("system", actual.get("instructions")) is None:
            messages = actual.get("messages", actual.get("input"))
            if isinstance(messages, list):
                lookups.extend((index, position, element) for position, element in enumerate(messages) if _is_ref(element))
    role_key = lambda element: (resolver.trajectory_id, element["$ref"].get("payload_id"))  # noqa: E731
    unknown = [element for _, _, element in lookups if _roles.get(role_key(element)) is None]
    for element, message in zip(unknown, await resolver.expand_refs(unknown)):
        role = message.get("role") if isinstance(message, dict) and not _is_ref(message) else None
        _roles.put(role_key(element), role if isinstance(role, str) else "", 1)
    system = [slot for slot in lookups if _roles.get(role_key(slot[2])) in {"system", "developer"}]
    for (index, position, _), message in zip(system, await resolver.expand_refs([slot[2] for slot in system])):
        actual = datas[index]["input"]
        field = "messages" if "messages" in actual else "input"
        items = list(actual[field])
        items[position] = message
        datas[index] = {**datas[index], "input": {**actual, field: items}}
    return [({**event, "data": data}, row.get("hints")) for event, data, row in zip(events, datas, rows)]


def _is_ref(value) -> bool:
    return isinstance(value, dict) and len(value) == 1 and isinstance(value.get("$ref"), dict)


async def records_for_reduction(resolver: Resolver, rows: list[TrajectoryRecord]) -> dict[str, dict]:
    """Stored record rows as reducer state: record-level references expanded, message bodies kept."""
    values = await resolver.expand_refs([row.data for row in rows], skip_kinds=KEPT_KINDS)
    return {value["record_id"]: value for value in values}


# -- State at a watermark --

def _hidden(summary: TrajectorySessionSummary | None, through: int) -> dict:
    """coverage_start and unsupported_events of the summary as of through.

    The summary row is read after the records or the trajectory row, so a
    projection that committed in between may have added entries past through.
    """
    statistics_ = summary.statistics if summary is not None else {}
    unsupported = [item for item in statistics_.get("unsupported_events") or []
                   if isinstance(item, dict) and str(item.get("seq")).isdigit() and int(item["seq"]) <= through]
    return {"coverage_start": statistics_.get("coverage_start") if through >= 1 else None,
            "unsupported_events": unsupported}


async def _head_state(db, trajectory, through: int, resolver: Resolver) -> dict | None:
    """Expanded state from record rows at projected_seq, or None when a row was written past it."""
    rows = (await db.scalars(select(TrajectoryRecord).where(TrajectoryRecord.trajectory_id == trajectory.id)
        .order_by(TrajectoryRecord.start_seq, TrajectoryRecord.record_id))).all()
    relevant = [row for row in rows if row.start_seq <= through]
    if any(row.applied_seq > through or row.projector_version != PROJECTOR_VERSION for row in relevant):
        return None
    state = empty_state()
    state.update(through_seq=str(through), **_hidden(await db.get(TrajectorySessionSummary, trajectory.id), through))
    values = await resolver.expand_refs([row.data for row in relevant])
    state["records"] = {value["record_id"]: value for value in values}
    return state


async def _stored_checkpoint(db, trajectory, at_seq: int, *, blob_store=None) -> tuple[TrajectoryCheckpoint, dict] | None:
    """The latest same-version checkpoint <= at_seq with its digest-verified state (availability as stored)."""
    row = await db.scalar(select(TrajectoryCheckpoint).where(TrajectoryCheckpoint.trajectory_id == trajectory.id,
        TrajectoryCheckpoint.through_seq <= at_seq, TrajectoryCheckpoint.projector_version == PROJECTOR_VERSION)
        .order_by(TrajectoryCheckpoint.through_seq.desc()).limit(1))
    if row is None:
        return None
    state = dict(row.state)
    if "record_pages" in state:
        pages = await expand_pages(db, trajectory.id, state.pop("record_pages"), through_seq=at_seq, blob_store=blob_store)
        state["records"] = {key: value for page in pages for key, value in page["records"].items()}
    if digest(state) != row.digest:
        raise CorruptContent("Trajectory checkpoint digest mismatch")
    return row, state


async def replay_events(db, trajectory, state: dict, through: int, resolver: Resolver, *, blob_store=None,
                        observe=None) -> dict:
    """Fold the stored events after state["through_seq"] up to through (gap and tail checked).

    ``observe(row, event, before, after)`` sees each stored row, the event the
    reducer folded and the states around it.
    """
    after, replayed = int(state["through_seq"]), 0
    while after < through:
        rows = await stored_events(db, trajectory, after, through, 1000, blob_store=blob_store)
        if not rows:
            raise CorruptContent("Committed trajectory tail is missing")
        for offset, row in enumerate(rows):
            if row["seq"] != after + offset + 1:
                raise CorruptContent(f"Trajectory sequence gap before {row['seq']}")
        for row, (event, hints) in zip(rows, await reduction_events(resolver, rows)):
            before, state = state, reduce(state, event, hints)
            if observe is not None:
                observe(row, event, before, state)
            replayed += 1
            if replayed % REPLAY_YIELD_EVENTS == 0:
                # A read that replays while projection lags must not hold the worker's event loop.
                await asyncio.sleep(0)
        after = rows[-1]["seq"]
    return state


def bounded_unsupported(state: dict) -> dict:
    """The state with at most UNSUPPORTED_EVENTS_LIMIT unsupported events, the first ones, as summaries keep them."""
    unsupported = state.get("unsupported_events") or []
    if len(unsupported) <= UNSUPPORTED_EVENTS_LIMIT:
        return state
    return {**state, "unsupported_events": unsupported[:UNSUPPORTED_EVENTS_LIMIT]}


async def expanded_state(db, trajectory, through: int, *, blob_store=None, resolver: Resolver | None = None,
                         observe=None) -> dict:
    """The state at through with every ``$ref`` expanded and payload availability as stored.

    ``observe`` (see replay_events) sees the events replayed on top of a
    checkpoint; the head state read from record rows replays none. Either way
    the state lists at most UNSUPPORTED_EVENTS_LIMIT unsupported events.
    """
    require_content(trajectory)
    resolver = resolver or Resolver(db, trajectory.id, through_seq=through, blob_store=blob_store)
    if through == trajectory.projected_seq:
        state = await _head_state(db, trajectory, through, resolver)
        if state is not None:
            return bounded_unsupported(state)
    checkpoint = await _stored_checkpoint(db, trajectory, through, blob_store=blob_store)
    state = checkpoint[1] if checkpoint is not None else empty_state()
    state = await replay_events(db, trajectory, state, through, resolver, blob_store=blob_store, observe=observe)
    values = await resolver.expand_refs(list(state["records"].values()))
    return bounded_unsupported({**state, "records": dict(zip(state["records"], values))})


async def _visible_state(resolver: Resolver, state: dict) -> dict:
    values = await resolver.visible(list(state["records"].values()))
    return {**state, "records": dict(zip(state["records"], values))}


async def state_at(db, trajectory, through_seq=None, *, blob_store=None, observe=None):
    through = watermark(trajectory, through_seq)
    resolver = Resolver(db, trajectory.id, through_seq=through, blob_store=blob_store)
    return await _visible_state(resolver, await expanded_state(db, trajectory, through, blob_store=blob_store,
                                                               resolver=resolver, observe=observe))


async def get_checkpoint(db, trajectory, at_seq, *, blob_store=None):
    require_content(trajectory)
    found = await _stored_checkpoint(db, trajectory, at_seq, blob_store=blob_store)
    if found is None:
        return None
    row, state = found
    state = await _visible_state(Resolver(db, trajectory.id, through_seq=at_seq, blob_store=blob_store), state)
    return {"through_seq": str(row.through_seq), "projector_version": row.projector_version,
            "state": state, "digest": row.digest}


CHECKPOINT_PAGE_RECORDS = 100


def checkpoint_blobs(trajectory_id: str, state: dict) -> list[dict]:
    """Record pages of an expanded state as content-addressed blobs; unchanged pages keep their sha256."""
    ordered = sorted(state["records"].values(), key=lambda item: (int(item["start_seq"]), item["record_id"]))
    return [json_blob(trajectory_id, {"records": {item["record_id"]: item for item in
                                                  ordered[offset:offset + CHECKPOINT_PAGE_RECORDS]}})
            for offset in range(0, len(ordered), CHECKPOINT_PAGE_RECORDS)]


async def store_checkpoint(db, trajectory, state: dict, blobs: list[dict]) -> TrajectoryCheckpoint:
    """Insert the checkpoint of an expanded state whose page blobs are uploaded; the caller commits."""
    through_seq = int(state["through_seq"])
    rows, inserted = await ensure_payload_rows(db, trajectory.id, blobs, first_seq=through_seq)
    stored = {**state, "records": {}, "record_pages": [{"$payload": reference(rows[blob["dedupe_key"]])} for blob in blobs]}
    row = await db.get(TrajectoryCheckpoint, (trajectory.id, through_seq))
    if row is None:
        row = TrajectoryCheckpoint(trajectory_id=trajectory.id, through_seq=through_seq,
            projector_version=PROJECTOR_VERSION, state=stored, digest=digest(state), created_at=now())
        db.add(row)
    trajectory.checkpoint_seq = max(trajectory.checkpoint_seq or 0, through_seq)
    trajectory.stored_bytes = (trajectory.stored_bytes or 0) + inserted
    await db.flush()
    return row


# -- Sessions and headers --

def summary_rules(events: list[dict], running_status: str, model: str | None) -> tuple[str, str | None, bool]:
    """(running_status, model, gap seen) of a session summary after events, applied in order."""
    gap = False
    for event in events:
        family, _, action = event["type"].partition(".")
        data = event["data"] if isinstance(event["data"], dict) else {}
        if family == "run":
            if action == "started":
                running_status = "running"
            elif action in {"finished", "interrupted"}:
                running_status = "waiting" if data.get("status") == "waiting" else "error" if data.get("status") == "failed" else "idle"
        if family in {"permission", "question"} and action in {"requested", "asked"}:
            running_status = "waiting"
        if event["type"] == "request.started" and data.get("model"):
            model = str(data["model"])[:128]
        if event["type"] == "recording.gap" and data.get("phase") != "paused":
            gap = True
    return running_status, model, gap


SUMMARY_RULE_FAMILIES = frozenset({"run", "permission", "question"})
#: Ruled events a live header reads past the projection; a tail that reaches this many is replayed instead.
HEADER_TAIL_LIMIT = 2000


def _ruled(event_type: str) -> bool:
    """Whether ``summary_rules`` looks at an event of this type for the running status or the model."""
    return event_type.partition(".")[0] in SUMMARY_RULE_FAMILIES or event_type == "request.started"


def _row_metadata(session, trajectory, summary, owner, workspace) -> dict:
    stats = dict(summary.statistics) if summary else contribution(None)
    for key in HIDDEN_STATISTICS:
        stats.pop(key, None)
    missing = stats.pop("usage_missing", 0)
    request_count = stats.get("request_count", 0)
    stats.update(input_tokens=stats.get("input_tokens", 0) if request_count > missing else None,
        output_tokens=stats.get("output_tokens", 0) if request_count > missing else None,
        usage_complete=not missing, duration_ms=None,
        through_seq=str(summary.applied_seq if summary else 0), coverage_start=iso(trajectory.started_at) if trajectory else None)
    return {"session_id": session.id, "user_id": session.user_id, "trajectory_id": trajectory.id if trajectory else None,
        "title": session.title, "owner": {"user_id": session.user_id, "username": owner.username if owner else None, "email": owner.email if owner else None},
        "workspace": {"id": session.workspace_id, "name": workspace.name if workspace else None}, "workspace_id": session.workspace_id,
        "running_status": summary.running_status if summary else session.status,
        "recording_status": (trajectory.recording_status if enabled(session.user_id) else "paused") if trajectory else "not_recorded",
        "coverage_start": iso(trajectory.started_at) if trajectory else None,
        "last_activity_at": iso(summary.last_activity_at if summary else session.updated_at),
        "model": summary.model if summary else session.model, "agent": session.agent,
        "committed_seq": str(trajectory.committed_seq) if trajectory else "0",
        "projected_through_seq": str(trajectory.projected_seq) if trajectory else "0",
        "through_seq": str(trajectory.committed_seq) if trajectory else "0", "statistics": stats}


async def _metadata(db, session, trajectory, summary=None, owner=None, workspace=None):
    owner = owner if owner is not None else await db.get(TrajectoryMetaUser, session.user_id)
    if workspace is None and session.workspace_id:
        workspace = await db.get(TrajectoryMetaWorkspace, session.workspace_id)
    return _row_metadata(session, trajectory, summary, owner, workspace)


async def _cache_changed_after(db, trajectory_id, head):
    return bool(await db.scalar(select(TrajectoryRecord.record_id).where(
        TrajectoryRecord.trajectory_id == trajectory_id, TrajectoryRecord.start_seq <= head,
        or_(TrajectoryRecord.applied_seq > head, TrajectoryRecord.projector_version != PROJECTOR_VERSION)).limit(1)))


async def _model_at(db, trajectory_id: str, head: int):
    """The model of the request record updated last at head: one indexed query, no record data loaded.

    ``JSONType`` is JSONB on PostgreSQL and JSON text on SQLite, so the path expression is per dialect.
    """
    column = TrajectoryRecord.data
    if db.bind.dialect.name == "postgresql":
        model = func.jsonb_extract_path_text(column, "data", "model")
    else:
        model = func.json_extract(column, "$.data.model")
    return await db.scalar(select(model).where(
        TrajectoryRecord.trajectory_id == trajectory_id, TrajectoryRecord.kind == "request",
        TrajectoryRecord.start_seq <= head, model.is_not(None), model != "")
        .order_by(TrajectoryRecord.applied_seq.desc(), TrajectoryRecord.start_seq.desc(), TrajectoryRecord.record_id.desc())
        .limit(1))


async def _summary_tail(db, trajectory, projected: int, head: int) -> list | None:
    """Hot (seq, type, data) rows of the ruled events (``_ruled``) with projected < seq <= head in seq order;
    None when they reach HEADER_TAIL_LIMIT.

    Archival covers only seq <= projected_seq, so the tail is all hot rows while the projection stays at
    ``projected``; the caller checks that after the read.
    """
    rows = (await db.execute(select(TrajectoryEvent.seq, TrajectoryEvent.type, TrajectoryEvent.data).where(
        TrajectoryEvent.trajectory_id == trajectory.id, TrajectoryEvent.seq > projected, TrajectoryEvent.seq <= head,
        or_(TrajectoryEvent.type == "request.started",
            *(TrajectoryEvent.type.like(f"{family}.%") for family in sorted(SUMMARY_RULE_FAMILIES))))
        .order_by(TrajectoryEvent.seq).limit(HEADER_TAIL_LIMIT))).all()
    return None if len(rows) >= HEADER_TAIL_LIMIT else rows


async def _summarized_header(db, trajectory, summary, head: int, base: dict, *, blob_store=None):
    """(state, statistics, (running_status, model)) of a header from what the projection wrote, never record
    data; None when the caller must replay.

    The record summaries and ``base`` (the summary row's statistics) describe ``projected_seq``. A live probe
    whose head is past it carries the summary's statuses through the tail's ruled events, as a replay would;
    whole-data ``$payload`` references among them are loaded as a replay loads them (``reduction_events``).
    None when the tail reaches HEADER_TAIL_LIMIT or the projection moved during the reads.
    """
    projected = trajectory.projected_seq
    rows = (await db.scalars(select(TrajectoryRecord.summary).where(TrajectoryRecord.trajectory_id == trajectory.id,
        TrajectoryRecord.kind.in_(["agent", "run"]), TrajectoryRecord.start_seq <= projected)
        .order_by(TrajectoryRecord.start_seq, TrajectoryRecord.record_id))).all()
    tail = await _summary_tail(db, trajectory, projected, head) if head > projected else []
    # Checked after the reads, each its own transaction on the read facade: a projection committed in between
    # wrote rows past the summary, and archival, which only follows the projection, may have moved tail rows
    # out of the hot table.
    if tail is None or await _cache_changed_after(db, trajectory.id, projected):
        return None
    if head > projected and await db.scalar(
            select(SessionTrajectory.projected_seq).where(SessionTrajectory.id == trajectory.id)) != projected:
        return None
    datas = await Resolver(db, trajectory.id, through_seq=head, blob_store=blob_store).expand_payloads(
        [row.data for row in tail])
    running_status, model, _ = summary_rules([{"type": row.type, "data": data} for row, data in zip(tail, datas)],
                                             summary.running_status, summary.model)
    state = empty_state()
    state.update(through_seq=str(projected), records={row["record_id"]: row for row in rows},
                 **_hidden(summary, projected))
    metrics = {**base, "duration_ms": statistics(state)["duration_ms"], "through_seq": str(projected),
               "coverage_start": state["coverage_start"]}
    return state, metrics, (running_status, model)


async def _replayed_header(db, trajectory, summary, head: int, *, blob_store=None):
    """(state, statistics, statuses) of a header from the state at head; ``statuses`` is (running_status, model)
    carried from the summary through the replayed events, or None to keep the metadata's."""
    replayed, ruled = [], []

    def observe(row, event, before, after):
        if not replayed:
            replayed.append(row["seq"])
        if _ruled(event["type"]):
            ruled.append(event)
    state = await state_at(db, trajectory, head, blob_store=blob_store, observe=observe) if trajectory else empty_state()
    statuses = None
    base = summary.applied_seq if summary is not None else 0
    if trajectory is not None and head == trajectory.committed_seq and replayed and replayed[0] <= base + 1:
        # Projection lags the head: carry the summary's statuses through the
        # events it has not applied, so they describe the same position as
        # the statistics (a finished run must not read as running).
        running, model = (summary.running_status, summary.model) if summary is not None else ("idle", None)
        statuses = summary_rules([event for event in ruled if int(event["seq"]) > base], running, model)[:2]
    return state, statistics(state), statuses


async def get_session_header(db, session_id: str, through_seq=None, *, blob_store=None):
    """The session header at ``through_seq`` (the head without it).

    A live probe (no ``through_seq``) is the viewer's frequent check and reads
    no record data and replays nothing, even while the projection lags the
    head: statistics and agents as of ``projected_through_seq``, the running
    status and model carried from the summary through the ruled events after
    it (``_summarized_header``). A tail of HEADER_TAIL_LIMIT ruled events, or a
    projection that moves during the reads, is replayed instead. An explicit
    ``through_seq`` is served from the summaries only at the projected position
    and replayed otherwise.
    """
    session, trajectory = await get_trajectory(db, session_id, optional=True)
    summary = await db.get(TrajectorySessionSummary, trajectory.id) if trajectory else None
    header = await _metadata(db, session, trajectory, summary)
    head = watermark(trajectory, through_seq)
    summaries_only = False
    if trajectory is not None and trajectory.content_expired_at is not None:
        # Expired content keeps its summary and statistics; records are gone.
        state = empty_state()
        state.update(through_seq=str(head), **_hidden(summary, head))
        metrics = {**header["statistics"], "through_seq": str(head), "coverage_start": state["coverage_start"]}
    else:
        summarized = None
        if trajectory is not None and summary is not None and summary.applied_seq == trajectory.projected_seq and (
                head == trajectory.projected_seq or through_seq is None):
            summarized = await _summarized_header(db, trajectory, summary, head, header["statistics"], blob_store=blob_store)
        if summarized is not None:
            state, metrics, (header["running_status"], header["model"]) = summarized
            summaries_only = True
        else:
            state, metrics, statuses = await _replayed_header(db, trajectory, summary, head, blob_store=blob_store)
            if statuses is not None:
                header["running_status"], header["model"] = statuses
    if trajectory is not None and head < trajectory.committed_seq:
        rows = sorted(state["records"].values(), key=lambda item: int(item["as_of_seq"]))
        root_runs = [row for row in rows if row["kind"] == "run" and row.get("source_session_id") == session_id]
        status = root_runs[-1]["status"] if root_runs else "idle"
        header["running_status"] = ("running" if status in {"pending", "running", "streaming"} else "waiting" if status == "waiting"
                                    else "error" if status == "failed" else "idle")
        if summaries_only:
            header["model"] = await _model_at(db, trajectory.id, head)
        else:
            requests = [row for row in rows if row["kind"] == "request" and row["data"].get("model")]
            header["model"] = requests[-1]["data"]["model"] if requests else None
    header.update(through_seq=str(head), statistics=metrics, agents=agents(state), projector_version=PROJECTOR_VERSION,
        capabilities={"recording": enabled(session.user_id), "admin_read": True, "export": trajectory is not None, "refs": True},
        unsupported_events=state["unsupported_events"])
    return header


async def list_sessions(db, *, user_id=None, user_query=None, q=None, workspace_id=None, status=None,
                        recording_status=None, activity_from=None, activity_to=None,
                        include_unrecorded=False, cursor=None, limit=50, sort="last_activity_desc"):
    if sort not in {"last_activity_desc", "last_activity_asc"}:
        raise TrajectoryError("Unsupported session sort")
    ascending = sort == "last_activity_asc"
    filters = digest([user_id, user_query, q, workspace_id, status, recording_status,
        iso(activity_from), iso(activity_to), include_unrecorded, sort])
    Session, User, Workspace = TrajectoryMetaSession, TrajectoryMetaUser, TrajectoryMetaWorkspace
    activity = func.coalesce(TrajectorySessionSummary.last_activity_at, Session.updated_at)
    # Replicas of users and workspaces can lag their sessions: outer joins keep
    # such rows listed (owner and workspace names read as null until synced).
    statement = select(Session, SessionTrajectory, TrajectorySessionSummary, User, Workspace)
    statement = statement.outerjoin(User, User.id == Session.user_id).outerjoin(Workspace, Workspace.id == Session.workspace_id)
    statement = statement.outerjoin(SessionTrajectory, and_(SessionTrajectory.session_id == Session.id,
        SessionTrajectory.user_id == Session.user_id, SessionTrajectory.deleted_at.is_(None)))
    statement = statement.outerjoin(TrajectorySessionSummary, TrajectorySessionSummary.trajectory_id == SessionTrajectory.id)
    statement = statement.where(Session.is_deleted.is_(False), Session.deleted_at.is_(None))
    if not include_unrecorded:
        statement = statement.where(SessionTrajectory.id.is_not(None))
    else:
        statement = statement.where(or_(Session.parent_id.is_(None), SessionTrajectory.id.is_not(None)))
    if user_id:
        statement = statement.where(Session.user_id == user_id)
    if user_query:
        needle = f"%{user_query}%"
        statement = statement.where(or_(User.username.ilike(needle), User.email.ilike(needle), Session.user_id.ilike(needle)))
    if q:
        needle = f"%{q}%"
        statement = statement.where(or_(Session.title.ilike(needle), Session.id.ilike(needle)))
    if workspace_id:
        statement = statement.where(Session.workspace_id == workspace_id)
    if status:
        statement = statement.where(func.coalesce(TrajectorySessionSummary.running_status, Session.status) == status)
    if recording_status:
        visible_recording = func.coalesce(SessionTrajectory.recording_status, "not_recorded")
        if not enabled():
            visible_recording = case((SessionTrajectory.id.is_not(None), "paused"), else_=visible_recording)
        elif selected := selected_user_ids("TRAJECTORY_RECORD_USER_IDS"):
            visible_recording = case((and_(SessionTrajectory.id.is_not(None), Session.user_id.not_in(selected)), "paused"), else_=visible_recording)
        statement = statement.where(visible_recording == recording_status)
    if activity_from:
        statement = statement.where(activity >= activity_from)
    if activity_to:
        statement = statement.where(activity <= activity_to)
    if cursor:
        cursor_filters, cursor_time, cursor_session = cursor_decode(cursor)
        if cursor_filters != filters:
            raise TrajectoryError("Session cursor belongs to another filter or sort")
        try:
            timestamp = datetime.fromisoformat(cursor_time.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise TrajectoryError("Invalid session cursor timestamp") from exc
        if not isinstance(cursor_session, str):
            raise TrajectoryError("Invalid session cursor identity")
        statement = statement.where(or_(activity > timestamp, and_(activity == timestamp, Session.id > cursor_session)) if ascending else
            or_(activity < timestamp, and_(activity == timestamp, Session.id < cursor_session)))
    rows = (await db.execute(statement.order_by(activity.asc() if ascending else activity.desc(),
        Session.id.asc() if ascending else Session.id.desc()).limit(limit + 1))).all()
    items = [_row_metadata(session, trajectory, summary, owner, workspace)
             for session, trajectory, summary, owner, workspace in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        session, _, summary, _, _ = rows[limit - 1]
        # Keep database microseconds in cursor; display timestamps use ms.
        exact_time = summary.last_activity_at if summary else session.updated_at
        next_cursor = cursor_encode([filters, exact_time.isoformat(), session.id])
    return {"items": items, "next_cursor": next_cursor, "has_more": len(rows) > limit}


# -- Records --

def _record_cursor(before, head):
    cursor_head, start_seq, record_id = cursor_decode(before)
    if str(cursor_head) != str(head):
        raise TrajectoryError("Record cursor belongs to another watermark")
    start_seq = sequence(start_seq, maximum=head)
    if not isinstance(record_id, str):
        raise TrajectoryError("Invalid record cursor identity")
    return start_seq, record_id


async def list_records(db, trajectory, *, through_seq=None, before=None, limit=100, kind=None, status=None, agent_id=None,
                       blob_store=None):
    head = watermark(trajectory, through_seq)
    require_content(trajectory)
    if head == trajectory.projected_seq:
        query = select(TrajectoryRecord.summary).where(TrajectoryRecord.trajectory_id == trajectory.id, TrajectoryRecord.start_seq <= head)
        for value, field in ((kind, TrajectoryRecord.kind), (status, TrajectoryRecord.status), (agent_id, TrajectoryRecord.agent_id)):
            if value:
                query = query.where(field == value)
        if before:
            start_seq, record_id = _record_cursor(before, head)
            # Rows are ordered by the key column, which differs from the id only for ids wider than it.
            query = query.where(or_(TrajectoryRecord.start_seq < start_seq, and_(TrajectoryRecord.start_seq == start_seq,
                                                                                 TrajectoryRecord.record_id < record_key(record_id))))
        values = (await db.scalars(query.order_by(TrajectoryRecord.start_seq.desc(), TrajectoryRecord.record_id.desc()).limit(limit + 1))).all()
        page = values[:limit]
        next_cursor = cursor_encode([str(head), page[-1]["start_seq"], page[-1]["record_id"]]) if len(values) > limit and page else None
        # Checked after the read: a projection committed in between shows up here.
        if not await _cache_changed_after(db, trajectory.id, head):
            summary = await db.get(TrajectorySessionSummary, trajectory.id)
            return {"items": list(reversed(page)), "next_cursor": next_cursor, "has_more": len(values) > limit,
                    "through_seq": str(head), "projector_version": PROJECTOR_VERSION,
                    "unsupported_events": _hidden(summary, head)["unsupported_events"]}
    state = await state_at(db, trajectory, head, blob_store=blob_store)
    records = list(state["records"].values())
    if kind:
        records = [row for row in records if row["kind"] == kind]
    if status:
        records = [row for row in records if row["status"] == status]
    if agent_id:
        records = [row for row in records if row.get("agent_id") == agent_id]
    if before:
        start_seq, record_id = _record_cursor(before, head)
        records = [row for row in records if (int(row["start_seq"]), row["record_id"]) < (start_seq, record_id)]
    records.sort(key=lambda row: (int(row["start_seq"]), row["record_id"]), reverse=True)
    page = records[:limit]
    next_cursor = cursor_encode([str(head), page[-1]["start_seq"], page[-1]["record_id"]]) if len(records) > limit and page else None
    # Summary rows deliberately omit complete prompt/input/output and blocks.
    summaries = [{key: value for key, value in row.items() if key not in {"data", "blocks"}} for row in reversed(page)]
    return {"items": summaries, "next_cursor": next_cursor, "has_more": len(records) > limit,
            "through_seq": str(head), "projector_version": PROJECTOR_VERSION,
            "unsupported_events": state["unsupported_events"]}


async def _record_events(db, trajectory, record: dict, start: int, end: int, blob_store,
                         replayed: dict[int, dict] | None = None) -> list[dict]:
    """Stored events of a record in start..end: those the projector applied to it
    (trajectory_record_events), for assistant and system records every event
    of the same request_id, and ``replayed`` (stored rows by seq that a replay
    saw change the record: links exist only up to projected_seq). A constant
    number of statements."""
    indexed = select(TrajectoryRecordEvent.seq).where(TrajectoryRecordEvent.trajectory_id == trajectory.id,
        TrajectoryRecordEvent.record_id == record_key(record["record_id"]), TrajectoryRecordEvent.seq >= start,
        TrajectoryRecordEvent.seq <= end)
    matches = [TrajectoryEvent.seq.in_(indexed.scalar_subquery())]
    same_request = record["kind"] in {"assistant", "system"}
    request_id = record.get("request_id")
    if same_request:
        matches.append(TrajectoryEvent.request_id == request_id if request_id is not None else TrajectoryEvent.request_id.is_(None))
    hot = [_stored(row) for row in (await db.scalars(select(TrajectoryEvent).where(TrajectoryEvent.trajectory_id == trajectory.id,
        TrajectoryEvent.seq >= start, TrajectoryEvent.seq <= end, or_(*matches)).order_by(TrajectoryEvent.seq))).all()]
    seen = {row["seq"] for row in hot}
    archived_upper = min(end, hot[0]["seq"] - 1) if hot else end
    segments_exist = start <= archived_upper and await db.scalar(select(TrajectorySegment.from_seq).where(
        TrajectorySegment.trajectory_id == trajectory.id, TrajectorySegment.to_seq >= start,
        TrajectorySegment.from_seq <= archived_upper).limit(1)) is not None
    rows = hot
    if segments_exist:
        seqs = set((await db.scalars(indexed)).all())
        # Without the request rule only linked events match: read just their lines.
        rows = await _archived(db, trajectory.id, start, archived_upper, blob_store, keep=lambda row: row["seq"] not in seen and (
            row["seq"] in seqs or same_request and row.get("request_id") == request_id),
            only=None if same_request else seqs) + hot
    if replayed:
        found = {row["seq"] for row in rows}
        rows = sorted([*rows, *(row for seq, row in replayed.items() if start <= seq <= end and seq not in found)],
                      key=lambda row: row["seq"])
    return rows


async def get_record(db, trajectory, record_id, *, through_seq=None, expand="full", blob_store=None):
    """One record at H with its events. expand="refs" keeps ``$ref`` values
    (in the record and its events) as references; ``$payload`` and ``$media``
    behave as with "full"."""
    if expand not in {"full", "refs"}:
        raise TrajectoryError("Unsupported record expansion")
    head = watermark(trajectory, through_seq)
    require_content(trajectory)
    resolver = Resolver(db, trajectory.id, through_seq=head, blob_store=blob_store)
    record, replayed = None, {}
    if head == trajectory.projected_seq:
        stored = await db.get(TrajectoryRecord, (trajectory.id, record_key(record_id)))
        if stored is None or stored.start_seq > head:
            raise LookupError("Record is not available at this position")
        if stored.applied_seq <= head and stored.projector_version == PROJECTOR_VERSION:
            record = stored.data if expand == "refs" else (await resolver.expand_refs([stored.data]))[0]
    if record is None:
        def observe(row, event, before, after):
            # Links exist only for projected events: a lagging projection has
            # none for the newest ones, so keep what the replay applied here.
            value = after["records"].get(record_id)
            if value is not None and value is not before["records"].get(record_id):
                replayed[row["seq"]] = row
        state = await expanded_state(db, trajectory, head, blob_store=blob_store, resolver=resolver, observe=observe)
        record = state["records"].get(record_id)
        if record is None:
            raise LookupError("Record is not available at this position")
    record = (await resolver.visible([record]))[0]
    events = await _record_events(db, trajectory, record, int(record["start_seq"]), min(head, int(record["as_of_seq"])),
                                  blob_store, replayed)
    values = await expand_all(db, trajectory.id, [row["data"] for row in events], through_seq=head,
                              refs=expand == "full", resolver=resolver)
    events = [{**event_dict(row), "data": value} for row, value in zip(events, values)]
    return {"record": {**record, "as_of_seq": str(head), "events": events}, "through_seq": str(head),
            "projector_version": PROJECTOR_VERSION}


def _like_pattern(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _match_window(text: str, q: str) -> str:
    """The 240 characters of text around the first case-insensitive match of q."""
    index = -1
    for fold in (str.casefold, str.lower):
        folded = fold(text)
        if len(folded) == len(text):
            index = folded.find(fold(q))
            break
    index = max(index, 0)
    return text[max(0, index - 60): index + 180]


async def search(db, trajectory, *, q, through_seq=None, cursor=None, limit=50, blob_store=None):
    head = watermark(trajectory, through_seq)
    require_content(trajectory)
    offset = 0
    if cursor:
        cursor_head, cursor_query, offset = cursor_decode(cursor)
        if str(cursor_head) != str(head) or cursor_query != q:
            raise TrajectoryError("Search cursor belongs to another query or watermark")
        offset = sequence(offset)
    if head == trajectory.projected_seq:
        rows = (await db.execute(select(TrajectoryRecord.record_id, TrajectoryRecord.kind, TrajectoryRecord.applied_seq,
            TrajectoryRecord.search_doc).where(TrajectoryRecord.trajectory_id == trajectory.id, TrajectoryRecord.start_seq <= head,
            TrajectoryRecord.search_doc.ilike(_like_pattern(q), escape="\\"))
            .order_by(TrajectoryRecord.start_seq, TrajectoryRecord.record_id).offset(offset).limit(limit + 1))).all()
        if not await _cache_changed_after(db, trajectory.id, head):
            page = [{"record_id": row.record_id, "seq": str(row.applied_seq), "kind": row.kind,
                     "preview": _match_window(row.search_doc, q)} for row in rows[:limit]]
            has_more = len(rows) > limit
            return {"items": page, "next_cursor": cursor_encode([str(head), q, offset + limit]) if has_more else None,
                    "has_more": has_more, "through_seq": str(head)}
    state = await state_at(db, trajectory, head, blob_store=blob_store)
    needle = q.casefold()
    found = []
    for row in sorted(state["records"].values(), key=lambda row: (int(row["start_seq"]), row["record_id"])):
        content = json.dumps(row, ensure_ascii=False)
        match = content.casefold().find(needle)
        if match >= 0:
            found.append({"record_id": row["record_id"], "seq": row["as_of_seq"], "kind": row["kind"],
                          "preview": content[max(0, match - 60): match + 180]})
    page = found[offset:offset + limit]
    has_more = offset + limit < len(found)
    return {"items": page, "next_cursor": cursor_encode([str(head), q, offset + limit]) if has_more else None,
            "has_more": has_more, "through_seq": str(head)}
