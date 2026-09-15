"""Trace database reads (SPEC 8.9): sessions over metadata replicas, headers and record lists."""
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import delete, event, insert, update

from tests.unit.test_worker_projection_support import (AT, REFERENCES, Ingest, Metrics, add_meta, add_payload,  # noqa: F401
    add_trajectory, archive, blobs, project_all, settings, trace_db)
from trajectory.payload import is_ref
from trajectory.projector import agents, empty_state, replay, statistics
from trajectory.repository import (cursor_decode, cursor_encode, get_record, get_session_header, get_trajectory,
    list_records, list_sessions, read_events, record_key, reset_segment_cache, search, segment_cache, state_at)
from trajectory.segments import encode_segment
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryEvent, TrajectoryRecord, TrajectorySegment
from trajectory.types import CorruptContent, TrajectoryError, iso
from trajectory.worker.projection import ProjectionService, search_doc

ROW_KEYS = {"session_id", "user_id", "trajectory_id", "title", "owner", "workspace", "workspace_id", "running_status",
            "recording_status", "coverage_start", "last_activity_at", "model", "agent", "committed_seq",
            "projected_through_seq", "through_seq", "statistics"}


def _event(trajectory_id, session_id, user_id, seq, kind, data=None, *, at=None, **ids):
    return {"event_id": f"evt_{trajectory_id}_{seq}", "trajectory_id": trajectory_id, "user_id": user_id,
            "session_id": session_id, "source_session_id": ids.pop("source_session_id", session_id), "seq": str(seq),
            "type": kind, "version": ids.pop("version", 1), "occurred_at": iso(at or AT + timedelta(seconds=seq)),
            "data": data if data is not None else {}, **ids}


def conversation(trajectory_id, session_id, user_id, *, start=AT, extra=()):
    run = {"run_id": f"run_{trajectory_id}", "turn_id": "turn_1", "agent_id": "root"}
    helper = {**run, "agent_id": "helper", "parent_agent_id": "root"}
    rows = [
        ("run.started", {}, run),
        ("request.started", {"model": "model-x"}, {"request_id": "req_1", **run}),
        ("request.delta", {"chunk_index": 0, "delta": "Hello "}, {"request_id": "req_1", **run}),
        ("request.delta", {"chunk_index": 1, "delta": "world"}, {"request_id": "req_1", **run}),
        ("request.usage", {"usage": {"input_tokens": 11, "output_tokens": 4}}, {"request_id": "req_1", **run}),
        ("request.finished", {"status": "completed"}, {"request_id": "req_1", **run}),
        ("tool.requested", {"name": "grep", "requested_arguments": {"pattern": "50%_done"}},
         {"call_id": "call_1", "request_id": "req_1", **run}),
        ("tool.started", {}, {"call_id": "call_1", **run}),
        ("tool.finished", {"status": "failed", "error": "exit 2", "output": "no match"}, {"call_id": "call_1", **run}),
        ("agent.spawned", {"name": "helper", "prompt": "Look around"}, helper),
        ("agent.finished", {"status": "completed", "output": "done"}, helper),
        ("run.finished", {"status": "completed"}, run),
        *extra,
    ]
    return [_event(trajectory_id, session_id, user_id, index, kind, data, at=start + timedelta(seconds=index), **ids)
            for index, (kind, data, ids) in enumerate(rows, start=1)]


async def recorded(blobs, trajectory_id, session_id, user_id="user_a", workspace_id="ws_a", *, start=AT, extra=(),
                   project=True, **meta):
    await add_meta(session_id, user_id, workspace_id, **meta)
    await add_trajectory(trajectory_id, session_id, user_id, workspace_id)
    events = conversation(trajectory_id, session_id, user_id, start=start, extra=extra)
    await Ingest(blobs).append(trajectory_id, events)
    if project:
        await project_all(ProjectionService(settings(), blob_store=blobs, metrics=Metrics()), trajectory_id)
    return events


