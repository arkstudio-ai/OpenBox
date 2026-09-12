"""Read-only replay at a fixed committed watermark and transactional caches."""
from copy import deepcopy
from datetime import datetime
import base64
import json
from sqlalchemy import and_, case, func, or_, select
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace
from db.models.trajectory import (SessionTrajectory, TrajectoryEvent, TrajectoryRecord,
    TrajectorySessionSummary, TrajectoryCheckpoint, TrajectoryPayload)
from trajectory.config import admin_enabled, enabled, integer, selected_user_ids
from trajectory.payload import expand, store_json
from trajectory.projector import (agents, contribution, empty_state, reduce, statistics, targets)
from trajectory.types import PROJECTOR_VERSION, CorruptContent, TrajectoryError, canonical, digest, iso, now, sequence


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


async def get_trajectory(db, session_id: str, *, optional=False) -> tuple[Session, SessionTrajectory | None]:
    session = await db.get(Session, session_id)
    if session is None or session.is_deleted:
        raise LookupError("Session not found")
    trajectory = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.session_id == session_id,
        SessionTrajectory.user_id == session.user_id, SessionTrajectory.deleted_at.is_(None)))
    if trajectory is None and not optional:
        raise LookupError("Session has not started recording")
    return session, trajectory


def watermark(trajectory, requested=None) -> int:
    head = trajectory.committed_seq if trajectory is not None else 0
    return head if requested is None else sequence(requested, maximum=head)


async def _deleted_payloads(db, trajectory_id):
    return bool(await db.scalar(select(TrajectoryPayload.payload_id).where(
        TrajectoryPayload.trajectory_id == trajectory_id,
        TrajectoryPayload.availability != "available").limit(1)))


async def _cache_changed_after(db, trajectory_id, head):
    return bool(await db.scalar(select(TrajectoryRecord.record_id).where(
        TrajectoryRecord.trajectory_id == trajectory_id, TrajectoryRecord.start_seq <= head,
        or_(TrajectoryRecord.applied_seq > head, TrajectoryRecord.projector_version != PROJECTOR_VERSION)).limit(1)))


async def _load_rows(db, trajectory_id, ids):
    if not ids:
        return {}
    rows = (await db.scalars(select(TrajectoryRecord).where(TrajectoryRecord.trajectory_id == trajectory_id,
        TrajectoryRecord.record_id.in_(ids)))).all()
    return {row.record_id: row for row in rows}


