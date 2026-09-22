# Wave 3 work packages (trajectory re-architecture, code only)

> **2026-09-15:** analytics (`w3-analytics`, contract 4, the export timer and its alarm) was reverted and the legacy converter removed; old recordings are not kept. See "Scope changes after v1" at the top of [SPEC.md](SPEC.md).

Base: the `feat/trajectory-rearch` commit that adds this file, on top of the wave-2 integration `05bc977`; every prompt names the exact sha. `w3-frontend` and `w3-ops` started earlier from `7c6b5ba` because wave 2 did not touch their files. Contract: `docs/trajectory-rearch/SPEC.md`; wave-1 decisions: `docs/trajectory-rearch/reports/wave1/NOTES.md`; wave-2 integration notes: `docs/trajectory-rearch/reports/wave2/INTEGRATION.md`. Where this file refines SPEC, this file wins.

**Scope decision (user, 2026-09-14):** finish the remaining production code fast. Every package writes unit tests for the code it adds and runs `cd backend && uv run pytest tests/unit -q` (frontend: `cd frontend-v2 && npx vitest run` and `npm run test:nginx`). No new integration, end-to-end, performance, chaos or load tests; no Playwright runs; no local release rehearsal; the 88 quarantined legacy trajectory tests stay quarantined.

## Rules
- Work in your own git worktree on branch `wp/<id>` created with `git checkout -b wp/<id> <base sha>` (never `-B`; stop and report if the branch exists).
- Change only the files your package owns (matrix below). An unavoidable change elsewhere must be minimal and listed in the report. Do not edit SPEC §13; list new settings in the report.
- Commit early and often; every message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Do not push. Never use bare `git stash`. Never access production, the Aliyun CLI, OSS or any cloud API. No new dependencies except the grant below. Keep untouched behavior unchanged and follow the surrounding style.
- Final answer: branch, head sha, summary, changed files, unit test command with pass/fail/skip counts, new settings, deviations, open issues.

## Shared contracts
1. **Spool format v2** (`w3-spool-blobs`): lines may carry `{"$blob": "<sha256>"}` values resolved from `blobs/<sha256>` in the spool directory; the worker reads v1 and v2.
2. **Trace role and timeouts**: production connects to `openbox_trace` as role `openbox_trace` with role defaults `statement_timeout = '5s'` and `work_mem = '32MB'` (`w3-ops`). Every operation that may legitimately take longer sets `SET LOCAL statement_timeout` explicitly: partition maintenance, purges, archive deletes, trace migrations env, legacy converter (`w3-harden-worker`), analytics queries (`w3-analytics`).
3. **Redis channel**: trajectory hints are published on `trajectory:hints` only; the worker no longer subscribes to or decodes `bus:events` (`w3-harden-service`).
4. **Analytics CLI** (`w3-analytics` ↔ `w3-ops`): `python -m trajectory.analytics export --date YYYY-MM-DD [--dry-run]` prints one JSON summary line (`date`, `objects`, `rows` per table, `bytes`, `duration_ms`), exits 0 on success, 1 on failure, 2 on usage or configuration errors, writes `analytics/trajectories/dt=YYYY-MM-DD/{sessions,requests,tools}.parquet` (prefix `TRAJECTORY_ANALYTICS_PREFIX`) through the trajectory blob store settings, and overwrites that date on rerun.
5. **New metric names** (worker `/metrics`, pushed by `trajectory.ops.cms`): counters `blob_put_raw_bytes`, `audit_dead_letters`, `analytics_exports`, `analytics_export_failures`, `failed_batches`; gauges `events_ingested_24h`, `hot_partitions`, `budget_degraded_trajectories`, `budget_degraded_users`. If `trajectory/worker/metrics.py` restricts names, `w3-harden-service` registers all of them.
6. **Recording-state epoch** (`w3-harden-producers` emits, `w3-harden-worker` applies): every `recording.state` control carries `epoch` (integer, the SPEC §5.6 period epoch of the root session); the worker applies a control only when its epoch is newer than the trajectory's last applied recording epoch. Controls without `epoch` keep the current status-based rule.