@contextmanager
def statements(engine):
    seen = []

    def listener(conn, cursor, statement, *args):
        seen.append(statement)
    event.listen(engine.sync_engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", listener)


def summaries(state):
    rows = [{key: value for key, value in record.items() if key not in {"data", "blocks"}} for record in state["records"].values()]
    return sorted(rows, key=lambda row: (int(row["start_seq"]), row["record_id"]))


async def test_get_trajectory_reads_the_session_replica_and_tolerates_its_lag(trace_db, blobs):
    await recorded(blobs, "trj_1", "s1")
    await add_meta("s_deleted", is_deleted=True)
    await add_trajectory("trj_deleted", "s_deleted")
    await add_trajectory("trj_lag", "s_lag", "user_lag", "ws_lag")
    await add_meta("s_new")
    await add_meta("s_tomb")
    await add_trajectory("trj_tomb", "s_tomb")
    await add_meta("s_foreign", "user_b")
    await add_trajectory("trj_foreign", "s_foreign", "user_a")
    async with trace_session() as db:
        await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == "trj_tomb").values(deleted_at=AT))
    async with trace_session() as db:
        session, trajectory = await get_trajectory(db, "s1")
        assert (session.id, session.title, trajectory.id) == ("s1", None, "trj_1")
        session, trajectory = await get_trajectory(db, "s_lag")
        assert (session.id, session.user_id, session.workspace_id, trajectory.id) == ("s_lag", "user_lag", "ws_lag", "trj_lag")
        assert (await get_trajectory(db, "s_new", optional=True))[1] is None
        for session_id, message in (("s_deleted", "not found"), ("s_missing", "not found"), ("s_new", "not started"),
                                    ("s_tomb", "not started"), ("s_foreign", "not started")):
            with pytest.raises(LookupError, match=message):
                await get_trajectory(db, session_id)


