# Trajectory analytics exports (plan phase 5)

Daily Parquet files of per-session, per-request and per-tool-call facts, built from the trace database by
`backend/trajectory/analytics/`. They answer usage, cost, latency and failure questions from a laptop with DuckDB,
without load on the production database. ClickHouse is not deployed; the trigger for it is below.

## Command (WAVE3 contract 4)

```sh
python -m trajectory.analytics export --date YYYY-MM-DD [--dry-run]
```

It runs in the backend image with the worker's environment: the trace database is `TRAJECTORY_DATABASE_URL` (the
embedded SQLite default outside external mode) and objects go through the trajectory blob store settings. A run
writes three objects:

```
{TRAJECTORY_ANALYTICS_PREFIX}dt=YYYY-MM-DD/sessions.parquet
{TRAJECTORY_ANALYTICS_PREFIX}dt=YYYY-MM-DD/requests.parquet
{TRAJECTORY_ANALYTICS_PREFIX}dt=YYYY-MM-DD/tools.parquet
```

It prints one JSON summary line on stdout. Messages go to stderr.

```json
{"date":"2026-09-14","dry_run":false,"objects":3,"rows":{"sessions":12,"requests":340,"tools":512},"bytes":48211,"duration_ms":812}
```

- `objects`: objects written; 0 in a dry run.
- `bytes`: total size of the three files built.
- After a failed export the line is still printed. It adds `"error": "<exception type>"`, and `objects` counts the
  objects written before the failure. Messages are never printed, because database and storage errors can carry
  statements or signed URLs.

| exit status | meaning |
|---|---|
| 0 | exported (or built, with `--dry-run`) |
| 1 | the export failed: database, DuckDB or upload error |
| 2 | usage or configuration error (see below); nothing was read or written |

Configuration errors are:

- a malformed `--date`, or one after the current UTC day;
- no trace database URL in external mode, a URL that names the business `DATABASE_URL`, or a missing SQLite file;
- an invalid `TRAJECTORY_ANALYTICS_PREFIX`, or one inside the trajectory namespace;
- an unsupported blob provider, or OSS without a bucket.

Behavior:

- **Dry run.** `--dry-run` reads the day and builds the three files, then uploads nothing and counts no metric. Use
  it to check the configuration and the row counts.
- **Reruns.** Every run writes all three objects with overwrite, empty tables included, so a rerun replaces the
  date as a whole and leaves other dates alone. Rows are written in a fixed order, so rebuilding unchanged data
  writes identical files.
