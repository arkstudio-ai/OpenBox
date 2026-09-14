"""Trajectory analytics export (trajectory.analytics, WAVE3 contract 4).

The trace database is SQLite, seeded through the projection harness so that
records and session summaries hold what the projector really writes. The
export runs against a memory blob store and the Parquet files are read back
with DuckDB.
"""
import contextlib
import io
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import duckdb
import pytest
from sqlalchemy import update
from sqlalchemy.dialects import postgresql

from tests.unit.test_worker_projection_support import (AT, Ingest, Metrics, add_meta, add_trajectory,  # noqa: F401
    project_all, settings, trace_db)
from trajectory.analytics import parquet, source
from trajectory.analytics.__main__ import run
from trajectory.analytics.export import (DEFAULT_PREFIX, PARQUET_CONTENT_TYPE, AnalyticsConfigError, ExportFailed,
    analytics_prefix, export_day, object_key, remove_stale_build_directories)
from trajectory.analytics.schema import COLUMNS, TABLES
from trajectory.storage import MemoryBlobStore
from trajectory.store.database import close_trace_engine, trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryMetaSession
from trajectory.types import iso
from trajectory.worker.projection import ProjectionService

DAY = date(2026, 9, 14)
MIDNIGHT = datetime(2026, 9, 14, tzinfo=timezone.utc)
RUN = {"run_id": "run_1", "turn_id": "turn_1", "agent_id": "root"}
DUCKDB_TYPES = {"TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE"}


def at(days: int, hours: int = 0, minutes: int = 0, seconds: float = 0.0) -> datetime:
    return MIDNIGHT + timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def session_a() -> list[tuple]:
    """Requests and tool calls on the day before, the day and the day after."""
    req0, req1, req2, req3 = ({**RUN, "request_id": f"req_{index}"} for index in range(4))
    usage = {"input": 120, "output": 30, "cache_read": 20, "cache_write": 5}
    return [
        (at(-1, 23), "run.started", {}, RUN),
        (at(-1, 23, 0, 1), "request.started", {"model": "m-prev", "purpose": "chat"}, req0),
        (at(-1, 23, 0, 2), "request.finished", {"status": "completed"}, req0),
        (at(0, 8), "request.prepared", {"model": "openai/gpt-x", "provider": "openai", "purpose": "chat", "attempt": 1,
            "capture_level": "adapter_input", "input": {"messages": [{"role": "user", "content": "secret prompt"}]}},
         req1),
        (at(0, 8, 0, 1), "request.started", {"model": "openai/gpt-x", "purpose": "chat",
                                             "timing_source": "producer_monotonic"}, req1),
        (at(0, 8, 0, 2), "request.delta", {"chunk_index": 1, "mode": "delta",
                                           "blocks": [{"type": "text", "delta": "secret answer"}]}, req1),
        (at(0, 8, 0, 3), "request.usage", {"mode": "replace", "usage": usage}, req1),
        (at(0, 8, 0, 4), "request.finished", {"status": "completed", "finish_reason": "tool_calls", "usage": usage,
            "chunk_count": 1, "duration_ms": 2500.5, "ttft_ms": 800.25, "timing_source": "producer_monotonic"}, req1),
        (at(0, 8, 0, 5), "request.usage", {"mode": "replace", "source": "existing_billing_ledger",
                                           "usage": {**usage, "credits": "0.012345678901"}}, req1),
        (at(0, 8, 0, 6), "tool.requested", {"tool": "read", "requested_arguments": {"path": "secret.txt"}},
         {**RUN, "call_id": "call_1", "request_id": "req_1"}),
        (at(0, 8, 0, 7), "tool.started", {"tool": "read", "effective_arguments": {"path": "secret.txt"}},
         {**RUN, "call_id": "call_1"}),
        (at(0, 8, 0, 9), "tool.finished", {"tool": "read", "status": "completed", "title": "Read secret.txt",
            "model_output": "secret file", "duration_ms": 1500.0, "total_duration_ms": 2000.0,
            "timing_source": "producer_monotonic"}, {**RUN, "call_id": "call_1"}),
        (at(0, 8, 0, 10), "tool.requested", {"tool": "bash", "requested_arguments": {"command": "secret command"}},
         {**RUN, "call_id": "call_2", "request_id": "req_1"}),
        (at(0, 8, 0, 11), "tool.finished", {"tool": "bash", "status": "denied", "reason": "secret reason",
                                            "duration_ms": None}, {**RUN, "call_id": "call_2"}),
        (at(0, 8, 0, 12), "request.started", {"model": "openai/gpt-x", "purpose": "chat"}, req2),
        (at(0, 8, 0, 13), "request.finished", {"status": "failed", "duration_ms": 100,
            "error": {"type": "RateLimitError", "message": "secret quota"}}, req2),
        (at(0, 9), "tool.requested", {"tool": "grep"}, {**RUN, "call_id": "call_4", "request_id": "req_2"}),
        (at(0, 9, 0, 1), "tool.started", {"tool": "grep"}, {**RUN, "call_id": "call_4"}),
        (at(0, 9, 0, 2), "tool.finished", {"tool": "grep", "status": "failed",
            "error": {"type": "ToolError", "message": "secret path"}}, {**RUN, "call_id": "call_4"}),
        # Requested on the day, started on the next: it belongs to the next day.
        (at(0, 23, 59, 59.5), "tool.requested", {"tool": "glob"}, {**RUN, "call_id": "call_3", "request_id": "req_2"}),
        (at(1, 0, 0, 0.5), "tool.started", {"tool": "glob"}, {**RUN, "call_id": "call_3"}),
        (at(1, 0, 0, 1), "tool.finished", {"tool": "glob", "status": "failed",
            "error": {"type": "TimeoutError", "message": "secret"}}, {**RUN, "call_id": "call_3"}),
        (at(1, 1), "request.started", {"model": "m-next"}, req3),
        (at(1, 1, 0, 1), "request.finished", {"status": "completed"}, req3),
        (at(1, 1, 0, 2), "run.finished", {"status": "completed"}, RUN),
    ]


