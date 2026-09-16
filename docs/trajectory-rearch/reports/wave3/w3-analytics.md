# w3-analytics report

Final report for w3-analytics. The code is finished and committed, and the working tree is clean. As the orchestrator asked, I stopped the full unit suite and ran no further tests.

**Branch:** `wp/w3-analytics`, created from `05bc977`.

**Head:** `2a3aed0e181b020002feb930a09f9224c70077e1`. Commits: `f25bd48` adds duckdb, `f28d4c6` adds the package, tests and docs, `2a3aed0` is a doc fix.

## Summary
- **Command:** `python -m trajectory.analytics export --date YYYY-MM-DD [--dry-run]` works as contract 4 describes. It exits 0 on success, 1 on failure and 2 on usage or configuration errors. It prints one JSON summary line, and prints it after a failure too.
- **Reading:** it reads only the trace database, in one read-only transaction. On PostgreSQL that transaction is `REPEATABLE READ` and runs `SET LOCAL statement_timeout = '60s'`. Rows are read in pages of 500, and only IDs, statuses, names, times and numbers are selected. Titles, previews, arguments, outputs and error messages are never read.
- **Which rows belong to a day (UTC):**
  - A request counts on the day it started.
  - A tool call counts on the day it started, else the day it was requested, else the day it finished.
  - A session is included if it started or was last active on the day, or has a request or tool call on it.
  - Deleted sessions are left out.
- **Building:** rows are written to JSON lines in a temporary directory. An in-memory DuckDB (256 MB, 1 thread) turns them into ordered, zstd-compressed Parquet files. DuckDB can only touch the temporary directory: extensions cannot be installed or loaded, network and outside files are refused, and the configuration is locked.
- **Upload:** all three files are written with overwrite to `{prefix}dt=YYYY-MM-DD/{sessions,requests,tools}.parquet`, empty tables included. A rerun replaces the date, and unchanged data produces identical files.
- **Metrics:** runs that upload count `analytics_exports` or `analytics_export_failures`.
- **Columns:** I chose them from what the projector actually writes; the full schema is in `ANALYTICS.md`. Request durations use the producer's own measurement when it was recorded. The projector recalculates a request's duration from timestamps once the billing usage event arrives, but the record data still holds the measured value.
- **Docs:** `ANALYTICS.md` covers the command, the schema of the three files, querying from a laptop with DuckDB, and the ClickHouse trigger: more than 1 million events a day, or cross-session full-text search with second-level aggregation.
- **Dependency:** `duckdb` 1.5.5 has a prebuilt cp312 `manylinux_2_28_x86_64` wheel, so the python:3.12-slim image needs no compiler.

## Changed files
All inside my ownership:
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-a7d7603c54eeb4564/backend/trajectory/analytics/` (`__init__.py`, `__main__.py`, `export.py`, `source.py`, `parquet.py`, `schema.py`)
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-a7d7603c54eeb4564/backend/tests/unit/test_trajectory_analytics.py`
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-a7d7603c54eeb4564/docs/trajectory-rearch/ANALYTICS.md`
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-a7d7603c54eeb4564/backend/pyproject.toml`
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-a7d7603c54eeb4564/backend/uv.lock`

## Tests
- `cd backend && uv run pytest tests/unit/test_trajectory_analytics.py -q`: **21 passed, 0 failed, 0 skipped.** This ran once on the final code; only one doc sentence changed afterwards.
- The tests seed SQLite through the real projection harness, export to a memory blob store and read the Parquet files back with DuckDB. They cover a dry run, a rerun overwriting the same date, and a failed upload exiting 1.
- The gate `cd backend && uv run pytest tests/unit -q` was **not run**: I started it and stopped it on the orchestrator's instruction, so there are no counts.

## New settings
- `TRAJECTORY_ANALYTICS_PREFIX`, default `analytics/trajectories/`. It is refused if it falls inside the trajectory key namespace.
- The 60 s statement timeout, page size of 500, DuckDB limits and row-group size are constants, not settings.

## Deviations
1. The full unit gate was not run (orchestrator instruction).
2. The summary line always includes `dry_run` and adds `error` (the exception type only) after a failure.
3. A dry run builds the files but uploads nothing, reports `objects: 0` and counts no metric.
4. These inputs also exit 2:
   - a date after the current UTC day;
   - a trace database URL that names the business database;
   - a missing SQLite file.

## Open issues
1. **Metrics never reach the worker.** The command runs in its own process, so its two counters are lost when it exits and never appear in the worker's `/metrics`. Integration needs a bridge: for example, w3-ops turns the exit status or summary line into a `cms put`, or the export runs inside the worker.
2. **PostgreSQL SQL is unexecuted.** It was only compiled in a unit test; the JSONB extraction, `COLLATE "C"`, row-value paging and the read-only / timeout statements have never run against PostgreSQL.
3. **OSS access is unverified.** No one has checked that the gw2 access key can write under `analytics/trajectories/`; the earlier preflight covered only `trajectories/` and `backups/postgres/`. No OSS lifecycle rule covers the new prefix.
4. **Metric names are unregistered.** Until w3-harden-service registers them, the registry logs a one-time "unknown counter" warning.
5. **Parquet and JSON support on Linux is unchecked.** I confirmed only on the macOS wheel that DuckDB includes them. If the Linux wheel lacked them, the export would fail with exit 1 rather than download anything.
6. **Large reads.** Long-lived sessions have all their request and tool records scanned for each export, because no index covers record times. Backfilling an old date scans every session active since that date.