- **Metrics.** A run that uploads counts `analytics_exports` on success or `analytics_export_failures` on failure
  (WAVE3 contract 5). The counters live in the metrics registry of the process that runs the export; see
  [Operations](#operations).

### Settings

| variable | default | notes |
|---|---|---|
| `TRAJECTORY_ANALYTICS_PREFIX` | `analytics/trajectories/` | Key prefix of the files. Leading and trailing `/` are ignored. It must be a valid blob key prefix outside the trajectory namespace (`TRAJECTORY_OSS_PREFIX`, default `trajectories/`), where retention and GC treat every directory as a trajectory. |

Existing settings it reads:

- `TRAJECTORY_DATABASE_URL`, `TRAJECTORY_WORKER_MODE`: through `WorkerSettings`.
- `TRAJECTORY_BLOB_PROVIDER`, `TRAJECTORY_BLOB_LOCAL_PATH`, `TRAJECTORY_OSS_BUCKET`, `TRAJECTORY_OSS_REGION`,
  `TRAJECTORY_OSS_ENDPOINT`, `TRAJECTORY_OSS_INTERNAL`, `TRAJECTORY_OSS_PREFIX`, `TRAJECTORY_BLOB_FAULT`.
- `DATABASE_URL`: only compared with the trace URL.
- `TMPDIR`: where the build directory is created.

Fixed limits (module constants):

| limit | value |
|---|---|
| PostgreSQL statement timeout | 60 s |
| page size | 500 sessions, and 500 records per page of their records |
| DuckDB | 256 MB memory limit, 1 thread |
| Parquet | zstd, row groups of 50 000 rows |

## What a day contains

Days are UTC days, `[00:00, 24:00)`. `dt` is that day.

- **`requests`**: request records that started on the day.
- **`tools`**: tool-call records that started on the day. A call that never started (denied or rejected) counts by
  the time it was requested, else by the time it finished.
- **`sessions`**: one row per live root session that started or was last active on the day, or has a row in that
  day's `requests` or `tools` file. Live means not tombstoned and not deleted in the metadata replica.

Values are as of the export:

- A call still open at export time keeps its open status (`pending`, `running`, `streaming`).
- Whole-session statistics include activity after the day.
- Rerun a date to refresh it.

Deleted sessions are left out of exports made after the deletion. Files written earlier are not rewritten; they hold
no content.

**Never exported:** titles, previews, prompts, messages, tool arguments, outputs, error messages, status reasons,
user names, emails and workspace names. Users, workspaces and sessions appear only as opaque ids.

## Schema

Types are DuckDB names:

- `TIMESTAMPTZ` is stored in Parquet as a UTC timestamp in microseconds.
- `DECIMAL(38,12)` keeps the billing precision of credits.
- Every column is nullable.

### `sessions.parquet`

Order: `started_at, trajectory_id`.

| column | type | meaning |
|---|---|---|
| `trajectory_id` | VARCHAR | `session_trajectories.id` |
| `session_id` | VARCHAR | root session |
| `user_id` | VARCHAR | owner |
| `workspace_id` | VARCHAR | workspace (`""` when ingest did not know it) |
| `project_id` | VARCHAR | from the metadata replica |
| `session_kind` | VARCHAR | session `kind`, from the metadata replica |
| `agent` | VARCHAR | agent name, from the metadata replica |
| `model` | VARCHAR | model of the latest started request (session summary) |
| `started_at` | TIMESTAMPTZ | first ingested event |
| `last_activity_at` | TIMESTAMPTZ | latest ingested event |
| `recording_status` | VARCHAR | `recording`, `gap`, `paused`, `expired` |
| `running_status` | VARCHAR | `idle`, `running`, `waiting`, `error`; null before the first projection |
| `budget_level` | VARCHAR | `normal`, `degraded`, `blocked` |
| `event_count` | BIGINT | events ingested |
| `stored_bytes` | BIGINT | compressed bytes in blobs and segments |
| `committed_seq`, `projected_seq` | BIGINT | ingest and projection watermarks; the statistics below cover events through `projected_seq` |
| `request_count`, `tool_count` | BIGINT | whole session (session summary statistics); null before the first projection |
| `error_count` | BIGINT | whole-session requests and tool calls with status `failed`, `denied` or `timed_out` |
| `unknown_count` | BIGINT | whole-session requests and tool calls left `unknown` by an interruption or a recording gap |
| `input_tokens`, `output_tokens` | BIGINT | whole session, over requests with usage; null when no request has usage |
| `usage_complete` | BOOLEAN | every request of the session has usage |
| `day_request_count`, `day_tool_count` | BIGINT | this session's rows in the day's `requests` and `tools` files |
| `day_error_count` | BIGINT | of those rows, status `failed`, `denied` or `timed_out` |
| `day_input_tokens`, `day_output_tokens` | BIGINT | sums over the day's requests; null when none has usage |
| `day_credits` | DECIMAL(38,12) | sum over the day's requests; null when none has credits |

### `requests.parquet`

Order: `started_at, trajectory_id, start_seq`.

| column | type | meaning |
|---|---|---|
| `trajectory_id`, `session_id`, `user_id`, `workspace_id` | VARCHAR | owning root session |
| `request_id` | VARCHAR | model request |
| `source_session_id` | VARCHAR | session that made the request (a delegated child session, else the root) |
| `run_id`, `turn_id`, `step_id` | VARCHAR | execution identity |
| `agent_id`, `parent_agent_id` | VARCHAR | agent that made the request, and its parent |
| `parent_call_id` | VARCHAR | tool call that made the request (tool-side model calls) |
| `status` | VARCHAR | `completed`, `failed`, `cancelled`, `timed_out`, `unknown`; `pending`, `running` or `streaming` while open |
| `model` | VARCHAR | model id as called |
| `provider` | VARCHAR | provider part of the model id (`request.prepared`) |
| `purpose` | VARCHAR | request purpose recorded by the producer (the billing kind, for example `chat`) |
| `attempt` | BIGINT | attempt number |
| `finish_reason` | VARCHAR | provider finish reason |
| `error_type` | VARCHAR | exception type of a failed request |
| `started_at`, `finished_at` | TIMESTAMPTZ | |
| `duration_ms` | DOUBLE | producer-measured duration, else `finished_at - started_at` (see `timing_source`) |
| `ttft_ms` | DOUBLE | time to first output |
| `timing_source` | VARCHAR | `producer_monotonic` or `session_timestamps` |
| `chunk_count` | BIGINT | streamed chunks |
| `input_tokens`, `output_tokens` | BIGINT | first of `input_tokens`, `prompt_tokens`, `input` (likewise for output), as the session statistics read usage |
| `cache_read_tokens`, `cache_write_tokens` | BIGINT | `cache_read` / `cache_write` of the billing normalization, else the provider names |
| `credits` | DECIMAL(38,12) | credits settled by the billing ledger for the request |
| `start_seq`, `end_seq` | BIGINT | first and closing event of the record in the trajectory |

### `tools.parquet`

Order: `coalesce(started_at, requested_at, finished_at), trajectory_id, start_seq`.

| column | type | meaning |
|---|---|---|
| `trajectory_id`, `session_id`, `user_id`, `workspace_id` | VARCHAR | owning root session |
| `call_id` | VARCHAR | tool call |
| `request_id` | VARCHAR | model request that asked for the call |
| `source_session_id`, `run_id`, `turn_id`, `step_id`, `agent_id` | VARCHAR | execution identity |
| `parent_call_id` | VARCHAR | enclosing tool call |
| `tool` | VARCHAR | tool id (`tool`, else `name` or `tool_name` of the record data) |
| `status` | VARCHAR | `completed`, `failed`, `denied`, `cancelled`, `timed_out`, `unknown`; `pending`, `running` or `waiting` while open |
| `error_type` | VARCHAR | exception type of a failed call |
| `requested_at`, `started_at`, `finished_at` | TIMESTAMPTZ | `started_at` is null for a call that never ran |
| `duration_ms` | DOUBLE | execution time measured by the producer, else `finished_at - started_at`; null for a call that never ran |
| `total_duration_ms` | DOUBLE | producer-measured time including permission checks and hooks |
| `timing_source` | VARCHAR | `producer_monotonic` or `session_timestamps` |
| `start_seq`, `end_seq` | BIGINT | first and closing event of the record in the trajectory |

## How a run works

### 1. Reading the trace database

All reads run in one read-only transaction.

- **PostgreSQL:** the transaction is `REPEATABLE READ` (one snapshot for all three tables) and runs
  `SET LOCAL statement_timeout = '60s'`, because the trace role's 5 s default is for request-serving reads
  (WAVE3 contract 2).
- **Candidate sessions:** trajectories whose `started_at .. last_activity_at` overlaps the day. They are read in
  pages of 500 by trajectory id.
- **Records:** each page is followed by the `request` and `tool` records of its trajectories, in pages of 500 by
  `(trajectory_id, record_id)`.
- **What is read:** record JSON is reduced to the scalars above inside the database (`jsonb_extract_path_text` on
  PostgreSQL, `json_extract` on SQLite).
- **Day filter:** it compares the projector's ISO-8601 UTC time text under `COLLATE "C"`, then Python checks the
  parsed instant.
- **Memory:** Python holds one page at a time.

### 2. Staging

Rows are appended as JSON lines to a build directory `openbox-analytics-*` in the temp directory. Every line must
carry exactly its table's columns. The directory is removed when the run ends, and a directory older than a day
(left by a killed run) is removed by the next run.

### 3. Building the Parquet files

An in-memory DuckDB connection reads the JSON lines with the explicit column types above and writes each file with
an ordered `COPY ... (FORMAT parquet, COMPRESSION zstd)`. It runs with:

- `memory_limit='256MB'`, `threads=1`, and spilling to the build directory;
- `autoinstall_known_extensions` and `autoload_known_extensions` off, and `allow_community_extensions=false`;
- `allowed_directories` set to the build directory, then `enable_external_access=false`. From then on `INSTALL`,
  `LOAD`, HTTP and S3 reads and every file outside the directory are refused.
- `lock_configuration=true`, so none of this can be changed again.

The parquet and json extensions are built into the Python wheel, so nothing is ever downloaded.

### 4. Uploading

The three files are read one at a time and put with overwrite, content type `application/vnd.apache.parquet`.

## Querying from a laptop with DuckDB

1. Copy the files. From OSS, with read access to the prefix, use for example
   `ossutil cp -r oss://<bucket>/analytics/trajectories/ ./trajectory-analytics/`. A desktop or dev backend (local
   blob provider) already has them under `TRAJECTORY_BLOB_LOCAL_PATH/analytics/trajectories/`.
2. Open DuckDB (`duckdb` CLI, or `python -c "import duckdb"`) in that directory's parent and define views. `dt`
   comes from the directory names:

```sql
CREATE VIEW sessions AS SELECT * FROM read_parquet('trajectory-analytics/dt=*/sessions.parquet', hive_partitioning = true);
CREATE VIEW requests AS SELECT * FROM read_parquet('trajectory-analytics/dt=*/requests.parquet', hive_partitioning = true);
CREATE VIEW tools    AS SELECT * FROM read_parquet('trajectory-analytics/dt=*/tools.parquet', hive_partitioning = true);
SET TimeZone = 'Asia/Shanghai';  -- display only; dt stays the UTC day
```

Tokens, credits and latency by model per day:

```sql
SELECT dt, model, count(*) AS requests,
       sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens, sum(credits) AS credits,
       quantile_cont(ttft_ms, 0.5) AS ttft_p50_ms, quantile_cont(duration_ms, 0.95) AS duration_p95_ms,
       avg((status IN ('failed', 'timed_out'))::INT) AS failure_rate
FROM requests GROUP BY ALL ORDER BY dt, credits DESC NULLS LAST;
```

Tool failures and slow tools over the last week:

```sql
SELECT tool, count(*) AS calls,
       count(*) FILTER (WHERE status IN ('failed', 'denied', 'timed_out')) AS errors,
       quantile_cont(duration_ms, 0.95) AS p95_ms,
       mode(error_type) AS common_error
FROM tools WHERE dt >= current_date - 7 GROUP BY ALL ORDER BY errors DESC;
```

Active users and sessions per day:

```sql
SELECT dt, count(DISTINCT user_id) AS users, count(*) AS sessions,
       sum(day_request_count) AS requests, sum(day_credits) AS credits
FROM sessions GROUP BY dt ORDER BY dt;
```

A session has one `sessions` row per active day:

- To sum across days, use the `day_*` columns.
- For whole-session totals, take the latest `dt` of each `trajectory_id`:
  `SELECT * FROM sessions QUALIFY row_number() OVER (PARTITION BY trajectory_id ORDER BY dt DESC) = 1`.

## ClickHouse trigger (plan §10)

Parquet files and DuckDB stay the analytics store until either condition holds:

1. **More than 1 million events per day.**
   - Measured by the worker gauge `events_ingested_24h` (WAVE3 contract 5), pushed to CloudMonitor by
     `trajectory.ops.cms`.
   - The ClickHouse trigger alarm (w3-ops) fires when it exceeds 1 000 000.
   - At that volume the per-trajectory PostgreSQL design, the daily export and laptop-sized copies stop being
     comfortable.
2. **Cross-session full-text search with second-level aggregation.**
   - This means searching content across all sessions and aggregating the matches within seconds.
   - Neither store can serve it: the trace database only searches inside one session (`search_doc`, trigram index),
     and these files hold no content.
   - This condition is a product decision, not a metric.

When either holds, ClickHouse is designed and deployed as its own work package. The Parquet history loads as it is
(ClickHouse `file()` or `s3()` table functions). The types map directly:

| Parquet (DuckDB) | ClickHouse |
|---|---|
| `TIMESTAMPTZ` | `DateTime64(6, 'UTC')` |
| `DECIMAL(38,12)` | `Decimal(38, 12)` |
| `BIGINT` | `Int64` |
| `DOUBLE` | `Float64` |
| `VARCHAR` | `String` |
| `BOOLEAN` | `Bool` |

## Operations

- **Schedule.** Run the command once a day for the previous UTC day (the w3-ops analytics timer). A later rerun of
  the same date refreshes calls that were still open.
- **Monitoring.** The command runs in its own process, so its counters do not reach the worker's `/metrics` unless
  the export runs inside the worker process. For the timer-run command, the exit status and the summary line are
  the primary signals.
- **Access.**
  - The OSS identity needs `PutObject` on the analytics prefix.
  - Readers need `GetObject` and `ListObjects` on it.
  - The trace database role needs only `SELECT`.
- **Retention.** The files hold ids and counts only, a few kilobytes to megabytes per day. No lifecycle rule covers
  the prefix; the data is kept until someone deletes it.