async def test_list_sessions_filters_sorts_pages_and_includes_unrecorded_roots(trace_db, blobs, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    monkeypatch.delenv("TRAJECTORY_RECORD_USER_IDS", raising=False)
    await recorded(blobs, "trj_1", "s1", "user_a", "ws_a", start=AT + timedelta(hours=1), title="Alpha report", username="alice")
    await recorded(blobs, "trj_2", "s2", "user_b", "ws_b", start=AT + timedelta(hours=2), title="Beta", username="bob")
    await recorded(blobs, "trj_6", "s6", "user_c", "ws_c", start=AT + timedelta(minutes=30), title="Gamma", user=False, workspace=False)
    shared = {"user": False, "workspace": False}
    await recorded(blobs, "trj_5", "s5", "user_a", "ws_a", is_deleted=True, **shared)
    await add_meta("s3", "user_a", "ws_a", title="Unrecorded root", status="running", updated_at=AT + timedelta(hours=3), **shared)
    await add_meta("s4", "user_a", "ws_a", title="Unrecorded child", parent_id="s3", updated_at=AT + timedelta(hours=4), **shared)

    async def ids(**filters):
        return [item["session_id"] for item in (await list_sessions(db, **filters))["items"]]
    async with trace_session() as db:
        page = await list_sessions(db)
        assert [item["session_id"] for item in page["items"]] == ["s2", "s1", "s6"] and page["has_more"] is False
        alpha, gamma = page["items"][1], page["items"][2]
        assert set(alpha) == ROW_KEYS and alpha["owner"] == {"user_id": "user_a", "username": "alice", "email": "user_a@example.invalid"}
        assert alpha["workspace"] == {"id": "ws_a", "name": "workspace-ws_a"} and alpha["title"] == "Alpha report"
        assert (alpha["recording_status"], alpha["running_status"], alpha["model"]) == ("recording", "idle", "model-x")
        assert alpha["last_activity_at"] == iso(AT + timedelta(hours=1, seconds=12)) and alpha["committed_seq"] == "12"
        assert alpha["statistics"] == {"request_count": 1, "tool_count": 1, "error_count": 1, "unknown_count": 0,
                                       "input_tokens": 11, "output_tokens": 4, "usage_complete": True, "duration_ms": None,
                                       "through_seq": "12", "coverage_start": iso(AT)}
        assert gamma["owner"]["username"] is None and gamma["workspace"] == {"id": "ws_c", "name": None}
        assert await ids(sort="last_activity_asc") == ["s6", "s1", "s2"]
        unrecorded = await list_sessions(db, include_unrecorded=True)
        assert [item["session_id"] for item in unrecorded["items"]] == ["s3", "s2", "s1", "s6"]
        assert (unrecorded["items"][0]["trajectory_id"], unrecorded["items"][0]["recording_status"],
                unrecorded["items"][0]["running_status"]) == (None, "not_recorded", "running")
        assert await ids(user_id="user_a") == ["s1"] and await ids(user_query="ALIC") == ["s1"]
        assert await ids(q="beta") == ["s2"] and await ids(q="s6") == ["s6"] and await ids(workspace_id="ws_b") == ["s2"]
        assert await ids(status="running", include_unrecorded=True) == ["s3"] and await ids(status="idle") == ["s2", "s1", "s6"]
        assert await ids(activity_from=AT + timedelta(hours=1), activity_to=AT + timedelta(hours=2, minutes=1)) == ["s2", "s1"]
        assert await ids(recording_status="recording") == ["s2", "s1", "s6"]
        monkeypatch.setenv("TRAJECTORY_RECORD_USER_IDS", "user_b")
        assert await ids(recording_status="paused") == ["s1", "s6"]
        monkeypatch.delenv("TRAJECTORY_RECORDING_ENABLED")
        assert await ids(recording_status="paused") == ["s2", "s1", "s6"]
        assert (await list_sessions(db))["items"][0]["recording_status"] == "paused"
        first = await list_sessions(db, limit=2)
        assert [item["session_id"] for item in first["items"]] == ["s2", "s1"] and first["has_more"]
        second = await list_sessions(db, limit=2, cursor=first["next_cursor"])
        assert [item["session_id"] for item in second["items"]] == ["s6"] and not second["has_more"] and second["next_cursor"] is None
        filters = cursor_decode(first["next_cursor"])[0]
        for cursor, extra in ((first["next_cursor"], {"q": "a"}), ("!!!", {}), (cursor_encode([filters, "not a time", "s1"]), {}),
                              (cursor_encode([filters, iso(AT), 7]), {})):
            with pytest.raises(TrajectoryError):
                await list_sessions(db, limit=2, cursor=cursor, **extra)
        with pytest.raises(TrajectoryError):
            await list_sessions(db, sort="title")


async def test_header_at_head_reads_summaries_and_matches_the_replayed_state(trace_db, blobs, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    events = await recorded(blobs, "trj_1", "s1", title="Alpha", agent="build")
    async with trace_session() as db:
        with statements(trace_db) as seen:
            header = await get_session_header(db, "s1")
    assert not any("trajectory_records.data" in statement for statement in seen)
    state = replay(events)
    assert header["statistics"] == statistics(state) and header["agents"] == agents(state) != []
    assert header["capabilities"] == {"recording": True, "admin_read": True, "export": True, "refs": True}
    assert (header["through_seq"], header["committed_seq"], header["projected_through_seq"]) == ("12", "12", "12")
    assert (header["model"], header["agent"], header["title"], header["unsupported_events"]) == ("model-x", "build", "Alpha", [])
    run = {"run_id": "run_2", "turn_id": "turn_2", "agent_id": "root"}
    more = [_event("trj_1", "s1", "user_a", 13, "run.started", {}, at=AT + timedelta(minutes=5), **run),
            _event("trj_1", "s1", "user_a", 14, "request.started", {"model": "model-y"}, at=AT + timedelta(minutes=6),
                   request_id="req_2", **run)]
    await Ingest(blobs).append("trj_1", more)
    async with trace_session() as db:
        # Projection lags: a live probe keeps the projection's statistics; earlier positions recompute status and model.
        live = await get_session_header(db, "s1")
        assert (live["through_seq"], live["statistics"]) == ("14", statistics(state))
        assert (live["running_status"], live["model"]) == ("running", "model-y")
        at_projection = await get_session_header(db, "s1", "12")
        assert (at_projection["running_status"], at_projection["model"]) == ("idle", "model-x")
        assert at_projection["statistics"] == statistics(state)
        historical = await get_session_header(db, "s1", "13")
        assert (historical["running_status"], historical["model"]) == ("running", "model-x")
        assert historical["statistics"] == statistics(replay(events + more[:1]))
        with pytest.raises(TrajectoryError):
            await get_session_header(db, "s1", "15")
    await add_meta("s_new")
    async with trace_session() as db:
        fresh = await get_session_header(db, "s_new")
    assert (fresh["trajectory_id"], fresh["recording_status"], fresh["through_seq"], fresh["agents"]) == (None, "not_recorded", "0", [])
    assert fresh["capabilities"]["export"] is False and fresh["statistics"] == statistics(empty_state())


async def test_header_at_the_projected_position_reads_through_the_read_facade(trace_db, blobs, monkeypatch):
    """The read facade (routes) offers unlocked SELECTs only: the model of a header behind the committed head
    comes from one query over the request records, not from a streamed scan."""
    from trajectory.store.database import trace_read_session
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    events = await recorded(blobs, "trj_1", "s1")
    run = {"run_id": "run_2", "turn_id": "turn_2", "agent_id": "root"}
    await Ingest(blobs).append("trj_1", [
        _event("trj_1", "s1", "user_a", 13, "run.started", {}, at=AT + timedelta(minutes=5), **run),
        _event("trj_1", "s1", "user_a", 14, "request.started", {"model": "model-y"}, at=AT + timedelta(minutes=6),
               request_id="req_2", **run)])
    async with trace_read_session() as reader:
        with statements(trace_db) as seen:
            at_projection = await get_session_header(reader, "s1", "12")
        live = await get_session_header(reader, "s1")
    assert (at_projection["through_seq"], at_projection["running_status"], at_projection["model"]) == ("12", "idle", "model-x")
    assert at_projection["statistics"] == statistics(replay(events))
    assert not any("trajectory_records.data" in statement and "LIMIT" not in statement.upper()
                   for statement in seen if "trajectory_records" in statement)
    assert live["through_seq"] == "14"


async def test_live_header_statuses_include_events_the_projection_has_not_applied(trace_db, blobs, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    events = await recorded(blobs, "trj_1", "s1")
    run = {"run_id": "run_2", "turn_id": "turn_2", "agent_id": "root"}
    more = [_event("trj_1", "s1", "user_a", 13, "run.started", {}, **run),
            _event("trj_1", "s1", "user_a", 14, "request.started", {"model": "model-y"}, request_id="req_2", **run),
            _event("trj_1", "s1", "user_a", 15, "permission.requested", {"permission_id": "perm_1"}, call_id="call_2", **run)]
    await Ingest(blobs).append("trj_1", more)
    await add_meta("s2", status="meta-status", model="meta-model")
    await add_trajectory("trj_2", "s2")
    await Ingest(blobs).append("trj_2", conversation("trj_2", "s2", "user_a")[:2])
    async with trace_session() as db:
        with statements(trace_db) as seen:
            lagging = await get_session_header(db, "s1")
        unprojected = await get_session_header(db, "s2")
    # A live probe: the statuses describe the head, carried through the tail the projection has not applied;
    # statistics and agents describe the projection, read from its summaries without record data or a replay.
    assert (lagging["through_seq"], lagging["projected_through_seq"]) == ("15", "12")
    assert (lagging["running_status"], lagging["model"]) == ("waiting", "model-y")
    assert lagging["statistics"] == statistics(replay(events)) and lagging["agents"] == agents(replay(events)) != []
    assert lagging["statistics"]["through_seq"] == "12"
    assert not any("trajectory_records.data" in statement or "trajectory_checkpoints" in statement for statement in seen)
    assert sum("FROM trajectory_events" in statement for statement in seen) == 1
    assert (unprojected["running_status"], unprojected["model"], unprojected["projected_through_seq"]) == ("running", "model-x", "0")
    await project_all(ProjectionService(settings(), blob_store=blobs, metrics=Metrics()), "trj_1")
    async with trace_session() as db:
        projected = await get_session_header(db, "s1")
    keys = ("running_status", "model", "through_seq")
    assert {key: projected[key] for key in keys} == {key: lagging[key] for key in keys}
    assert projected["statistics"] == statistics(replay(events + more))


async def test_a_live_header_replays_when_its_tail_reaches_the_limit_or_the_projection_moves_meanwhile(
        trace_db, blobs, monkeypatch):
    import trajectory.repository as repository
    events = await recorded(blobs, "trj_1", "s1")
    run = {"run_id": "run_2", "turn_id": "turn_2", "agent_id": "root"}
    more = [_event("trj_1", "s1", "user_a", 13, "run.started", {}, **run),
            _event("trj_1", "s1", "user_a", 14, "request.started", {"model": "model-y"}, request_id="req_2", **run),
            _event("trj_1", "s1", "user_a", 15, "permission.requested", {"permission_id": "perm_1"}, call_id="call_2", **run)]
    await Ingest(blobs).append("trj_1", more)
    replayed = ("waiting", "model-y", statistics(replay(events + more)))
    monkeypatch.setattr(repository, "HEADER_TAIL_LIMIT", 3)
    async with trace_session() as db:
        with statements(trace_db) as seen:
            header = await get_session_header(db, "s1")
    assert (header["running_status"], header["model"], header["statistics"]) == replayed
    assert any("trajectory_checkpoints" in statement for statement in seen)
    # A projection that commits between the reads may be followed by archival, which moves tail rows out of the
    # hot table: the tail read before it cannot be trusted.
    monkeypatch.setattr(repository, "HEADER_TAIL_LIMIT", 4)
    changed_after = repository._cache_changed_after

    async def projected_meanwhile(db, trajectory_id, head):
        await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == trajectory_id).values(projected_seq=13))
        return await changed_after(db, trajectory_id, head)

    monkeypatch.setattr(repository, "_cache_changed_after", projected_meanwhile)
    async with trace_session() as db:
        with statements(trace_db) as seen:
            header = await get_session_header(db, "s1")
    assert (header["running_status"], header["model"], header["statistics"]) == replayed
    assert any("trajectory_checkpoints" in statement for statement in seen)


async def test_expired_content_keeps_its_summary_but_refuses_record_reads(trace_db, blobs):
    await recorded(blobs, "trj_1", "s1")
    async with trace_session() as db:
        await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == "trj_1").values(
            content_expired_at=AT, recording_status="expired"))
        await db.execute(delete(TrajectoryRecord))
    async with trace_session() as db:
        header = await get_session_header(db, "s1")
        assert header["statistics"]["request_count"] == 1 and header["agents"] == [] and header["recording_status"] == "paused"
        trajectory = (await get_trajectory(db, "s1"))[1]
        with pytest.raises(FileNotFoundError, match="expired"):
            await list_records(db, trajectory)
        with pytest.raises(FileNotFoundError, match="expired"):
            await state_at(db, trajectory)


