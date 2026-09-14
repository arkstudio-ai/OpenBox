"""Trace database reads of the analytics export (WAVE3 contracts 2 and 4).

One UTC day is read inside a single read-only transaction. On PostgreSQL it
is REPEATABLE READ, so sessions and records come from one snapshot, and it
sets its own statement timeout: the trace role's 5 s default is meant for
request-serving reads. Candidate sessions are the live trajectories (neither
tombstoned nor deleted in the metadata replica) whose started_at ..
last_activity_at range overlaps the day. They are read in keyset pages of
PAGE_ROWS, each page followed by the day's request and tool records of its
trajectories in pages of their own, so memory stays bounded by the page size
whatever the day holds.

Attribution: a request belongs to the day it started; a tool call to the day
it started, else was requested, else finished (a denied call never starts).
A candidate session is kept when it started or was last active on the day,
or has a request or tool call on it. Record times are the projector's UTC
ISO-8601 strings (``trajectory.types.iso``), so the database narrows records
by comparing text and Python checks the parsed instant.

Only identifiers, statuses, names, times and numbers are read from records:
never titles, previews, arguments, outputs or error messages.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation
from typing import AsyncIterator

from sqlalchemy import collate, false, func, literal_column, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from trajectory.projector import ERROR, token_value
from trajectory.store.models import SessionTrajectory, TrajectoryMetaSession, TrajectoryRecord, TrajectorySessionSummary

#: PostgreSQL statement timeout of the export transaction (``SET LOCAL``, contract 2).
STATEMENT_TIMEOUT = "60s"
#: Sessions per page, and records per page of those sessions.
PAGE_ROWS = 500
#: Longest text kept: names and enumerations, cut beyond this.
TEXT_LIMIT = 256
_BIGINT_MAX = 2**63 - 1
#: Credits at the billing precision fit DECIMAL(38,12) below this bound.
_CREDITS_SCALE = Decimal("0.000000000001")
_CREDITS_BOUND = Decimal(10) ** 26
_DECIMAL = Context(prec=40)

#: Usage names as trajectory.projector.contribution reads them, then the
#: cache buckets of billing.pricing.normalize_usage with their provider names.
INPUT_TOKENS = ("input_tokens", "prompt_tokens", "input")
OUTPUT_TOKENS = ("output_tokens", "completion_tokens", "output")
CACHE_READ_TOKENS = ("cache_read", "cache_read_input_tokens")
CACHE_WRITE_TOKENS = ("cache_write", "cache_creation_input_tokens")
#: Keys of trajectory_session_summaries.statistics (projector.contribution).
STATISTICS = ("request_count", "tool_count", "error_count", "unknown_count", "input_tokens", "output_tokens",
              "usage_missing")
#: Identity fields of a record summary, per file.
REQUEST_IDENTITY = ("request_id", "source_session_id", "run_id", "turn_id", "step_id", "agent_id", "parent_agent_id",
                    "parent_call_id")
TOOL_IDENTITY = ("call_id", "request_id", "source_session_id", "run_id", "turn_id", "step_id", "agent_id",
                 "parent_call_id")
#: Scalars inside trajectory_records.data (the projected record; its own fields are under "data").
#: measured_* are the producer's duration and timing source from the finished event.
REQUEST_FIELDS = {
    "model": ("data", "model"), "provider": ("data", "provider"), "purpose": ("data", "purpose"),
    "attempt": ("data", "attempt"), "finish_reason": ("data", "finish_reason"),
    "error_type": ("data", "error", "type"), "chunk_count": ("data", "chunk_count"), "ttft_ms": ("data", "ttft_ms"),
    "measured_ms": ("data", "duration_ms"), "measured_timing": ("data", "timing_source"),
}
TOOL_FIELDS = {
    "tool": ("data", "tool"), "tool_name": ("data", "name"), "tool_alias": ("data", "tool_name"),
    "requested_at": ("data", "requested_at"), "total_duration_ms": ("data", "total_duration_ms"),
    "error_type": ("data", "error", "type"),
    "measured_ms": ("data", "duration_ms"), "measured_timing": ("data", "timing_source"),
}


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a UTC day."""
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


# -- Statements --