async def project_events_in_tx(db, trajectory, events):
    """Update only touched records; counters use differences, never chunk sums."""
    affected = {record_id for event in events for record_id, _ in targets(event)}
    for event in events:
        if event["type"] in {"message.committed", "part.committed"} and event.get("message_id"):
            related = (await db.scalars(select(TrajectoryRecord.record_id).where(TrajectoryRecord.trajectory_id == trajectory.id,
                TrajectoryRecord.message_id == event["message_id"], TrajectoryRecord.kind == "assistant"))).all()
            affected.update(related)
        if event["type"] == "request.prepared":
            previous_system = await db.scalar(select(TrajectoryRecord).where(TrajectoryRecord.trajectory_id == trajectory.id,
                TrajectoryRecord.kind == "system", TrajectoryRecord.agent_id == event.get("agent_id"))
                .order_by(TrajectoryRecord.start_seq.desc()).limit(1))
            if previous_system is not None:
                affected.add(previous_system.record_id)
        if event["type"] == "request.finished":
            affected.add(f"assistant:{event.get('request_id')}")
        if event["type"] in {"run.interrupted", "recording.gap"}:
            # Interrupted requests/tools receive explicit unknown status in
            # the projection without fabricating their missing completion.
            rows = (await db.scalars(select(TrajectoryRecord).where(TrajectoryRecord.trajectory_id == trajectory.id,
                TrajectoryRecord.kind.in_(["request", "tool", "assistant", "step"]),
                TrajectoryRecord.status.in_(["pending", "running", "streaming", "waiting"])))).all()
            affected.update(row.record_id for row in rows if row.data.get("run_id") == event.get("run_id"))
    existing = await _load_rows(db, trajectory.id, affected)
    state = empty_state()
    state.update(through_seq=str(trajectory.projected_seq), coverage_start=iso(trajectory.started_at),
                 records={record_id: deepcopy(row.data) for record_id, row in existing.items()})
    before = {key: contribution(value) for key, value in state["records"].items()}
    for event in events:
        state = reduce(state, event)
    summary = await db.get(TrajectorySessionSummary, trajectory.id)
    if summary is None:
        summary = TrajectorySessionSummary(trajectory_id=trajectory.id, user_id=trajectory.user_id,
            session_id=trajectory.session_id, workspace_id=trajectory.workspace_id,
            last_activity_at=now(), running_status="idle", recording_status="recording", model=None,
            applied_seq=0, statistics=contribution(None))
        db.add(summary)
    totals = dict(summary.statistics)
    for record_id, data in state["records"].items():
        old = before.get(record_id, contribution(None))
        for key, value in contribution(data).items():
            totals[key] = totals.get(key, 0) + value - old[key]
        row = existing.get(record_id)
        if row is None:
            row = TrajectoryRecord(trajectory_id=trajectory.id, record_id=record_id)
            db.add(row)
        row.kind = data["kind"]
        row.status = data["status"]
        row.agent_id = data.get("agent_id")
        row.message_id = data.get("message_id")
        row.start_seq = int(data["start_seq"])
        row.end_seq = int(data["end_seq"]) if data.get("end_seq") else None
        row.applied_seq = int(data["as_of_seq"])
        row.projector_version = PROJECTOR_VERSION
        row.data = data
        row.summary = {key: value for key, value in data.items() if key not in {"data", "blocks"}}
        row.search_text = json.dumps(data, ensure_ascii=False)
    for event in events:
        family, action = event["type"].split(".")
        if family == "run":
            if action == "started":
                summary.running_status = "running"
            elif action in {"finished", "interrupted"}:
                summary.running_status = "waiting" if event["data"].get("status") == "waiting" else "error" if event["data"].get("status") == "failed" else "idle"
        if family in {"permission", "question"} and action in {"requested", "asked"}:
            summary.running_status = "waiting"
        if event["type"] == "request.started" and event["data"].get("model"):
            summary.model = str(event["data"]["model"])
        if event["type"] == "recording.gap":
            summary.recording_status = trajectory.recording_status = "gap"
    summary.statistics = totals
    summary.applied_seq = trajectory.committed_seq
    summary.last_activity_at = now()
    trajectory.projected_seq = trajectory.committed_seq
    await db.flush()
    # Checkpoints are built by the archive worker, outside the execution
    # transaction. A large history must not delay a provider/tool boundary.


async def create_checkpoint_in_tx(db, trajectory, state=None) -> TrajectoryCheckpoint:
    through_seq = trajectory.projected_seq
    if state is not None:
        through_seq = int(state["through_seq"])
    existing = await db.get(TrajectoryCheckpoint, (trajectory.id, through_seq))
    if existing is not None:
        return existing
    if state is None:
        state = await state_at(db, trajectory, through_seq)
    state_digest = digest(state)
    # Stable pages reuse immutable historical bodies. Appending another run
    # writes only changed/new pages, rather than copying every old long reply.
    ordered = sorted(state["records"].values(), key=lambda item: (int(item["start_seq"]), item["record_id"]))
    pages = []
    for offset in range(0, len(ordered), 100):
        data = {"records": {item["record_id"]: item for item in ordered[offset:offset + 100]}}
        pages.append(await store_json(db, trajectory.id, data, first_seq=through_seq))
    stored = {**state, "records": {}, "record_pages": pages}
    row = TrajectoryCheckpoint(trajectory_id=trajectory.id, through_seq=through_seq,
        projector_version=PROJECTOR_VERSION, state=stored, digest=state_digest, created_at=now())
    db.add(row)
    return row


async def get_checkpoint(db, trajectory, at_seq):
    if await _deleted_payloads(db, trajectory.id):
        return None
    row = await db.scalar(select(TrajectoryCheckpoint).where(TrajectoryCheckpoint.trajectory_id == trajectory.id,
        TrajectoryCheckpoint.through_seq <= at_seq, TrajectoryCheckpoint.projector_version == PROJECTOR_VERSION)
        .order_by(TrajectoryCheckpoint.through_seq.desc()).limit(1))
    if row is None:
        return None
    state = await expand(db, trajectory.id, row.state, through_seq=at_seq)
    if "record_pages" in state:
        from trajectory.payload import expand_pages
        pages = await expand_pages(db, trajectory.id, state.pop("record_pages"), through_seq=at_seq)
        state["records"] = {key: value for page in pages for key, value in page["records"].items()}
    if digest(state) != row.digest:
        raise CorruptContent("Trajectory checkpoint digest mismatch")
    return {"through_seq": str(row.through_seq), "projector_version": row.projector_version,
            "state": state, "digest": row.digest}