async def test_list_records_serves_summaries_at_head_and_replays_other_positions(trace_db, blobs):
    events = await recorded(blobs, "trj_1", "s1", extra=[("session.renamed", {"title": "x"}, {})])
    state = replay(events)
    expected = summaries(state)
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        with statements(trace_db) as seen:
            page = await list_records(db, trajectory)
        assert not any("trajectory_records.data" in statement for statement in seen)
        assert page["items"] == expected and page["through_seq"] == "13" and page["projector_version"] == 1
        assert page["unsupported_events"] == state["unsupported_events"] == [{"seq": "13", "type": "session.renamed", "version": 1}]
        first = await list_records(db, trajectory, limit=3)
        assert first["items"] == expected[-3:] and first["has_more"]
        second = await list_records(db, trajectory, limit=3, before=first["next_cursor"])
        assert second["items"] == expected[-6:-3]
        for filters, keep in (({"kind": "tool"}, lambda row: row["kind"] == "tool"),
                              ({"status": "failed"}, lambda row: row["status"] == "failed"),
                              ({"agent_id": "helper"}, lambda row: row["agent_id"] == "helper")):
            assert (await list_records(db, trajectory, **filters))["items"] == [row for row in expected if keep(row)] != []
        historical = await list_records(db, trajectory, through_seq="6")
        assert historical["items"] == summaries(replay(events[:6])) and historical["through_seq"] == "6"
        for arguments in ({"through_seq": "6", "before": first["next_cursor"]}, {"before": "garbage"}):
            with pytest.raises(TrajectoryError):
                await list_records(db, trajectory, **arguments)
        # A row written past the position is never served from the cache.
        await db.execute(update(TrajectoryRecord).where(TrajectoryRecord.record_id == "run:run_trj_1").values(applied_seq=99))
        assert (await list_records(db, trajectory))["items"] == expected