def _json(dialect: str, column, *path: str):
    """A scalar inside a JSON column: its text on PostgreSQL (JSONB), its JSON value on SQLite.

    The path keys are the constants above, so they are inlined as literals.
    """
    if dialect == "postgresql":
        return func.jsonb_extract_path_text(column, *(literal_column(f"'{key}'") for key in path))
    return func.json_extract(column, literal_column(f"'$.{'.'.join(path)}'"))


def sessions_statement(dialect: str, start: datetime, end: datetime, after: str, limit: int):
    """Candidate sessions of [start, end) with a trajectory id after ``after``, in id order."""
    trajectory, summary, meta = SessionTrajectory, TrajectorySessionSummary, TrajectoryMetaSession
    return (
        select(trajectory.id, trajectory.user_id, trajectory.session_id, trajectory.workspace_id,
               trajectory.started_at, trajectory.last_activity_at, trajectory.recording_status,
               trajectory.budget_level, trajectory.event_count, trajectory.stored_bytes, trajectory.committed_seq,
               trajectory.projected_seq, summary.trajectory_id.label("summary_id"), summary.running_status,
               summary.model, *(_json(dialect, summary.statistics, key).label(key) for key in STATISTICS),
               meta.kind.label("session_kind"), meta.agent, meta.project_id)
        .select_from(trajectory)
        .outerjoin(summary, summary.trajectory_id == trajectory.id)
        .outerjoin(meta, meta.id == trajectory.session_id)
        .where(trajectory.deleted_at.is_(None), or_(meta.id.is_(None), meta.is_deleted == false()),
               trajectory.started_at < end, trajectory.last_activity_at >= start, trajectory.id > after)
        .order_by(trajectory.id)
        .limit(limit)
    )


def records_statement(dialect: str, kind: str, trajectory_ids: list[str], start: datetime, end: datetime,
                      after: tuple[str, str] | None, limit: int):
    """``request`` or ``tool`` records of these trajectories whose time text falls on the day, in key order."""
    record = TrajectoryRecord
    fields = REQUEST_FIELDS if kind == "request" else TOOL_FIELDS
    times = [_json(dialect, record.summary, "started_at")]
    if kind == "tool":
        times.append(_json(dialect, record.data, "data", "requested_at"))
    times.append(_json(dialect, record.summary, "finished_at"))
    at = func.coalesce(*times)
    if dialect == "postgresql":
        # Byte order: a linguistic collation need not sort ISO-8601 text chronologically.
        at = collate(at, "C")
    statement = (
        select(record.trajectory_id, record.record_id, record.status, record.start_seq, record.end_seq,
               record.summary, *(_json(dialect, record.data, *path).label(name) for name, path in fields.items()))
        .where(record.trajectory_id.in_(trajectory_ids), record.kind == kind,
               at >= start.date().isoformat(), at < end.date().isoformat())
    )
    if after is not None:
        statement = statement.where(tuple_(record.trajectory_id, record.record_id) > tuple_(*after))
    return statement.order_by(record.trajectory_id, record.record_id).limit(limit)


async def _read_only(connection: AsyncConnection) -> None:
    """PostgreSQL: a read-only transaction whose statements may run STATEMENT_TIMEOUT. SQLite: nothing to do."""
    if connection.dialect.name == "postgresql":
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        await connection.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))


# -- Reading --

@dataclass
class DayTotals:
    """A session's rows in the day's requests and tools files."""

    requests: int = 0
    tools: int = 0
    errors: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    credits: Decimal | None = None

    def add_request(self, row: dict) -> None:
        self.requests += 1
        self.errors += int(row["status"] in ERROR)
        self.input_tokens = _add(self.input_tokens, row["input_tokens"])
        self.output_tokens = _add(self.output_tokens, row["output_tokens"])
        if row["credits"] is not None:
            self.credits = _DECIMAL.add(self.credits or Decimal(0), Decimal(row["credits"]))

    def add_tool(self, row: dict) -> None:
        self.tools += 1
        self.errors += int(row["status"] in ERROR)