async def drain_checkpoints(limit=10):
    from db.base import get_db_session
    interval = integer("TRAJECTORY_CHECKPOINT_INTERVAL", 1000)
    last = select(TrajectoryCheckpoint.trajectory_id, func.max(TrajectoryCheckpoint.through_seq).label("head"))\
        .where(TrajectoryCheckpoint.projector_version == PROJECTOR_VERSION).group_by(TrajectoryCheckpoint.trajectory_id).subquery()
    async with get_db_session() as db:
        candidates = (await db.scalars(select(SessionTrajectory.id).outerjoin(last, last.c.trajectory_id == SessionTrajectory.id)
            .where(SessionTrajectory.deleted_at.is_(None), SessionTrajectory.projected_seq - func.coalesce(last.c.head, 0) >= interval)
            .order_by(SessionTrajectory.updated_at).limit(limit))).all()
    completed = 0
    for trajectory_id in candidates:
        async with get_db_session() as db:
            trajectory = await db.get(SessionTrajectory, trajectory_id)
            if trajectory is None or trajectory.deleted_at is not None:
                continue
            state = await state_at(db, trajectory, trajectory.projected_seq)
        async with get_db_session() as db:
            trajectory = await db.get(SessionTrajectory, trajectory_id)
            if trajectory is None or trajectory.deleted_at is not None:
                continue
            await create_checkpoint_in_tx(db, trajectory, state)
        completed += 1
    return completed


async def read_events(db, trajectory, *, after_seq=0, until_seq=None, limit=500, include_data=True):
    until = watermark(trajectory, until_seq)
    after = sequence(after_seq, maximum=until)
    from trajectory.recorder import event_dict
    rows = (await db.scalars(select(TrajectoryEvent).where(TrajectoryEvent.trajectory_id == trajectory.id,
        TrajectoryEvent.seq > after, TrajectoryEvent.seq <= until).order_by(TrajectoryEvent.seq).limit(limit + 1))).all()
    has_more = len(rows) > limit
    events = []
    expected = after + 1
    for row in rows[:limit]:
        if row.seq != expected:
            raise CorruptContent(f"Trajectory sequence gap before {row.seq}")
        expected += 1
        value = event_dict(row)
        if include_data:
            value["data"] = await expand(db, trajectory.id, value["data"], through_seq=until)
        events.append(value)
    if not has_more and expected - 1 != until:
        raise CorruptContent("Committed trajectory tail is missing")
    return {"events": events, "from_seq": str(after + 1) if events else str(after),
            "through_seq": events[-1]["seq"] if events else str(after), "until_seq": str(until),
            "has_more": has_more, "committed_seq": str(trajectory.committed_seq)}


async def state_at(db, trajectory, through_seq=None):
    through = watermark(trajectory, through_seq)
    if through == trajectory.projected_seq and not await _deleted_payloads(db, trajectory.id):
        rows = (await db.scalars(select(TrajectoryRecord).where(TrajectoryRecord.trajectory_id == trajectory.id)
            .order_by(TrajectoryRecord.start_seq, TrajectoryRecord.record_id))).all()
        relevant = [row for row in rows if row.start_seq <= through]
        if all(row.applied_seq <= through and row.projector_version == PROJECTOR_VERSION for row in relevant):
            state = empty_state()
            state.update(through_seq=str(through), coverage_start=iso(trajectory.started_at), records={row.record_id: deepcopy(row.data) for row in relevant})
            return state
    checkpoint = await get_checkpoint(db, trajectory, through)
    state = deepcopy(checkpoint["state"]) if checkpoint else empty_state()
    while int(state["through_seq"]) < through:
        page = await read_events(db, trajectory, after_seq=state["through_seq"], until_seq=through, limit=1000)
        for event in page["events"]:
            state = reduce(state, event)
    return state