## Ownership matrix
| id | owns |
|---|---|
| w3-spool-blobs | `trajectory/{emitter,spool,budget}.py`, `trajectory/worker/{spool_reader,content}.py`, SPEC §3, their unit tests |
| w3-harden-worker | `trajectory/worker/{ingest,services,budgets,meta,projection,archive,retention,lock,settings}.py`, `trajectory/{lifecycle,export,repository,segments,projector}.py`, `trajectory/store/**`, `trajectory/tools/**`, their unit tests |
| w3-harden-service | `trajectory/worker/{app,routes,ws,metrics,embedded,notify,__main__}.py`, `trajectory/auth.py`, `trajectory/payload.py`, `bus/**` (trajectory channel only), `api/internal.py`, `backend/scripts/trajectory_dev_server.py`, `trajectory/benchmark*.py` except `benchmark_emit.py`, their unit tests |
| w3-harden-producers | `trajectory/{artifacts,meta_sync,producers,jobs,files}.py`, `session/session.py` (non-fence parts), `session/fork.py`, `session/revert.py`, `agent/trajectory.py` (non-fence parts), producer call sites in `tool/**` and `cron/**`, their unit tests |
| w3-frontend | `frontend-v2/**`, `scripts/test-nginx.mjs` |
| w3-ops | `deploy/**`, `k8s/**`, `docs/operations/DEPLOY.md`, `backend/trajectory/ops/**` except `rebuild.py`, `backend/tests/unit/test_trajectory_ops_*.py`, SPEC §12 |
| w3-analytics | `backend/trajectory/analytics/**` (new), `backend/pyproject.toml` and `backend/uv.lock` (grant: `duckdb`), its unit tests, `docs/trajectory-rearch/ANALYTICS.md` |

## Package scopes

### w3-spool-blobs (plan 2.1)
- **Writer thread (externalize):**
  - Moves these values into `blobs/<sha256>`:
    - the `request.prepared` `system`, `tools` and each `messages[i]` whose canonical JSON exceeds `TRAJECTORY_SPOOL_BLOB_MIN_BYTES` (default 1024);
    - any other value over 16 KiB.
  - How a blob is written: temp file, then atomic rename. If the blob already exists, refresh its mtime instead.
  - The line carries `{"$blob": sha}` in place of the value.
  - The spool budget counts blob bytes.
- **Worker (resolve):**
  - Resolves `$blob` in `spool_reader`/`content` before sanitizing and hashing, so the sha is the same as for inline content.
  - A line whose blob is missing or corrupt becomes a gap and never stops the file.
- **Cleanup:** the worker deletes blob files whose mtime is older than the oldest unconsumed spool file, minus a margin.
- **Format:**
  - Spool format version 2; version 1 files are still read.
  - Update SPEC §3.
- **Constraints:**
  - `emit()` stays a non-blocking queue put.
  - Make the two timing-sensitive emitter writer unit tests deterministic.

### w3-harden-worker
- **Projection memory:**
  - Batches bounded by bytes as well as events.
  - `statistics.unsupported_events` bounded (keep a count).
  - Reads that replay while projection lags yield to the event loop.
- **Exports:**
  - Upload from the temporary file with a streaming body, and verify with streaming read-back hashing.
  - Check payload and asset sizes before reading bytes.
  - Delete stale `openbox-export-*` temp files at start.
  - `read_export` holds no trace DB session during the download.
- **Archive:**
  - Candidates that fail permanently back off exponentially and cannot starve the others.
  - Uploads orphaned by a concurrent tombstone are queued for GC.
- **Ingest:**
  - A file batch that fails `TRAJECTORY_INGEST_MAX_BATCH_FAILURES` (default 10) consecutive times is quarantined with a `recording.gap`, so the producer's later files proceed (`failed_batches`).
  - Content bound to an asset missing from the meta replica never blocks ingest indefinitely: bounded wait, then recorded with explicit availability.
- **Controls:** contract 6 on the worker side in `meta.py`.
- **Tombstones:** a tombstone publishes the deleted notification once. Today it is published twice, by ingest and by lifecycle.
- **Legacy converter:**
  - Takes the writer lock and refuses to run while a worker holds it.
  - New `--purge-legacy-blobs [--dry-run]`, allowed only after a successful `--finalize-drop`. It deletes exactly the legacy local blob files it migrated and prints counts and bytes.
- **Contract 2:** explicit `SET LOCAL statement_timeout` wherever needed.
- **Daily report and metrics:**
  - One structured log line per day: trajectories expired, tombstones processed, objects and bytes deleted, GC failures, degraded trajectories and users.
  - Gauges `hot_partitions`, `budget_degraded_trajectories`, `budget_degraded_users`, `events_ingested_24h`.
  - Counter `blob_put_raw_bytes`.
- **Business database:** the worker never reads `DATABASE_URL`.
- Projection and checkpoint blob writes already hold the object guard (wave-2 integration `ed7c3e7`).

### w3-harden-service
- **Contract 3:** the publisher is `notify.py`; the worker subscribes only to `trajectory:hints`.
- **Downloads with bounded memory** (payload, blob and asset):
  - Spool the object to a temporary file in chunks.
  - Revalidate the viewer and the payload/asset state after the read, exactly as today.
  - Then stream the file. A revoked read sends no bytes.