def one_request(moment: datetime, request_id: str, *, model="anthropic/claude-x", status="failed", **data) -> list[tuple]:
    ids = {"run_id": f"run_{request_id}", "request_id": request_id}
    return [(moment, "request.started", {"model": model}, ids),
            (moment + timedelta(seconds=1), "request.finished", {"status": status, **data}, ids)]


def user_input(moment: datetime) -> list[tuple]:
    return [(moment, "input.accepted", {"text": "secret hello"}, {"message_id": "msg_1"})]


async def append(trajectory_id: str, session_id: str, steps: list[tuple], *, user_id="user_a", project=True) -> None:
    async with trace_session() as db:
        first = (await db.get(SessionTrajectory, trajectory_id)).committed_seq + 1
    events = [{"event_id": f"evt_{trajectory_id}_{seq}", "trajectory_id": trajectory_id, "user_id": user_id,
               "session_id": session_id, "source_session_id": session_id, "seq": str(seq), "type": kind, "version": 1,
               "occurred_at": iso(moment), "data": data, **ids}
              for seq, (moment, kind, data, ids) in enumerate(steps, start=first)]
    await Ingest(MemoryBlobStore()).append(trajectory_id, events)
    if project:
        await project_all(ProjectionService(settings(), blob_store=MemoryBlobStore(), metrics=Metrics()), trajectory_id)


async def seed(trajectory_id: str, session_id: str, steps: list[tuple], *, user_id="user_a", workspace_id="ws_a",
               project=True, **meta) -> None:
    await add_meta(session_id, user_id, workspace_id, **meta)
    await add_trajectory(trajectory_id, session_id, user_id, workspace_id, started_at=steps[0][0])
    await append(trajectory_id, session_id, steps, user_id=user_id, project=project)