async def read_day(engine: AsyncEngine, day: date, *, page_rows: int = PAGE_ROWS) -> AsyncIterator[tuple[str, list[dict]]]:
    """(table, rows) pages of one UTC day; a page of sessions follows the request and tool rows of its trajectories.

    The transaction stays open between pages: consume this with
    contextlib.aclosing so an abandoned read returns its connection.
    """
    start, end = day_bounds(day)
    async with engine.connect() as connection:
        dialect = connection.dialect.name
        if dialect == "postgresql":
            await connection.execution_options(isolation_level="REPEATABLE READ")
        async with connection.begin():
            await _read_only(connection)
            after = ""
            while True:
                candidates = (await connection.execute(
                    sessions_statement(dialect, start, end, after, page_rows))).all()
                if not candidates:
                    return
                owners = {row.id: row for row in candidates}
                totals: dict[str, DayTotals] = {}
                for kind, table in (("request", "requests"), ("tool", "tools")):
                    shape = request_row if kind == "request" else tool_row
                    cursor = None
                    while True:
                        records = (await connection.execute(
                            records_statement(dialect, kind, list(owners), start, end, cursor, page_rows))).all()
                        rows = []
                        for record in records:
                            row = shape(owners[record.trajectory_id], record, start, end)
                            if row is None:
                                continue
                            day_totals = totals.setdefault(record.trajectory_id, DayTotals())
                            (day_totals.add_request if kind == "request" else day_totals.add_tool)(row)
                            rows.append(row)
                        if rows:
                            yield table, rows
                        if len(records) < page_rows:
                            break
                        cursor = (records[-1].trajectory_id, records[-1].record_id)
                sessions = [row for row in (session_row(owner, totals.get(owner.id), start, end)
                                            for owner in candidates) if row is not None]
                if sessions:
                    yield "sessions", sessions
                if len(candidates) < page_rows:
                    return
                after = candidates[-1].id


# -- Rows --

def session_row(owner, totals: DayTotals | None, start: datetime, end: datetime) -> dict | None:
    """The sessions row of a candidate, or None when it has no activity on the day."""
    started, last = _utc(owner.started_at), _utc(owner.last_activity_at)
    if totals is None and not start <= started < end and not start <= last < end:
        return None
    totals = totals or DayTotals()
    projected = owner.summary_id is not None
    missing = _integer(owner.usage_missing) or 0
    # As the admin session list reports them: tokens only when some request has usage.
    with_usage = projected and (_integer(owner.request_count) or 0) > missing
    return {
        "trajectory_id": _text(owner.id), "session_id": _text(owner.session_id), "user_id": _text(owner.user_id),
        "workspace_id": _text(owner.workspace_id), "project_id": _text(owner.project_id),
        "session_kind": _text(owner.session_kind), "agent": _text(owner.agent), "model": _text(owner.model),
        "started_at": _timestamp(started), "last_activity_at": _timestamp(last),
        "recording_status": _text(owner.recording_status), "running_status": _text(owner.running_status),
        "budget_level": _text(owner.budget_level), "event_count": _integer(owner.event_count),
        "stored_bytes": _integer(owner.stored_bytes), "committed_seq": _integer(owner.committed_seq),
        "projected_seq": _integer(owner.projected_seq),
        "request_count": _integer(owner.request_count) if projected else None,
        "tool_count": _integer(owner.tool_count) if projected else None,
        "error_count": _integer(owner.error_count) if projected else None,
        "unknown_count": _integer(owner.unknown_count) if projected else None,
        "input_tokens": _integer(owner.input_tokens) if with_usage else None,
        "output_tokens": _integer(owner.output_tokens) if with_usage else None,
        "usage_complete": missing == 0 if projected else None,
        "day_request_count": totals.requests, "day_tool_count": totals.tools, "day_error_count": totals.errors,
        "day_input_tokens": _integer(totals.input_tokens), "day_output_tokens": _integer(totals.output_tokens),
        "day_credits": _credits(totals.credits),
    }


def _owner(owner) -> dict:
    return {"trajectory_id": _text(owner.id), "session_id": _text(owner.session_id),
            "user_id": _text(owner.user_id), "workspace_id": _text(owner.workspace_id)}


def _summary(record) -> dict:
    return record.summary if isinstance(record.summary, dict) else {}


def _duration(record, summary: dict, *, ran: bool = True) -> tuple[float | None, str | None]:
    """(duration_ms, timing_source): the producer's measurement when the finished event carried one, else the projection's.

    A later event on a closed record, such as the billing usage that follows
    request.finished, makes the projector recompute the duration from the
    record's timestamps; the record data keeps the producer's measurement.
    """
    if not ran:
        return None, None
    measured = _float(record.measured_ms)
    if measured is not None:
        return measured, _text(record.measured_timing) or "producer_monotonic"
    return _float(summary.get("duration_ms")), _text(summary.get("timing_source"))