- **Export downloads:** validate once, through `export.validate_export`, keeping the route's current status and error mapping. Today they are validated twice, in different orders.
- **Audit outbox:** after a bounded number of attempts or a maximum age, a record moves to a dead-letter state, counts `audit_dead_letters` and is logged once.
- **`assert_admin`:** checks in the old order, the mobile-session claim before account state.
- **Embedded SQLite upgrade path:**
  - A database created by `create_all` is stamped at the matching trace revision.
  - Later revisions upgrade with alembic run in a worker thread, never inside the running loop.
- **Metrics:** register the contract-5 names if required.
- **Dev server:** `scripts/trajectory_dev_server.py` runs the business app with the embedded worker.
- **Obsolete benchmarks:**
  - Delete `trajectory/benchmark.py`, `benchmark_stream.py`, `benchmark_concurrency.py` and `benchmark_process_crash.py`; they drive the removed in-transaction recorder.
  - Remove references to them.
  - Keep `benchmark_emit.py`.

### w3-harden-producers
- `artifact.recorded`: later uses of the same asset no longer produce keep-first conflicts (per-process memo or use-site ids; keep the admin projection identical).
- Meta sync catch-up stays within the SPEC §5.8 query bound per interval, including the `file_assets` rescan.
- `update_session` and other unlocked writers emit no settings facts while recording is paused.
- Contract 6: `recording.state` controls carry `epoch`.

### w3-frontend
- **Polling budget (plan 4.3):** an idle admin session page with a healthy socket issues at most 4 requests per minute.
  - Rely on WS hints.
  - Safety polls at least every 30 s for events and every 60 s for header and list.
  - Payload `?meta=1` only while a payload is shown.
  - Behavior while disconnected is unchanged.
- **nginx:**
  - The access log appends `rt=$request_time urt=$upstream_response_time` after the existing fields.
  - `location ^~ /api/internal/ { return 404; }`
  - `scripts/test-nginx.mjs` covers both.

### w3-ops
- **Example compose files:** align them with the real server files.
- **Contract 2:** trace role in `create-trace-db.sh`. The overlay URL uses the role, and the worker gets an empty `DATABASE_URL`.
- **Alarms:** archive lag, hot partitions, backend CPU/memory, business-DB `trajectory_` statements, the ClickHouse trigger, analytics failures.
- **Analytics timer.**
- **Scripts:** `drill-delete-session.sh` and `restore-check.sh`.
- **Drill defaults:** per plan.
- **RUNBOOK corrections:**
  - Merge to `origin/main` first.
  - Stale `OPENBOX_IMAGE_TAG`.
  - Full rebuild.
  - Converter run with the worker stopped.
  - `--purge-legacy-blobs`.
  - Trace role password.
  - p95/CPU comparison from the nginx `rt=` field.
  - Database size before/after.
  - `NGINX_IMAGE=nginx:1.31.5-alpine`.

### w3-analytics (plan phase 5)
`trajectory/analytics/` implements the contract-4 CLI.
- **Source:** reads only the trace DB (session summaries, request usage, tool statistics), with explicit statement timeouts and bounded memory.
- **Build:** writes Parquet with DuckDB (`memory_limit=256MB`, `threads=1`, no network extensions) in a temp directory, then uploads through the trajectory blob store settings.
- **Behavior:** idempotent per date; counts `analytics_exports` and `analytics_export_failures`.
- **Documentation:** `docs/trajectory-rearch/ANALYTICS.md` covers the schema of the three files, querying from a laptop with DuckDB, and the ClickHouse trigger (plan §10).
- **Unit tests:** SQLite plus a memory blob store; Parquet read back.

## Documented deviations from the plan
- **No one-week dual write** (plan §6.3): the legacy recorder caused the production incident and is not run again. Golden replay parity and digest verification of migrated legacy data replace it.
- **Gray period** (plan §6.3): shortened to a verified internal-user window before all users.
- **Spool full** (plan §4.4): new lines are dropped with gaps, instead of deleting the oldest files.
- **180-day deletion** (plan §4.1): enforced by worker retention on last activity, not by an OSS lifecycle rule on object age.
- **Admin identity** (plan §3.1): checked through the backend internal endpoint with a 5 s cache. The worker still never connects to the business DB.
- **ClickHouse:** not deployed. The trigger is measured and alarmed.
- **Test scope:** reduced by the user's decision. No acceptance, performance, chaos, load or end-to-end suites, and no local rehearsal. The legacy trajectory tests stay quarantined.

## Wave-3 integration order and gate
w3-spool-blobs → w3-harden-worker → w3-harden-service → w3-harden-producers → w3-analytics → w3-frontend → w3-ops. Gate: `cd backend && uv run pytest tests/unit -q` green; frontend `npx vitest run` and `npm run test:nginx` green.