def _seqs(detail):
    return [event["seq"] for event in detail["record"]["events"]]


async def test_record_detail_at_head_reads_one_row_and_its_indexed_events(trace_db, blobs):
    events = await recorded(blobs, "trj_1", "s1")
    state = replay(events)
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        page = {event["seq"]: event for event in (await read_events(db, trajectory))["events"]}
        assistant = await get_record(db, trajectory, "assistant:req_1")
        # Events the projector applied, plus the request's other events inside the record's range.
        assert assistant["record"] == {**state["records"]["assistant:req_1"], "as_of_seq": "12",
                                       "events": [page[seq] for seq in ("3", "4", "5", "6")]}
        assert (assistant["through_seq"], assistant["projector_version"]) == ("12", 1)
        assert _seqs(await get_record(db, trajectory, "tool:call_1")) == ["7", "8", "9"]
        assert _seqs(await get_record(db, trajectory, "run:run_trj_1")) == ["1", "12"]
        historical = await get_record(db, trajectory, "assistant:req_1", through_seq="4")
        assert historical["record"] == {**replay(events[:4])["records"]["assistant:req_1"], "as_of_seq": "4",
                                        "events": [page["3"], page["4"]]}
        for record_id, through in (("tool:call_1", "6"), ("tool:missing", None)):
            with pytest.raises(LookupError, match="not available"):
                await get_record(db, trajectory, record_id, through_seq=through)
        with pytest.raises(TrajectoryError):
            await get_record(db, trajectory, "tool:call_1", expand="everything")


async def test_record_detail_expand_refs_keeps_value_references_in_the_record_and_its_events(trace_db, blobs):
    await add_meta("s1")
    await add_trajectory("trj_1", "s1")
    run = {"run_id": "run_1", "request_id": "req_1"}
    events = [_event("trj_1", "s1", "user_a", 1, "request.prepared",
                     {"input": {"system": "S" * 40, "messages": [{"role": "user", "content": "M" * 40}]}}, **run),
              _event("trj_1", "s1", "user_a", 2, "request.started", {"model": "model-x"}, **run)]
    await Ingest(blobs, system_bytes=16, message_bytes=16).append("trj_1", events)
    await project_all(ProjectionService(settings(), blob_store=blobs, metrics=Metrics()), "trj_1")
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        full = await get_record(db, trajectory, "request:req_1")
        kept = await get_record(db, trajectory, "request:req_1", expand="refs")
        historical = await get_record(db, trajectory, "request:req_1", through_seq="1")
    assert full["record"] == {**replay(events)["records"]["request:req_1"], "as_of_seq": "2", "events": full["record"]["events"]}
    assert [event["data"] for event in full["record"]["events"]] == [event["data"] for event in events]
    [message] = kept["record"]["data"]["input"]["messages"]
    assert is_ref(message) and message["$ref"]["kind"] == "message"
    assert is_ref(kept["record"]["events"][0]["data"]["input"]["system"])
    assert historical["record"]["data"]["input"] == events[0]["data"]["input"] and _seqs(historical) == ["1"]