async def seed_day() -> None:
    await seed("trj_a", "ses_a", session_a(), agent="build")
    async with trace_session() as db:
        await db.execute(update(TrajectoryMetaSession).where(TrajectoryMetaSession.id == "ses_a")
                         .values(kind="chat", project_id="prj_1"))
    await seed("trj_b", "ses_b", one_request(at(0, 12), "req_b1", error={"type": "APIError", "message": "secret"}),
               user_id="user_b", workspace_id="ws_b")
    await seed("trj_new", "ses_new", user_input(at(0, 15)))
    await seed("trj_raw", "ses_raw", user_input(at(0, 16)), project=False)
    # Not exported: a tombstoned session, one deleted in the metadata replica,
    # and one open across the day without activity on it.
    await seed("trj_gone", "ses_gone", one_request(at(0, 10), "req_gone"))
    async with trace_session() as db:
        await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == "trj_gone")
                         .values(deleted_at=AT, recording_status="deleted"))
    await seed("trj_meta_gone", "ses_meta_gone", one_request(at(0, 11), "req_meta_gone"), is_deleted=True)
    await seed("trj_idle", "ses_idle", one_request(at(-1, 10), "req_before") + one_request(at(1, 10), "req_after"))


def read_parquet(data: bytes, path) -> tuple[list[tuple[str, str]], list[dict]]:
    """(column names and types, rows) of a Parquet file."""
    path.write_bytes(data)
    connection = duckdb.connect()
    try:
        connection.execute("SET TimeZone = 'UTC'")
        described = connection.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
        cursor = connection.execute(f"SELECT * FROM read_parquet('{path}')")
        names = [column[0] for column in cursor.description]
        return [(row[0], row[1]) for row in described], [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def exported(store: MemoryBlobStore, tmp_path, day: date = DAY, prefix: str = DEFAULT_PREFIX) -> dict[str, list[dict]]:
    tables = {}
    for table in TABLES:
        key = object_key(prefix, day, table)
        assert store.content_types[key] == PARQUET_CONTENT_TYPE
        columns, rows = read_parquet(store.objects[key], tmp_path / f"read-{day}-{table}.parquet")
        assert columns == [(name, DUCKDB_TYPES.get(type_, type_)) for name, type_ in COLUMNS[table]]
        tables[table] = rows
    return tables


def pick(row: dict, *names: str) -> dict:
    return {name: row[name] for name in names}


async def test_export_writes_the_day_as_three_parquet_files(trace_db, tmp_path):
    await seed_day()
    store, metrics = MemoryBlobStore(), Metrics()

    summary = await export_day(DAY, engine=trace_db, blob_store=store, metrics=metrics, temp_root=tmp_path)

    keys = [object_key(DEFAULT_PREFIX, DAY, table) for table in TABLES]
    assert keys == [f"analytics/trajectories/dt=2026-09-14/{table}.parquet" for table in TABLES]
    assert sorted(store.objects) == sorted(keys)
    assert (summary.date, summary.dry_run, summary.objects, summary.error) == ("2026-09-14", False, 3, None)
    assert summary.rows == {"sessions": 4, "requests": 3, "tools": 3}
    assert summary.bytes == sum(len(store.objects[key]) for key in keys)
    assert metrics.counters == {"analytics_exports": 1}
    assert list(tmp_path.glob("openbox-analytics-*")) == []
    tables = exported(store, tmp_path)

    sessions = tables["sessions"]
    assert [row["trajectory_id"] for row in sessions] == ["trj_a", "trj_b", "trj_new", "trj_raw"]
    steps = len(session_a())
    assert sessions[0] == {
        "trajectory_id": "trj_a", "session_id": "ses_a", "user_id": "user_a", "workspace_id": "ws_a",
        "project_id": "prj_1", "session_kind": "chat", "agent": "build", "model": "m-next",
        "started_at": at(-1, 23), "last_activity_at": at(1, 1, 0, 2), "recording_status": "recording",
        "running_status": "idle", "budget_level": "normal", "event_count": steps, "stored_bytes": 0,
        "committed_seq": steps, "projected_seq": steps,
        "request_count": 4, "tool_count": 4, "error_count": 4, "unknown_count": 0, "input_tokens": 120,
        "output_tokens": 30, "usage_complete": False,
        "day_request_count": 2, "day_tool_count": 3, "day_error_count": 3, "day_input_tokens": 120,
        "day_output_tokens": 30, "day_credits": Decimal("0.012345678901"),
    }
    assert pick(sessions[1], "user_id", "workspace_id", "model", "request_count", "error_count", "input_tokens",
                "usage_complete", "day_request_count", "day_error_count", "day_input_tokens", "day_credits") == {
        "user_id": "user_b", "workspace_id": "ws_b", "model": "anthropic/claude-x", "request_count": 1,
        "error_count": 1, "input_tokens": None, "usage_complete": False, "day_request_count": 1,
        "day_error_count": 1, "day_input_tokens": None, "day_credits": None}
    # Started on the day without requests; the unprojected session has no statistics yet.
    assert pick(sessions[2], "request_count", "input_tokens", "usage_complete", "day_request_count",
                "day_tool_count") == {"request_count": 0, "input_tokens": None, "usage_complete": True,
                                      "day_request_count": 0, "day_tool_count": 0}
    assert pick(sessions[3], "committed_seq", "projected_seq", "running_status", "request_count",
                "usage_complete", "day_request_count") == {"committed_seq": 1, "projected_seq": 0,
        "running_status": None, "request_count": None, "usage_complete": None, "day_request_count": 0}

    requests = tables["requests"]
    assert [(row["trajectory_id"], row["request_id"]) for row in requests] == [
        ("trj_a", "req_1"), ("trj_a", "req_2"), ("trj_b", "req_b1")]
    assert requests[0] == {
        "trajectory_id": "trj_a", "session_id": "ses_a", "user_id": "user_a", "workspace_id": "ws_a",
        "request_id": "req_1", "source_session_id": "ses_a", "run_id": "run_1", "turn_id": "turn_1",
        "step_id": None, "agent_id": "root", "parent_agent_id": None, "parent_call_id": None,
        "status": "completed", "model": "openai/gpt-x", "provider": "openai", "purpose": "chat", "attempt": 1,
        "finish_reason": "tool_calls", "error_type": None, "started_at": at(0, 8, 0, 1),
        "finished_at": at(0, 8, 0, 4), "duration_ms": 2500.5, "ttft_ms": 800.25,
        "timing_source": "producer_monotonic", "chunk_count": 1, "input_tokens": 120, "output_tokens": 30,
        "cache_read_tokens": 20, "cache_write_tokens": 5, "credits": Decimal("0.012345678901"),
        "start_seq": 4, "end_seq": 8,
    }
    assert pick(requests[1], "status", "error_type", "duration_ms", "input_tokens", "credits", "provider",
                "start_seq", "end_seq") == {"status": "failed", "error_type": "RateLimitError", "duration_ms": 100.0,
        "input_tokens": None, "credits": None, "provider": None, "start_seq": 15, "end_seq": 16}

    tools = tables["tools"]
    assert [(row["call_id"], row["tool"], row["status"]) for row in tools] == [
        ("call_1", "read", "completed"), ("call_2", "bash", "denied"), ("call_4", "grep", "failed")]
    assert tools[0] == {
        "trajectory_id": "trj_a", "session_id": "ses_a", "user_id": "user_a", "workspace_id": "ws_a",
        "call_id": "call_1", "request_id": "req_1", "source_session_id": "ses_a", "run_id": "run_1",
        "turn_id": "turn_1", "step_id": None, "agent_id": "root", "parent_call_id": None, "tool": "read",
        "status": "completed", "error_type": None, "requested_at": at(0, 8, 0, 6), "started_at": at(0, 8, 0, 7),
        "finished_at": at(0, 8, 0, 9), "duration_ms": 1500.0, "total_duration_ms": 2000.0,
        "timing_source": "producer_monotonic", "start_seq": 10, "end_seq": 12,
    }
    # A denied call never starts: its request time places it on the day.
    assert pick(tools[1], "requested_at", "started_at", "duration_ms", "timing_source") == {
        "requested_at": at(0, 8, 0, 10), "started_at": None, "duration_ms": None, "timing_source": None}
    assert pick(tools[2], "error_type", "duration_ms", "timing_source", "request_id") == {
        "error_type": "ToolError", "duration_ms": 1000.0, "timing_source": "session_timestamps",
        "request_id": "req_2"}

    # Identifiers, names, times and numbers only: no prompt, output, argument, title or error message.
    values = [value for rows in tables.values() for row in rows for value in row.values() if isinstance(value, str)]
    assert values and not [value for value in values if "secret" in value]


async def test_pages_of_any_size_read_the_same_rows(trace_db):
    await seed_day()

    async def read(page_rows: int) -> dict[str, list[str]]:
        tables = {table: [] for table in TABLES}
        async with contextlib.aclosing(source.read_day(trace_db, DAY, page_rows=page_rows)) as pages:
            async for table, rows in pages:
                assert rows and len(rows) <= page_rows
                tables[table].extend(json.dumps(row, sort_keys=True) for row in rows)
        return {table: sorted(rows) for table, rows in tables.items()}

    everything = await read(source.PAGE_ROWS)
    assert {table: len(rows) for table, rows in everything.items()} == {"sessions": 4, "requests": 3, "tools": 3}
    assert await read(1) == everything
    assert await read(2) == everything


async def test_dry_run_builds_the_files_without_uploading(trace_db, tmp_path):
    await seed_day()
    store, metrics = MemoryBlobStore(), Metrics()

    summary = await export_day(DAY, engine=trace_db, blob_store=store, dry_run=True, metrics=metrics,
                               temp_root=tmp_path)

    assert (summary.dry_run, summary.objects, summary.rows) == (True, 0, {"sessions": 4, "requests": 3, "tools": 3})
    assert summary.bytes > 0
    assert (store.objects, store.puts, metrics.counters) == ({}, 0, {})
    out = io.StringIO()
    assert await run(["export", "--date", "2026-09-14", "--dry-run"], stdout=out, engine=trace_db, blob_store=store,
                     metrics=metrics, today=DAY) == 0
    line = json.loads(out.getvalue())
    assert (line["dry_run"], line["objects"], line["rows"]) == (True, 0, {"sessions": 4, "requests": 3, "tools": 3})
    assert (store.objects, metrics.counters) == ({}, {})


async def test_a_rerun_overwrites_the_same_date(trace_db, tmp_path):
    await seed_day()
    store, metrics = MemoryBlobStore(), Metrics()
    first = await export_day(DAY, engine=trace_db, blob_store=store, metrics=metrics, temp_root=tmp_path)
    before = dict(store.objects)

    again = await export_day(DAY, engine=trace_db, blob_store=store, metrics=metrics, temp_root=tmp_path)
    assert store.objects == before
    assert (again.rows, again.bytes) == (first.rows, first.bytes)

    # A late request of the day arrives; the rerun replaces all three objects of the date.
    await append("trj_b", "ses_b", one_request(at(0, 13), "req_b2", status="completed",
                                               usage={"input": 7, "output": 3, "credits": "0.5"}), user_id="user_b")
    rerun = await export_day(DAY, engine=trace_db, blob_store=store, metrics=metrics, temp_root=tmp_path)

    assert sorted(store.objects) == sorted(before)
    assert store.puts == 9
    assert rerun.rows == {"sessions": 4, "requests": 4, "tools": 3}
    tables = exported(store, tmp_path)
    assert [row["request_id"] for row in tables["requests"]] == ["req_1", "req_2", "req_b1", "req_b2"]
    session_b = next(row for row in tables["sessions"] if row["trajectory_id"] == "trj_b")
    assert pick(session_b, "day_request_count", "day_input_tokens", "day_credits", "input_tokens") == {
        "day_request_count": 2, "day_input_tokens": 7, "day_credits": Decimal("0.5"), "input_tokens": 7}

    # Another date leaves this one alone.
    replaced = dict(store.objects)
    await export_day(DAY + timedelta(days=1), engine=trace_db, blob_store=store, metrics=metrics, temp_root=tmp_path)
    assert len(store.objects) == 6
    assert {key: store.objects[key] for key in replaced} == replaced
    next_day = exported(store, tmp_path, day=DAY + timedelta(days=1))
    assert [row["request_id"] for row in next_day["requests"]] == ["req_3", "req_after"]
    assert [row["call_id"] for row in next_day["tools"]] == ["call_3"]
    assert metrics.counters == {"analytics_exports": 4}


async def test_a_failed_upload_exits_1_with_the_summary_line(trace_db):
    await seed_day()
    store, metrics = MemoryBlobStore(), Metrics()

    def unreachable(key: str) -> None:
        if key.endswith("/requests.parquet"):
            raise ConnectionError("injected put failure")

    store.faults["put"] = unreachable
    out, err = io.StringIO(), io.StringIO()

    code = await run(["export", "--date", "2026-09-14"], stdout=out, stderr=err, engine=trace_db, blob_store=store,
                     metrics=metrics, today=DAY)

    assert code == 1
    line = json.loads(out.getvalue())
    assert (line["date"], line["dry_run"], line["objects"], line["error"]) == ("2026-09-14", False, 1, "ConnectionError")
    assert line["rows"] == {"sessions": 4, "requests": 3, "tools": 3}
    assert "failed: ConnectionError" in err.getvalue()
    assert sorted(store.objects) == [object_key(DEFAULT_PREFIX, DAY, "sessions")]
    assert metrics.counters == {"analytics_export_failures": 1}


async def test_a_failed_read_raises_export_failed(trace_db, tmp_path):
    await seed_day()
    metrics = Metrics()
    async with trace_db.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE trajectory_records")

    with pytest.raises(ExportFailed) as failed:
        await export_day(DAY, engine=trace_db, blob_store=MemoryBlobStore(), metrics=metrics, temp_root=tmp_path)

    assert (failed.value.summary.objects, failed.value.summary.error) == (0, "OperationalError")
    assert metrics.counters == {"analytics_export_failures": 1}
    assert list(tmp_path.glob("openbox-analytics-*")) == []


async def test_the_command_exports_from_the_configured_trace_database(trace_db, tmp_path, monkeypatch):
    await seed("trj_b", "ses_b", one_request(at(0, 12), "req_b1"), user_id="user_b")
    await close_trace_engine()
    monkeypatch.setenv("TRAJECTORY_WORKER_MODE", "external")
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    monkeypatch.setenv("TRAJECTORY_ANALYTICS_PREFIX", " /team/trajectory-analytics/ ")
    store, metrics = MemoryBlobStore(), Metrics()
    out = io.StringIO()

    assert await run(["export", "--date", "2026-09-14"], stdout=out, blob_store=store, metrics=metrics,
                     today=DAY + timedelta(days=1)) == 0

    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert list(line) == ["date", "dry_run", "objects", "rows", "bytes", "duration_ms"]
    assert (line["date"], line["objects"], line["rows"]) == ("2026-09-14", 3, {"sessions": 1, "requests": 1, "tools": 0})
    assert line["bytes"] == sum(len(value) for value in store.objects.values())
    assert sorted(store.objects) == sorted(f"team/trajectory-analytics/dt=2026-09-14/{table}.parquet"
                                           for table in TABLES)
    assert metrics.counters == {"analytics_exports": 1}
    tables = exported(store, tmp_path, prefix="team/trajectory-analytics/")
    assert tables["tools"] == []
    assert [row["request_id"] for row in tables["requests"]] == ["req_b1"]


@pytest.mark.parametrize("argv", [
    [], ["export"], ["export", "--date", "2026-9-14"], ["export", "--date", "20260914"],
    ["export", "--date", "2026-02-30"], ["import", "--date", "2026-09-14"],
])
async def test_usage_errors_exit_2(argv, capsys):
    metrics = Metrics()
    assert await run(argv, blob_store=MemoryBlobStore(), metrics=metrics, today=DAY) == 2
    assert metrics.counters == {}


async def test_configuration_errors_exit_2(tmp_path, monkeypatch):
    store, metrics = MemoryBlobStore(), Metrics()

    async def refused(argv=("export", "--date", "2026-09-14"), **kwargs) -> str:
        err = io.StringIO()
        assert await run(list(argv), stderr=err, metrics=metrics, today=DAY, **{"blob_store": store, **kwargs}) == 2
        return err.getvalue()

    assert "after the current UTC day" in await refused(("export", "--date", "2026-09-15"))
    monkeypatch.setenv("TRAJECTORY_WORKER_MODE", "external")
    assert "TRAJECTORY_DATABASE_URL is required" in await refused()
    url = f"sqlite+aiosqlite:///{tmp_path / 'absent.db'}"
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", url)
    assert "trace database file not found" in await refused()
    (tmp_path / "business.db").touch()
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'business.db'}")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'business.db'}")
    assert "must not name the business database" in await refused()
    monkeypatch.setenv("TRAJECTORY_ANALYTICS_PREFIX", "trajectories/analytics")
    assert "inside the trajectory namespace" in await refused()
    monkeypatch.setenv("TRAJECTORY_ANALYTICS_PREFIX", "analytics/../trajectories")
    assert "Invalid blob key" in await refused()
    monkeypatch.delenv("TRAJECTORY_ANALYTICS_PREFIX")
    monkeypatch.setenv("TRAJECTORY_BLOB_PROVIDER", "s3")
    assert "Unsupported TRAJECTORY_BLOB_PROVIDER" in await refused(blob_store=None)
    assert metrics.counters == {}