async def _metadata(db, session, trajectory, summary=None, owner=None, workspace=None):
    owner = owner if owner is not None else await db.get(User, session.user_id)
    workspace = workspace if workspace is not None else await db.get(Workspace, session.workspace_id)
    stats = dict(summary.statistics) if summary else contribution(None)
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


async def get_session_header(db, session_id: str, through_seq=None):
    session, trajectory = await get_trajectory(db, session_id, optional=True)
    summary = await db.get(TrajectorySessionSummary, trajectory.id) if trajectory else None
    header = await _metadata(db, session, trajectory, summary)
    head = watermark(trajectory, through_seq)
    latest = trajectory is not None and summary is not None and summary.applied_seq == head and head == trajectory.projected_seq and not await _deleted_payloads(db, trajectory.id)
    if latest:
        # The header is a frequent watermark probe, not a full replay load.
        rows = (await db.scalars(select(TrajectoryRecord.data).where(TrajectoryRecord.trajectory_id == trajectory.id,
            TrajectoryRecord.kind.in_(["agent", "run"]), TrajectoryRecord.start_seq <= head))).all()
        state = empty_state()
        state.update(through_seq=str(head), coverage_start=iso(trajectory.started_at), records={row["record_id"]: row for row in rows})
        metrics = {**header["statistics"], "duration_ms": statistics(state)["duration_ms"], "through_seq": str(head)}
        if await _cache_changed_after(db, trajectory.id, head):
            state = await state_at(db, trajectory, head)
            metrics = statistics(state)
    else:
        state = await state_at(db, trajectory, head) if trajectory else empty_state()
        metrics = statistics(state)
    if trajectory is not None and head < trajectory.committed_seq:
        rows = sorted(state['records'].values(), key=lambda item: int(item['as_of_seq']))
        root_runs = [row for row in rows if row['kind']=='run' and row.get('source_session_id')==session_id]
        status = root_runs[-1]['status'] if root_runs else 'idle'
        header['running_status'] = 'running' if status in {'pending','running','streaming'} else 'waiting' if status=='waiting' else 'error' if status=='failed' else 'idle'
        requests = [row for row in rows if row['kind']=='request' and row['data'].get('model')]
        header['model'] = requests[-1]['data']['model'] if requests else None
    header.update(through_seq=str(head), statistics=metrics, agents=agents(state),
        projector_version=PROJECTOR_VERSION, capabilities={"recording": enabled(session.user_id), "admin_read": True, "export": trajectory is not None},
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
    activity = func.coalesce(TrajectorySessionSummary.last_activity_at, Session.updated_at)
    statement = select(Session, SessionTrajectory, TrajectorySessionSummary, User, Workspace).join(User, Session.user_id == User.id)
    statement = statement.join(Workspace, Workspace.id == Session.workspace_id)
    statement = statement.outerjoin(SessionTrajectory, and_(SessionTrajectory.session_id == Session.id,
        SessionTrajectory.user_id == Session.user_id, SessionTrajectory.deleted_at.is_(None)))
    statement = statement.outerjoin(TrajectorySessionSummary, TrajectorySessionSummary.trajectory_id == SessionTrajectory.id)
    statement = statement.where(Session.is_deleted.is_(False))
    if not include_unrecorded:
        statement = statement.where(SessionTrajectory.id.is_not(None))
    else:
        statement = statement.where(or_(Session.parent_id.is_(None), SessionTrajectory.id.is_not(None)))
    if user_id:
        statement = statement.where(Session.user_id == user_id)
    if user_query:
        needle = f"%{user_query}%"
        statement = statement.where(or_(User.username.ilike(needle), User.email.ilike(needle), User.id.ilike(needle)))
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
    items = [await _metadata(db, session, trajectory, summary, owner, workspace) for session, trajectory, summary, owner, workspace in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        session, _, summary, _, _ = rows[limit - 1]
        # Keep database microseconds in cursor; display timestamps use ms.
        exact_time = summary.last_activity_at if summary else session.updated_at
        next_cursor = cursor_encode([filters, exact_time.isoformat(), session.id])
    return {"items": items, "next_cursor": next_cursor, "has_more": len(rows) > limit}


async def list_records(db, trajectory, *, through_seq=None, before=None, limit=100, kind=None, status=None, agent_id=None):
    head = watermark(trajectory, through_seq)
    if head == trajectory.projected_seq and not await _deleted_payloads(db, trajectory.id):
        query = select(TrajectoryRecord.summary).where(TrajectoryRecord.trajectory_id == trajectory.id, TrajectoryRecord.start_seq <= head)
        for value, field in ((kind, TrajectoryRecord.kind), (status, TrajectoryRecord.status), (agent_id, TrajectoryRecord.agent_id)):
            if value:
                query = query.where(field == value)
        if before:
            cursor_head, start_seq, record_id = cursor_decode(before)
            if str(cursor_head) != str(head):
                raise TrajectoryError("Record cursor belongs to another watermark")
            start_seq = sequence(start_seq, maximum=head)
            if not isinstance(record_id, str):
                raise TrajectoryError("Invalid record cursor identity")
            query = query.where(or_(TrajectoryRecord.start_seq < start_seq, and_(TrajectoryRecord.start_seq == start_seq, TrajectoryRecord.record_id < record_id)))
        values = (await db.scalars(query.order_by(TrajectoryRecord.start_seq.desc(), TrajectoryRecord.record_id.desc()).limit(limit + 1))).all()
        page = values[:limit]
        next_cursor = cursor_encode([str(head), page[-1]["start_seq"], page[-1]["record_id"]]) if len(values) > limit and page else None
        if not await _cache_changed_after(db, trajectory.id, head):
            return {"items": list(reversed(page)), "next_cursor": next_cursor, "has_more": len(values) > limit,
                    "through_seq": str(head), "projector_version": PROJECTOR_VERSION, "unsupported_events": []}
    state = await state_at(db, trajectory, head)
    records = list(state["records"].values())
    if kind:
        records = [row for row in records if row["kind"] == kind]
    if status:
        records = [row for row in records if row["status"] == status]
    if agent_id:
        records = [row for row in records if row.get("agent_id") == agent_id]
    if before:
        cursor_head, start_seq, record_id = cursor_decode(before)
        if str(cursor_head) != str(head):
            raise TrajectoryError("Record cursor belongs to another watermark")
        start_seq = sequence(start_seq, maximum=head)
        if not isinstance(record_id, str):
            raise TrajectoryError("Invalid record cursor identity")
        records = [row for row in records if (int(row["start_seq"]), row["record_id"]) < (start_seq, record_id)]
    records.sort(key=lambda row: (int(row["start_seq"]), row["record_id"]), reverse=True)
    page = records[:limit]
    next_cursor = cursor_encode([str(head), page[-1]["start_seq"], page[-1]["record_id"]]) if len(records) > limit and page else None
    # Summary rows deliberately omit complete prompt/input/output and blocks.
    summaries = [{key: value for key, value in row.items() if key not in {"data", "blocks"}} for row in reversed(page)]
    return {"items": summaries, "next_cursor": next_cursor, "has_more": len(records) > limit,
            "through_seq": str(head), "projector_version": PROJECTOR_VERSION,
            "unsupported_events": state["unsupported_events"]}


async def get_record(db, trajectory, record_id, *, through_seq=None):
    head = watermark(trajectory, through_seq)
    state = await state_at(db, trajectory, head)
    row = state["records"].get(record_id)
    if row is None:
        raise LookupError("Record is not available at this position")
    events = []
    after = int(row["start_seq"]) - 1
    end = min(head, int(row["as_of_seq"]))
    while after < end:
        page = await read_events(db, trajectory, after_seq=after, until_seq=end, limit=1000)
        for event in page["events"]:
            ids = {key for key, _ in targets(event, state)} if event["version"] == 1 else set()
            if record_id in ids or row["kind"] in {"assistant", "system"} and event.get("request_id") == row.get("request_id"):
                events.append(event)
        after = int(page["through_seq"])
    return {"record": {**row, "as_of_seq": str(head), "events": events}, "through_seq": str(head), "projector_version": PROJECTOR_VERSION}


async def search(db, trajectory, *, q, through_seq=None, cursor=None, limit=50):
    head = watermark(trajectory, through_seq)
    state = await state_at(db, trajectory, head)
    offset = 0
    if cursor:
        cursor_head, cursor_query, offset = cursor_decode(cursor)
        if str(cursor_head) != str(head) or cursor_query != q:
            raise TrajectoryError("Search cursor belongs to another query or watermark")
        offset = sequence(offset)
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