async def test_record_detail_statements_do_not_grow_with_the_number_of_records(trace_db, blobs):
    counts = {}
    for trajectory_id, fillers in (("trj_small", 10), ("trj_large", 10_000)):
        session_id = f"s_{trajectory_id}"
        await add_meta(session_id)
        await add_trajectory(trajectory_id, session_id)
        events = [_event(trajectory_id, session_id, "user_a", 1, "input.accepted", {"text": "hi"}, message_id="msg"),
                  _event(trajectory_id, session_id, "user_a", 2, "input.injected", {"text": "again"}, message_id="msg")]
        await Ingest(blobs).append(trajectory_id, events)
        await project_all(ProjectionService(settings(), blob_store=blobs, metrics=Metrics()), trajectory_id)
        filler = {"record_id": "", "kind": "user", "status": "accepted", "start_seq": "1", "as_of_seq": "1"}
        async with trace_session() as db:
            await db.execute(insert(TrajectoryRecord), [
                {"trajectory_id": trajectory_id, "record_id": f"user:filler_{index:05d}", "kind": "user", "status": "accepted",
                 "start_seq": 1, "applied_seq": 1, "projector_version": 1, "search_doc": "user",
                 "data": {**filler, "record_id": f"user:filler_{index:05d}"}, "summary": filler} for index in range(fillers)])
        async with trace_session() as db:
            trajectory = (await get_trajectory(db, session_id))[1]
            with statements(trace_db) as seen:
                detail = await get_record(db, trajectory, "user:msg")
            assert _seqs(detail) == ["1", "2"] and detail["record"]["data"]["text"] == "again"
            counts[trajectory_id] = len(seen)
    assert counts["trj_small"] == counts["trj_large"] <= 6


async def test_record_detail_while_projection_lags_lists_every_event_that_changed_the_record(trace_db, blobs):
    await recorded(blobs, "trj_1", "s1", project=False)
    service = ProjectionService(settings(), blob_store=blobs, metrics=Metrics())
    assert await service.project("trj_1", max_events=4) == 4
    positions, records = (None, "2", "5", "9"), ("request:req_1", "assistant:req_1", "tool:call_1", "run:run_trj_1")

    async def details():
        async with trace_session() as db:
            trajectory = (await get_trajectory(db, "s1"))[1]
            found = {}
            for through in positions:
                for record_id in records:
                    try:
                        found[through, record_id] = await get_record(db, trajectory, record_id, through_seq=through)
                    except LookupError:
                        found[through, record_id] = None
            return found
    lagging = await details()
    # Events after the projected position have no links yet; the replay supplies them.
    assert _seqs(lagging[None, "request:req_1"]) == ["2", "3", "4", "5", "6"]
    assert _seqs(lagging[None, "tool:call_1"]) == ["7", "8", "9"] and _seqs(lagging[None, "run:run_trj_1"]) == ["1", "12"]
    await project_all(service, "trj_1")
    assert lagging == await details()


async def test_reads_at_the_projected_position_ignore_summary_entries_written_after_it(trace_db, blobs):
    await add_meta("s1")
    await add_trajectory("trj_1", "s1")
    events = [_event("trj_1", "s1", "user_a", 1, "trajectory.started", {}),
              _event("trj_1", "s1", "user_a", 2, "input.accepted", {"text": "hi"}, message_id="msg_1"),
              _event("trj_1", "s1", "user_a", 3, "trajectory.renamed", {}, version=2)]
    await Ingest(blobs).append("trj_1", events)
    service = ProjectionService(settings(), blob_store=blobs, metrics=Metrics())
    assert await service.project("trj_1", max_events=2) == 2
    async with trace_session() as db:
        stale = (await get_trajectory(db, "s1"))[1]
    # The unsupported event is projected after the trajectory row was read.
    assert await service.project("trj_1") == 1
    async with trace_session() as db:
        assert (await list_records(db, stale, through_seq="2"))["unsupported_events"] == []
        assert await state_at(db, stale, 2) == replay(events[:2])
        current = (await get_trajectory(db, "s1"))[1]
        assert (await list_records(db, current))["unsupported_events"] == replay(events)["unsupported_events"] != []