def test_analytics_prefix(monkeypatch):
    assert analytics_prefix() == DEFAULT_PREFIX == "analytics/trajectories/"
    assert analytics_prefix("  ") == DEFAULT_PREFIX
    assert analytics_prefix(" /team/trajectory-analytics// ") == "team/trajectory-analytics/"
    for value in ("a//b", "a/../b", "trajectories", "trajectories/analytics", "C:/analytics"):
        with pytest.raises(AnalyticsConfigError):
            analytics_prefix(value)
    monkeypatch.setenv("TRAJECTORY_BLOB_PROVIDER", "oss")
    monkeypatch.setenv("TRAJECTORY_OSS_PREFIX", "openbox/trajectories")
    assert analytics_prefix("trajectories/analytics") == "trajectories/analytics/"
    with pytest.raises(AnalyticsConfigError):
        analytics_prefix("openbox/trajectories/analytics")


def test_duckdb_is_limited_and_confined_to_the_build_directory(tmp_path):
    connection = parquet.connect(tmp_path)
    try:
        settings_rows = dict(connection.execute(
            "SELECT name, value FROM duckdb_settings() WHERE name IN ('memory_limit', 'threads', "
            "'enable_external_access', 'autoinstall_known_extensions', 'autoload_known_extensions', "
            "'allow_community_extensions', 'lock_configuration', 'TimeZone')").fetchall())
        assert settings_rows == {"memory_limit": "244.1 MiB", "threads": "1", "enable_external_access": "false",
                                 "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
                                 "allow_community_extensions": "false", "lock_configuration": "true",
                                 "TimeZone": "UTC"}
        outside = tmp_path.parent / f"{tmp_path.name}-outside.parquet"
        for statement in ("INSTALL httpfs", "LOAD httpfs", "SET enable_external_access = true",
                          "SET memory_limit = '4GB'", "SELECT * FROM read_parquet('https://example.invalid/a.parquet')",
                          f"COPY (SELECT 1 AS x) TO '{outside}' (FORMAT parquet)"):
            with pytest.raises(duckdb.Error):
                connection.execute(statement)
        assert not outside.exists()
        inside = tmp_path.resolve() / "inside.parquet"
        assert connection.execute(f"COPY (SELECT 1 AS x) TO '{inside}' (FORMAT parquet)").fetchone() == (1,)
    finally:
        connection.close()