def request_row(owner, record, start: datetime, end: datetime) -> dict | None:
    """The requests row of a request record that started in [start, end), else None."""
    summary = _summary(record)
    started, finished = _instant(summary.get("started_at")), _instant(summary.get("finished_at"))
    at = started or finished
    if at is None or not start <= at < end:
        return None
    usage = summary.get("usage") if isinstance(summary.get("usage"), dict) else {}
    duration, timing = _duration(record, summary)
    return {
        **_owner(owner), **{name: _text(summary.get(name)) for name in REQUEST_IDENTITY},
        "status": _text(record.status), "model": _text(record.model), "provider": _text(record.provider),
        "purpose": _text(record.purpose), "attempt": _integer(record.attempt),
        "finish_reason": _text(record.finish_reason), "error_type": _text(record.error_type),
        "started_at": _timestamp(started), "finished_at": _timestamp(finished),
        "duration_ms": duration, "ttft_ms": _float(record.ttft_ms), "timing_source": timing,
        "chunk_count": _integer(record.chunk_count),
        "input_tokens": _integer(token_value(usage, INPUT_TOKENS)),
        "output_tokens": _integer(token_value(usage, OUTPUT_TOKENS)),
        "cache_read_tokens": _integer(token_value(usage, CACHE_READ_TOKENS)),
        "cache_write_tokens": _integer(token_value(usage, CACHE_WRITE_TOKENS)),
        "credits": _credits(usage.get("credits")),
        "start_seq": _integer(record.start_seq), "end_seq": _integer(record.end_seq),
    }


def tool_row(owner, record, start: datetime, end: datetime) -> dict | None:
    """The tools row of a tool record that started (else was requested, else finished) in [start, end), else None."""
    summary = _summary(record)
    requested = _instant(record.requested_at)
    started, finished = _instant(summary.get("started_at")), _instant(summary.get("finished_at"))
    at = started or requested or finished
    if at is None or not start <= at < end:
        return None
    name = next((value for value in (record.tool, record.tool_name, record.tool_alias) if value not in (None, "")), None)
    # A call that never started has no duration, as in the projected record.
    duration, timing = _duration(record, summary, ran=started is not None)
    return {
        **_owner(owner), **{field: _text(summary.get(field)) for field in TOOL_IDENTITY},
        "tool": _text(name), "status": _text(record.status), "error_type": _text(record.error_type),
        "requested_at": _timestamp(requested), "started_at": _timestamp(started), "finished_at": _timestamp(finished),
        "duration_ms": duration, "total_duration_ms": _float(record.total_duration_ms), "timing_source": timing,
        "start_seq": _integer(record.start_seq), "end_seq": _integer(record.end_seq),
    }


# -- Values --

def _utc(value: datetime) -> datetime:
    # SQLite hands back naive UTC timestamps.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _instant(value) -> datetime | None:
    """A timestamp or ISO-8601 text as an aware UTC datetime; None for anything else."""
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, OverflowError):
        return None


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value) -> str | None:
    """Text of a scalar, cut to TEXT_LIMIT, with what UTF-8 cannot carry (lone surrogates) replaced."""
    if value is None or isinstance(value, (dict, list)):
        return None
    value = value if isinstance(value, str) else str(value)
    return value[:TEXT_LIMIT].encode("utf-8", "replace").decode("utf-8")


def _number(value) -> Decimal | None:
    """A finite number, or its text as JSON extraction returns it; None for anything else (booleans too)."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            number = Decimal(value.strip())
        elif isinstance(value, (int, float, Decimal)):
            number = Decimal(value)
        else:
            return None
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _integer(value) -> int | None:
    """A whole number within BIGINT, else None."""
    number = _number(value)
    if number is None or abs(number) > _BIGINT_MAX or number != number.to_integral_value():
        return None
    return int(number)


def _float(value) -> float | None:
    number = _number(value)
    if number is None:
        return None
    result = float(number)
    return result if math.isfinite(result) else None


def _credits(value) -> str | None:
    """Credits as plain decimal text at the billing precision (DECIMAL(38,12)), else None."""
    number = _number(value)
    if number is None or abs(number) >= _CREDITS_BOUND:
        return None
    return format(number.quantize(_CREDITS_SCALE, context=_DECIMAL), "f")


def _add(total: int | None, value: int | None) -> int | None:
    return value if total is None else total if value is None else total + value