async def test_list_records_and_header_statements_do_not_grow_with_the_number_of_records(trace_db, blobs):
    counts = {}
    for trajectory_id, fillers in (("trj_small", 10), ("trj_large", 10_000)):
        session_id = f"s_{trajectory_id}"
        await recorded(blobs, trajectory_id, session_id)
        rows = []
        for index in range(fillers):
            summary = {"record_id": f"user:filler_{index:05d}", "kind": "user", "status": "accepted", "start_seq": "1",
                       "as_of_seq": "1"}
            rows.append({"trajectory_id": trajectory_id, "record_id": summary["record_id"], "kind": "user",
                         "status": "accepted", "start_seq": 1, "applied_seq": 1, "projector_version": 1,
                         "search_doc": "user", "data": {**summary, "data": {"text": "x" * 100}}, "summary": summary})
        async with trace_session() as db:
            await db.execute(insert(TrajectoryRecord), rows)
        async with trace_session() as db:
            trajectory = (await get_trajectory(db, session_id))[1]
            with statements(trace_db) as seen:
                page = await list_records(db, trajectory, limit=5)
                second = await list_records(db, trajectory, limit=5, before=page["next_cursor"])
                header = await get_session_header(db, session_id)
            counts[trajectory_id] = len(seen)
        assert page["has_more"] and second["has_more"] and len(second["items"]) == 5
        assert header["statistics"]["request_count"] == 1
        assert not any("trajectory_records.data" in statement for statement in seen)
    assert counts["trj_small"] == counts["trj_large"]


async def test_record_ids_wider_than_the_key_column_round_trip(trace_db, blobs):
    await add_meta("s1")
    await add_trajectory("trj_1", "s1")
    artifact_id = "报告/" + "x" * 300
    events = [_event("trj_1", "s1", "user_a", 1, "artifact.recorded", {"artifact_id": artifact_id, "name": "report.md"})]
    await Ingest(blobs).append("trj_1", events)
    await project_all(ProjectionService(settings(), blob_store=blobs, metrics=Metrics()), "trj_1")
    record_id = f"artifact:{artifact_id}"
    assert len(record_key(record_id)) == 256 and record_key("tool:short") == "tool:short"
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        detail = await get_record(db, trajectory, record_id)
        listed = await list_records(db, trajectory)
    assert detail["record"]["record_id"] == record_id and _seqs(detail) == ["1"]
    assert [item["record_id"] for item in listed["items"]] == [record_id]


async def test_search_uses_search_documents_at_head_and_replays_other_positions(trace_db, blobs):
    events = await recorded(blobs, "trj_1", "s1")
    state = replay(events)
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        hits = await search(db, trajectory, q="GREP")
        assert hits == {"items": [{"record_id": "tool:call_1", "seq": "9", "kind": "tool",
                                   "preview": search_doc(state["records"]["tool:call_1"])[:240]}],
                        "next_cursor": None, "has_more": False, "through_seq": "12"}
        # LIKE wildcards in the query are literal characters.
        assert [item["record_id"] for item in (await search(db, trajectory, q="50%_d"))["items"]] == ["tool:call_1"]
        assert (await search(db, trajectory, q="5_%"))["items"] == []
        first = await search(db, trajectory, q="_", limit=2)
        assert first["has_more"] and len(first["items"]) == 2
        second = await search(db, trajectory, q="_", limit=2, cursor=first["next_cursor"])
        assert {item["record_id"] for item in second["items"]}.isdisjoint(item["record_id"] for item in first["items"])
        with pytest.raises(TrajectoryError):
            await search(db, trajectory, q="other", limit=2, cursor=first["next_cursor"])
        # Other positions search the replayed state: nothing produced later shows up.
        earlier = await search(db, trajectory, q="grep", through_seq="8")
        assert [(item["record_id"], item["seq"]) for item in earlier["items"]] == [("tool:call_1", "8")]
        assert set(earlier["items"][0]) == {"record_id", "seq", "kind", "preview"}
        assert (await search(db, trajectory, q="no match", through_seq="8"))["items"] == []
        await db.execute(update(TrajectoryRecord).where(TrajectoryRecord.record_id == "run:run_trj_1").values(applied_seq=99))
        assert [item["record_id"] for item in (await search(db, trajectory, q="no match"))["items"]] == ["tool:call_1"]


async def test_read_events_spans_hot_rows_and_segments_with_gap_tail_and_digest_checks(trace_db, blobs):
    events = await recorded(blobs, "trj_1", "s1")
    await archive(blobs, "trj_1", 5)
    await archive(blobs, "trj_1", 9)
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        page = await read_events(db, trajectory)
        assert [event["seq"] for event in page["events"]] == [str(seq) for seq in range(1, 13)]
        assert [event["data"] for event in page["events"]] == [event["data"] for event in events]
        assert [event["occurred_at"] for event in page["events"]] == [event["occurred_at"] for event in events]
        sliced = await read_events(db, trajectory, after_seq="3", until_seq="11", limit=4)
        assert ([event["seq"] for event in sliced["events"]], sliced["has_more"], sliced["from_seq"], sliced["through_seq"],
                sliced["until_seq"]) == (["4", "5", "6", "7"], True, "4", "7", "11")
        rest = await read_events(db, trajectory, after_seq="7", until_seq="11", limit=10)
        assert [event["seq"] for event in rest["events"]] == ["8", "9", "10", "11"] and not rest["has_more"]
        assert (await read_events(db, trajectory, after_seq="12"))["events"] == []
        with pytest.raises(TrajectoryError):
            await read_events(db, trajectory, after_seq="13")
    gets = blobs.gets
    async with trace_session() as db:
        await read_events(db, (await get_trajectory(db, "s1"))[1])
    assert blobs.gets == gets
    async with trace_session() as db:
        await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == "trj_1").values(committed_seq=14, next_seq=15))
    async with trace_session() as db:
        with pytest.raises(CorruptContent, match="tail"):
            await read_events(db, (await get_trajectory(db, "s1"))[1])
        await db.execute(delete(TrajectoryEvent).where(TrajectoryEvent.seq == 11))
    async with trace_session() as db:
        with pytest.raises(CorruptContent, match="gap"):
            await read_events(db, (await get_trajectory(db, "s1"))[1], until_seq="12")
        segment = await db.get(TrajectorySegment, ("trj_1", 6))
    rows = (await read_events_rows(blobs, segment))
    rows[0]["data"] = {"changed": True}
    blobs.objects[segment.storage_key] = encode_segment(rows)[0]
    reset_segment_cache()
    async with trace_session() as db:
        with pytest.raises(CorruptContent, match="digest"):
            await read_events(db, (await get_trajectory(db, "s1"))[1], until_seq="9")