def test_staged_rows_must_match_the_table_columns(tmp_path):
    staged = parquet.StagedTable(tmp_path, "tools")
    try:
        with pytest.raises(ValueError, match="tool_name"):
            staged.write([{**dict.fromkeys(name for name, _ in COLUMNS["tools"]), "tool_name": "read"}])
        assert staged.rows == 0
    finally:
        staged.close()


async def test_postgres_reads_are_read_only_with_an_explicit_statement_timeout():
    executed = []

    class Connection:
        def __init__(self, dialect: str):
            self.dialect = SimpleNamespace(name=dialect)

        async def execute(self, statement):
            executed.append(str(statement))

    await source._read_only(Connection("postgresql"))
    assert executed == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '60s'"]
    executed.clear()
    await source._read_only(Connection("sqlite"))
    assert executed == []


def test_postgres_statements_extract_json_scalars_and_compare_times_bytewise():
    start, end = source.day_bounds(DAY)
    assert (start, end) == (MIDNIGHT, MIDNIGHT + timedelta(days=1))
    sessions = str(source.sessions_statement("postgresql", start, end, "", 500).compile(dialect=postgresql.dialect()))
    assert "jsonb_extract_path_text(trajectory_session_summaries.statistics, 'usage_missing')" in sessions
    assert "LEFT OUTER JOIN trajectory_meta_sessions" in sessions
    tools = str(source.records_statement("postgresql", "tool", ["trj_a"], start, end, ("trj_a", "tool:call_1"), 500)
                .compile(dialect=postgresql.dialect()))
    assert "jsonb_extract_path_text(trajectory_records.data, 'data', 'requested_at')" in tools
    assert 'COLLATE "C"' in tools
    assert "(trajectory_records.trajectory_id, trajectory_records.record_id) >" in tools


