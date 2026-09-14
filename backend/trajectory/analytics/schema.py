"""Columns of the three analytics files (docs/trajectory-rearch/ANALYTICS.md).

A staged row carries exactly its table's columns and DuckDB reads them with
these types, so the schema of a file never depends on type inference or on
the values one day happens to hold.
"""

TABLES = ("sessions", "requests", "tools")

#: Credits keep the billing precision (billing.pricing.PRECISION, 12 decimal places).
CREDITS_TYPE = "DECIMAL(38,12)"

SESSIONS = (
    ("trajectory_id", "VARCHAR"),
    ("session_id", "VARCHAR"),
    ("user_id", "VARCHAR"),
    ("workspace_id", "VARCHAR"),
    ("project_id", "VARCHAR"),
    ("session_kind", "VARCHAR"),
    ("agent", "VARCHAR"),
    ("model", "VARCHAR"),
    ("started_at", "TIMESTAMPTZ"),
    ("last_activity_at", "TIMESTAMPTZ"),
    ("recording_status", "VARCHAR"),
    ("running_status", "VARCHAR"),
    ("budget_level", "VARCHAR"),
    ("event_count", "BIGINT"),
    ("stored_bytes", "BIGINT"),
    ("committed_seq", "BIGINT"),
    ("projected_seq", "BIGINT"),
    # Whole-session statistics as projected when the export ran.
    ("request_count", "BIGINT"),
    ("tool_count", "BIGINT"),
    ("error_count", "BIGINT"),
    ("unknown_count", "BIGINT"),
    ("input_tokens", "BIGINT"),
    ("output_tokens", "BIGINT"),
    ("usage_complete", "BOOLEAN"),
    # The session's rows in this day's requests and tools files.
    ("day_request_count", "BIGINT"),
    ("day_tool_count", "BIGINT"),
    ("day_error_count", "BIGINT"),
    ("day_input_tokens", "BIGINT"),
    ("day_output_tokens", "BIGINT"),
    ("day_credits", CREDITS_TYPE),
)

#: The owning root session, first in the request and tool files.
OWNER = (
    ("trajectory_id", "VARCHAR"),
    ("session_id", "VARCHAR"),
    ("user_id", "VARCHAR"),
    ("workspace_id", "VARCHAR"),
)

REQUESTS = OWNER + (
    ("request_id", "VARCHAR"),
    ("source_session_id", "VARCHAR"),
    ("run_id", "VARCHAR"),
    ("turn_id", "VARCHAR"),
    ("step_id", "VARCHAR"),
    ("agent_id", "VARCHAR"),
    ("parent_agent_id", "VARCHAR"),
    ("parent_call_id", "VARCHAR"),
    ("status", "VARCHAR"),
    ("model", "VARCHAR"),
    ("provider", "VARCHAR"),
    ("purpose", "VARCHAR"),
    ("attempt", "BIGINT"),
    ("finish_reason", "VARCHAR"),
    ("error_type", "VARCHAR"),
    ("started_at", "TIMESTAMPTZ"),
    ("finished_at", "TIMESTAMPTZ"),
    ("duration_ms", "DOUBLE"),
    ("ttft_ms", "DOUBLE"),
    ("timing_source", "VARCHAR"),
    ("chunk_count", "BIGINT"),
    ("input_tokens", "BIGINT"),
    ("output_tokens", "BIGINT"),
    ("cache_read_tokens", "BIGINT"),
    ("cache_write_tokens", "BIGINT"),
    ("credits", CREDITS_TYPE),
    ("start_seq", "BIGINT"),
    ("end_seq", "BIGINT"),
)

TOOLS = OWNER + (
    ("call_id", "VARCHAR"),
    ("request_id", "VARCHAR"),
    ("source_session_id", "VARCHAR"),
    ("run_id", "VARCHAR"),
    ("turn_id", "VARCHAR"),
    ("step_id", "VARCHAR"),
    ("agent_id", "VARCHAR"),
    ("parent_call_id", "VARCHAR"),
    ("tool", "VARCHAR"),
    ("status", "VARCHAR"),
    ("error_type", "VARCHAR"),
    ("requested_at", "TIMESTAMPTZ"),
    ("started_at", "TIMESTAMPTZ"),
    ("finished_at", "TIMESTAMPTZ"),
    ("duration_ms", "DOUBLE"),
    ("total_duration_ms", "DOUBLE"),
    ("timing_source", "VARCHAR"),
    ("start_seq", "BIGINT"),
    ("end_seq", "BIGINT"),
)

COLUMNS = {"sessions": SESSIONS, "requests": REQUESTS, "tools": TOOLS}

#: Row order inside each file. Each key is unique per row (a record's start_seq
#: is unique per kind within a trajectory), so rebuilding the same data writes
#: the same rows in the same order.
ORDER = {
    "sessions": "started_at, trajectory_id",
    "requests": "started_at, trajectory_id, start_seq",
    "tools": "coalesce(started_at, requested_at, finished_at), trajectory_id, start_seq",
}