async def test_record_detail_downloads_only_the_segments_holding_its_linked_events(trace_db, blobs):
    await recorded(blobs, "trj_1", "s1")
    records = ("run:run_trj_1", "tool:call_1", "assistant:req_1")
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        hot = {record_id: await get_record(db, trajectory, record_id) for record_id in records}
    for through in (3, 6, 9):
        await archive(blobs, "trj_1", through)
    reset_segment_cache()
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        gets = blobs.gets
        # The run's events are 1 (first segment) and 12 (hot): the other two segments stay untouched.
        assert await get_record(db, trajectory, "run:run_trj_1") == hot["run:run_trj_1"]
        assert blobs.gets == gets + 1
        for record_id in records[1:]:
            assert await get_record(db, trajectory, record_id) == hot[record_id]
        page = await read_events(db, trajectory, after_seq="2", limit=5)
        assert [event["seq"] for event in page["events"]] == ["3", "4", "5", "6", "7"] and page["has_more"]


async def read_events_rows(blobs, segment):
    from trajectory.segments import load_segment
    return await load_segment(blobs, segment)


async def test_events_without_data_expansion_keep_stored_references(trace_db, blobs):
    await add_meta("s1")
    await add_trajectory("trj_1", "s1")
    wide = {f"field_{index:02d}": "w" * 30 for index in range(12)}
    events = conversation("trj_1", "s1", "user_a", extra=[("session.settings_changed", wide, {}),
                                                         ("input.accepted", {"text": "T" * 300}, {"message_id": "msg_1"})])
    await Ingest(blobs, **{**REFERENCES, "payload_bytes": 400}).append("trj_1", events)
    await archive(blobs, "trj_1", 7)
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        expanded = await read_events(db, trajectory)
        stored = await read_events(db, trajectory, include_data=False)
    assert [event["data"] for event in expanded["events"]] == [event["data"] for event in events]
    assert is_ref(stored["events"][-1]["data"]["text"]) and set(stored["events"][-2]["data"]) == {"$payload"}


async def test_a_deleted_payload_keeps_head_reads_on_their_fast_paths(trace_db, blobs):
    await recorded(blobs, "trj_1", "s1")
    await add_payload(blobs, "trj_1", b"gone", first_seq=1, media_type="image/png", availability="deleted")
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        with statements(trace_db) as seen:
            assert len((await list_records(db, trajectory))["items"]) == 5
            assert (await get_session_header(db, "s1"))["statistics"]["request_count"] == 1
            assert (await search(db, trajectory, q="grep"))["items"][0]["record_id"] == "tool:call_1"
            assert _seqs(await get_record(db, trajectory, "tool:call_1")) == ["7", "8", "9"]
    # No replay and no checkpoint: only the record detail reads its own events.
    assert sum("FROM trajectory_events" in statement for statement in seen) == 1
    assert not any("trajectory_checkpoints" in statement for statement in seen)


async def test_segment_cache_stays_within_its_byte_budget(trace_db, blobs, monkeypatch):
    await recorded(blobs, "trj_1", "s1")
    await archive(blobs, "trj_1", 6)
    monkeypatch.setenv("TRAJECTORY_SEGMENT_CACHE_BYTES", "64")
    reset_segment_cache()
    try:
        async with trace_session() as db:
            trajectory = (await get_trajectory(db, "s1"))[1]
            await read_events(db, trajectory)
            gets = blobs.gets
            await read_events(db, trajectory)
        # The segment is larger than the budget: never kept, fetched again.
        assert blobs.gets == gets + 1 and segment_cache().size == 0 and segment_cache().max_bytes == 64
    finally:
        reset_segment_cache()