def test_row_values_are_normalized():
    assert [source._integer(value) for value in (42, "42", 42.0, " 7 ", Decimal("3"))] == [42, 42, 42, 7, 3]
    assert [source._integer(value) for value in (True, "4.5", 2**63, "nan", "x", None, [1])] == [None] * 7
    assert (source._float("12.5"), source._float("inf"), source._float(False)) == (12.5, None, None)
    assert source._credits("1E-7") == "0.000000100000"
    assert source._credits(Decimal("0.1234567890129")) == "0.123456789013"
    assert (source._credits("x"), source._credits("1e30")) == (None, None)
    assert source._instant("2026-09-14T08:00:00.000Z") == at(0, 8)
    assert source._instant(datetime(2026, 9, 14, 8)) == at(0, 8)
    assert (source._instant("yesterday"), source._instant(12)) == (None, None)
    assert source._text("a" * 300) == "a" * source.TEXT_LIMIT
    assert (source._text("\ud800x"), source._text({"a": 1}), source._text(5)) == ("?x", None, "5")


def test_stale_build_directories_are_removed(tmp_path):
    stale, fresh, other = (tmp_path / "openbox-analytics-stale", tmp_path / "openbox-analytics-fresh",
                           tmp_path / "unrelated")
    for directory in (stale, fresh, other):
        directory.mkdir()
    (stale / "sessions.jsonl").write_text("{}\n")
    old = time.time() - 2 * 24 * 3600
    os.utime(stale, (old, old))
    os.utime(other, (old, old))

    assert remove_stale_build_directories(tmp_path) == 1
    assert (stale.exists(), fresh.exists(), other.exists()) == (False, True, True)
