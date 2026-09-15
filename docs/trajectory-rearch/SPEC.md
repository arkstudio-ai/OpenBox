# Session trajectory re-architecture: implementation spec (v1)

Status: authoritative for all work packages on branch `feat/trajectory-rearch`.
Plan document (Chinese, product-level): "OpenBox 会话追踪改造计划" artifact. This file is the engineering contract.
Code maps of the current system (read them before changing an area): `docs/trajectory-rearch/maps/`
`recorder.full.md`, `projection.md`, `producers.md`, `api.md`, `tests.md`, `infra.full.md`, `runtime.md`.

## Scope changes after v1 (2026-09-15)

Decided during development, before any deployment. These points override the sections they name; the rest of the body is v1 text.

- **§2 switches:** `TRAJECTORY_SINK` is removed from the code; the backend always writes the spool. `TRAJECTORY_WORKER_MODE=off` disables the whole pipeline: no emitter, no metadata sync and no worker.
- **§6.9 and §8.14, old data:** old recordings are not kept. Business migration `d3b5f7a9c1e2` drops the seven trajectory tables of the business database together with the renamed copies of an earlier revision; its `downgrade()` recreates the seven tables empty, so that a main-built image, whose readiness check and session deletion still expect them, can start again. There is no converter: `trajectory.tools.migrate_legacy` is deleted.
- **§12, operations:** the worker mounts no business `blob-data` volume. There is no analytics export, timer or alarm and no `TRAJECTORY_ANALYTICS_PREFIX` (`trajectory.analytics` and duckdb were reverted): `setup-alarms.sh` creates 15 rules and the worker metrics have no `analytics_*` counters. `pg-backup.sh` has no legacy-table mode; the `openbox` dump always excludes the rows of the old trajectory tables (the schema stays). `business_trajectory_statements` counts every `pg_stat_statements` entry of `openbox` that mentions `trajectory_`.
- **§12.1 steps 6-7** (commands in `deploy/gw2/RUNBOOK.md` §5): step 6 switches the backend, whose migration drops the old tables; verify that none is left and that `alembic current` is `e5c7a9b1d3f4`, then run `SELECT pg_stat_statements_reset()` in `openbox`, because the migration's `DROP TABLE` statements match the isolation metric. Step 7 deletes the old payload files: only the `trajectories/` directory of the business `blob-data` volume (about 1.5 GB, unredacted), keeping `policies/`. A backend rollback first downgrades the business chain to `c7e9b1d3f5a7` with the new image (RUNBOOK §6).
- **§14.3 and §15.3, testing:** unit tests only; no acceptance, isolation, benchmark, chaos, end-to-end or performance suites.

---

## 0. Goals and non-negotiables

1. **Business isolation.** No trajectory SQL inside business transactions or on the business database. Business code only performs an in-memory append (serialize + enqueue). No trajectory projection, blob I/O, OSS download or DB read on business request/run paths.
2. **Fail-open recording.** Recording can never make a business operation fail, block, or change behavior. Emitters never raise. Overflow or failure drops events and produces a `recording.gap`.
3. **Fencing is not recording.** Stopping stale or superseded runs is enforced by the execution runtime (`question.runtime`) whether recording is on or off.
4. **Separate storage.** Trajectory metadata/index lives in a dedicated database (`openbox_trace`, own engine, own alembic chain). Bytes (blobs, cold event segments, checkpoints, exports) live in object storage (Aliyun OSS in production, local directory in dev/desktop). The business database holds no trajectory tables after cutover.
5. **Contract compatibility.** The admin HTTP/WS contract in `maps/api.md` §2-3 stays byte-compatible unless this spec adds an explicitly optional parameter. The admin UI replays events client-side with a TypeScript port of the projector; `/events` and `/checkpoint` must keep returning fully expanded data.
6. **Bounded growth.** Content is deduplicated (content-addressed per trajectory), compressed (zstd), budgeted per trajectory/user, archived to cold segments, and expired by retention.
7. **Operability.** Health, metrics, alarms, backups, lifecycle rules, drills and runbooks are part of the deliverable.

Everything in this spec is in scope. "Optional"/"later" items are called out explicitly; everything else must be implemented, tested and deployed.

---

## 1. Target architecture

```
business backend process (uvicorn)                           trajectory worker process (same image)
  producers ── emit()/emit_after_commit() ──► bounded queue    ┌────────────────────────────────────────┐
                                        writer thread          │ spool reader ─► ingest (seq, dedupe,   │
                                             │  JSONL append   │   sanitize, externalize, meta/control) │
                                             ▼                 │ projection (batched)                    │
                                   shared spool volume ───────►│ archive (OSS segments), retention, GC   │
  meta sync task (30 s) ── control lines ──►  (0700)           │ exports, checkpoints, budgets           │
  internal endpoints ◄── viewer introspection / audit ─────────│ admin HTTP API + WS (same contract)     │
                                                                └───────┬───────────────────┬────────────┘
                                                                        ▼                   ▼
                                                           Postgres DB openbox_trace    OSS bucket prefix
                                                           (index, hot events, records)  trajectories/
nginx (frontend container): /api/admin/trajectories/*, /ws/admin/trajectories ─► worker; everything else ─► backend
```

- Worker mode `TRAJECTORY_WORKER_MODE`: `external` (production: separate container) or `embedded` (desktop/dev: the same worker services run as asyncio tasks inside the backend process and the admin routes are mounted there) or `off`.
- Default: `external` when `JWT_SECRET` is set (server mode), `embedded` otherwise.
- The single-writer invariant: exactly one ingest/projection/archive writer per trace database, enforced by a lock (§8.1).

---

## 2. Transition switches

- `TRAJECTORY_SINK`: `db` (legacy in-transaction recorder, unchanged behavior) or `spool` (new emitter).
  - Wave 1 keeps the default `db` so the branch stays releasable and existing tests pass.
  - Wave 2 changes the default to `spool` once the worker and producer conversion are complete, and removes the legacy `db` write path together with the business trajectory tables (renamed to `legacy_trajectory_*`, see §6.9).
- `TRAJECTORY_WORKER_MODE` as above.
- Recording enablement flags stay: `TRAJECTORY_RECORDING_ENABLED`, `TRAJECTORY_RECORD_USER_IDS`, `TRAJECTORY_ADMIN_ENABLED`, `TRAJECTORY_ADMIN_USER_IDS` (backend and worker must share them).

---

## 3. Spool format v2

Version 2 adds blob values (§3.5) to version 1. Workers read files with version 1 and version 2 lines; a writer emits version 2 only for event lines that reference blobs, so deploy workers before backends.

### 3.1 Directory layout (`TRAJECTORY_SPOOL_DIR`)

```
<spool>/
  producers/<producer_id>/
      producer.json                      # {"version":1,"producer_id","boot_id","hostname","pid","role","started_at"}
      00000000000000000001.jsonl         # closed file (safe to consume)
      00000000000000000002.jsonl.part    # open file (never consumed unless abandoned, §8.2)
  blobs/
      <sha256>                           # a value moved out of an event line (§3.5); shared by all producers
      .swept                             # mtime = the worker's last blob sweep
  control/
      budgets.json                       # written by the worker (atomic rename), read by producers
      worker.json                        # worker heartbeat {"version":1,"pid","hostname","updated_at","ingest_lag_seconds"}
  quarantine/                            # files the worker cannot parse, moved here with a .reason sidecar
```

- `producer_id` = `{UTC yyyymmddHHMMSS}-{hostname}-{pid}-{8 hex}`; lexical order equals start order.
- File names are 20-digit zero-padded counters per producer, starting at 1.
- Permissions: directories `0700`, files `0600`. The spool may contain unredacted data (generic redaction runs in the worker, §8.4); it must only be mounted into the backend and worker containers.
- Consumed files are deleted by the worker after the consuming transaction commits. Blob files are deleted by the worker's sweep (§3.5).
- The JSON documents (`producer.json`, `budgets.json`, `worker.json`, `.reason`) keep `"version": 1`.

### 3.2 Line format

One JSON object per line, UTF-8, `\n` terminated, no pretty printing:

```json
{"v":1,"k":"event","n":42,"t":"2026-09-14T08:00:00.123Z","event":{...}}
{"v":2,"k":"event","n":43,"t":"2026-09-14T08:00:00.125Z","event":{..."data":{"input":{"system":{"$blob":"<sha256>"}, ...}}}}
{"v":1,"k":"control","n":44,"t":"2026-09-14T08:00:00.130Z","control":{"type":"gap", ...}}
```

- `v`: spool format version of the line: `1` when every value is inline, `2` for an event line that holds `{"$blob": "<sha256>"}` values (§3.5). Controls are always `1`. Readers accept exactly the integers 1 and 2 and must reject other versions (quarantine the file).
- `k`: `event` or `control`.
- `n`: producer-local counter, contiguous from 1 for every line the producer writes (events and controls). Dropped events do not consume a counter value; drops are reported with a `gap` control. A missing `n` observed by the worker means data loss (crash, disk error).
- `t`: enqueue time (ISO-8601 UTC, milliseconds, `Z`).
- `event`: a prepared event object (§3.3).
- `control`: a control record (§3.4).

Unknown top-level keys must be ignored by readers (forward compatibility).

### 3.3 Event object

Shape equals today's `trajectory.types.prepare()` output:

```json
{
  "type": "request.prepared", "version": 1, "event_id": "request:req_x:prepared",
  "occurred_at": "2026-09-14T08:00:00.120Z",
  "user_id": "u", "session_id": "<root session>", "source_session_id": "<source session>", "workspace_id": "w",
  "turn_id": "...", "run_id": "...", "generation": 3, "agent_id": "...", "parent_agent_id": null,
  "step_id": "...", "request_id": "...", "call_id": null, "parent_call_id": null,
  "message_id": "...", "part_id": null, "caused_by_event_id": null,
  "data": { ... }
}
```

- Null identity fields are omitted (as `TraceContext.to_dict()` does).
- `data` is JSON (NaN/Infinity become null; unsupported Python objects become `{"availability":"not_recorded","reason":"unsupported_value"}`).
- Producer-only helper keys inside `data` that the worker consumes and strips before storage:
  - `media_sources`: `{ "<sha256 of decoded media bytes>": "<file_asset_id>" }` (request capture).
  - `asset_ref`: `{ "asset_id", "oss_key", "media_type", "size_bytes", "name"? }` on `artifact.recorded` (the worker turns it into the `payload` reference).
  - `source_root_session_id` on `history.forked` (the worker adds `source_trajectory_id`, `source_through_seq`).

### 3.4 Control records

| type | fields | producer | worker action |
|---|---|---|---|
| `gap` | `reason` (`queue_overflow`,`spool_full`,`serialization_failed`,`invalid_event`,`event_too_large`,`writer_error`,`budget`; the worker's reader adds `spool_blob_missing`,`spool_blob_corrupt`, §3.5), `dropped_events`, `dropped_bytes`, `first_dropped_at`, `last_dropped_at`, `sessions`: `[{user_id, session_id, run_ids:[..≤20], request_ids:[..≤50]}]` (≤200 sessions) | emitter writer | append `recording.gap` events (§8.6) |
| `producer.goodbye` | `last_n` | emitter on graceful close | mark producer closed |
| `session.meta` | `session`: `{id,user_id,workspace_id,project_id,parent_id,kind,title,status,model,agent,is_deleted,deleted_at,created_at,updated_at}` | meta sync | upsert `trajectory_meta_sessions` |
| `user.meta` | `user`: `{id,username,email,role,is_active,is_deleted,updated_at}` | meta sync | upsert `trajectory_meta_users` |
| `workspace.meta` | `workspace`: `{id,name,updated_at}` | meta sync | upsert `trajectory_meta_workspaces` |
| `asset.meta` | `asset`: `{id,user_id,workspace_id,session_id,oss_key,mime,size,status,is_deleted,deleted_at,updated_at}` | meta sync | upsert `trajectory_meta_assets` |
| `session.deleted` | `session_id`,`user_id`,`deleted_at` | `delete_session` after commit | tombstone + purge (§8.11) |
| `asset.deleted` | `asset_id`,`user_id`,`deleted_at` | asset deletion after commit | revoke references (§8.11) |
| `recording.state` | `user_id`,`session_id` (root),`state` (`paused`/`resumed`),`reason`,`at` | session write path (§5.6) | pause/resume bookkeeping and gap events |

Controls are content-free and are written even when recording is disabled for the user.

### 3.5 Blob values (version 2)

Repeated prompt content (system prompts, tool lists, conversation history) is written once per content instead of once per request.

- **What moves** (writer thread only; `emit()` stays a non-blocking queue put):
  - `request.prepared`: `data.input.system`, `instructions` and `tools`, and each item of `data.input.messages` or of a list-valued `data.input.input`, when its compact JSON is larger than `TRAJECTORY_SPOOL_BLOB_MIN_BYTES` (1024).
  - Any other value inside `data` of any event (depth 1-6, leaves first) whose compact JSON is larger than 16 KiB. A value that already holds a reference stays inline, so blob content never contains references.
  - An event whose serialized bytes contain `"$blob"` is written inline as version 1: in a version 2 line every `{"$blob": ...}` object is a reference.
- **Blob file**: `blobs/<sha256>` holds the value's compact JSON (the exact bytes it has inline); the name is the sha256 of the content. Written as a temp file (`.<sha256>.<8 hex>.tmp`), fsynced, then renamed; the blobs directory is fsynced before a data file that references a new blob is closed. A blob that already exists gets its mtime refreshed instead, at most every 60 s per writer. Blobs are written before the line that references them; if a blob cannot be stored the event is written inline.
- **Spool budget**: `spool_max_bytes` (§5.2) counts the allocated size (`max(st_size, st_blocks × 512)`) of the files in the producer directories, `blobs/` and `quarantine/`; an event line is dropped with `spool_full` when the line plus its new blob bytes do not fit. Writer-owned lines (`gap`, `producer.goodbye`) may exceed the cap by 1 MiB, so a producer restarted on a full spool still ends with its goodbye; one refused even then is counted in the emitter's `writer_lines_skipped`. The worker keeps `quarantine/` within `TRAJECTORY_SPOOL_QUARANTINE_MAX_BYTES` and `TRAJECTORY_SPOOL_QUARANTINE_RETENTION_DAYS` (§13), oldest files first.
- **Reading**: the worker's `read_batch` replaces every reference of a version 2 event line with the blob content before decoding, so sanitize, the event hash, previews and content addressing (§8.4) see the same event as for the inline line, and the resolved size counts against `TRAJECTORY_INGEST_BATCH_BYTES` and the user byte budget. A blob that is missing or whose content does not match its sha256 turns the line into a `gap` control with the same `n` and `t` (`reason` `spool_blob_missing` or `spool_blob_corrupt`, `dropped_events` 1, the event's user, root session, run and request ids); the rest of the file is ingested normally.
- **Sweep**: at most once a minute, after a complete scan of the producer directories, the worker deletes blob files whose mtime is older than the oldest remaining data file (consumed or not; the scan time when there is none) minus 600 s. Data files unmodified for 15 minutes do not hold the cutoff back: they are read instead (at most 256 MiB per sweep, else the plain rule applies) and the blobs they reference are kept whatever their age, so one stuck file does not keep every later blob until the spool is full. A candidate is renamed aside and its mtime checked again, so a writer that refreshed it meanwhile keeps it. Orphaned temp files older than the margin are deleted too.

---

## 4. Transport guarantees

- Per producer: FIFO; `n` contiguous.
- Across producers: arrival order approximated by closed-file order (worker picks the oldest closed file across producers).
- Delivery: at-most-once from producer to spool (loss is detected and represented as a gap); effectively-once from spool to trace DB (file offsets committed in the same transaction as the ingested rows + event_id dedupe).
- Facts tied to a business write are emitted only after the outer business transaction commits.

---

## 5. Producer API (business process)

### 5.1 Module layout
- `backend/trajectory/emitter.py`: `Emitter` class, singleton accessors, SQLAlchemy commit hooks.
- `backend/trajectory/spool.py`: shared constants and helpers for the spool format (line encoding, file naming, `producer.json`, budget file schema), used by both emitter and worker reader.
- `backend/trajectory/__init__.py`: re-exports the public API below and keeps existing exports used by producers.
- `backend/trajectory/recorder.py`: keeps the legacy `db` sink during wave 1; `record`, `record_stream`, `flush` dispatch on `TRAJECTORY_SINK`.

### 5.2 `Emitter`

```python
class Emitter:
    def __init__(self, spool_dir: Path, *, role: str = "backend", queue_bytes: int, max_event_bytes: int,
                 file_bytes: int, file_ms: int, spool_max_bytes: int): ...
    def start(self) -> None                      # idempotent; creates producer dir + producer.json; starts writer thread
    def emit_bytes(self, event_json: bytes, *, user_id: str, session_id: str,
                   run_id: str | None, request_id: str | None) -> bool   # enqueue a serialized event; False when dropped
    def emit_control(self, control: dict) -> bool
    def flush(self, timeout: float = 5.0) -> bool  # block until everything enqueued so far is written and the current file rotated
    def close(self, timeout: float = 5.0) -> None  # flush, write producer.goodbye, rotate, stop thread
    def stats(self) -> dict                        # queued_bytes, queued_lines, written_lines, dropped_events, dropped_bytes,
                                                   # dropped_by_reason, files_closed, last_error, spool_bytes
def get_emitter() -> Emitter | None               # None when TRAJECTORY_SINK != "spool"
def reset_emitter_for_tests() -> None
```

Rules:
- `emit_bytes` does only: size check (`max_event_bytes` → drop, reason `event_too_large`), byte-budget check against `queue_bytes` (→ drop, reason `queue_overflow`), append to a `collections.deque` under a lock, update counters. Target ≤ 20 µs excluding serialization.
- Drop accounting keeps per-root-session sets of run_ids/request_ids (capped) and writes one `gap` control as soon as the queue has room (at most one gap line per 100 ms).
- Writer thread: waits on a condition (≤ 100 ms), assigns `n`, builds the line by byte concatenation (`b'{"v":1,"k":"event","n":' + n + b',"t":"' + t + b'","event":' + event_json + b'}\n'`), writes to the open `.part` file (buffered), flushes buffers at least every 100 ms, rotates when size ≥ `file_bytes` or age ≥ `file_ms` (non-empty): flush, `fsync`, rename `.part` → `.jsonl`.
- Spool budget: the writer rescans the producer directories every 5 s and `blobs/` plus `quarantine/` every 60 s, adding its own data and blob bytes in between (allocated sizes, §3.5). While that estimate is at or above 90 % of `spool_max_bytes` it rescans every 0.5 s and 5 s instead: processes sharing one spool each check the cap against their own sample, and then overshoot it by at most what the others write in half a second. Above `spool_max_bytes` new event lines are dropped (reason `spool_full`), writer-owned `gap` and `producer.goodbye` lines only above `spool_max_bytes` + 1 MiB.
- Disk floor (`TRAJECTORY_SPOOL_MIN_FREE_BYTES`, default 1 GiB, 0 disables it): with every producer-directory rescan the writer samples the free space of the spool's file system (`os.statvfs(spool_dir)`, `f_bavail × f_frsize`) and subtracts its own writes until the next sample. An event line (with its new blob bytes) that would leave less free space than the floor is dropped with reason `disk_full`, counted and reported through `gap` controls like `spool_full`; a writer-owned line is refused only when it would leave less than half the floor. A failed sample sets no floor until the next one and the error is noted. The floor protects the host disk even when `spool_max_bytes` is misconfigured or other data fills the disk.
- Heartbeat: a daemon thread `trajectory-emitter-heartbeat-<pid>`, started with the writer (in `start` and when a dead writer is restarted), refreshes the mtime of `producer.json` every 5 s with `os.utime`. The writer never does it, so a live process whose writer is stalled (slow disk, long write, busy interpreter) is never taken for a producer that is gone (`TRAJECTORY_SPOOL_ABANDON_SECONDS`, §8.2). A missing `producer.json` is left alone: recreating the producer directory stays the writer's job, on its next file open. The thread stops once the writer has handled `producer.goodbye` on close, never runs in a fork child and never raises; errors are only noted.
- Write errors (e.g. ENOSPC): drop the affected lines (reason `writer_error`), close the file, retry opening after 1 s. Never propagate.
- Process exit: `atexit` calls `close(2.0)`; backend lifespan shutdown calls `close(5.0)`.

### 5.3 Public functions (all never raise)

```python
def emit(type: str, data: dict, *, context: TraceContext | None = None, event_id: str | None = None,
         occurred_at=None, **ids) -> str | None
def emit_after_commit(db, type: str, data: dict, *, context: TraceContext | None = None,
                      event_id: str | None = None, occurred_at=None, **ids) -> str | None
def emit_control(control: dict, *, db=None) -> bool      # db given → after commit
def emit_stream(context: TraceContext, event: dict) -> None   # request.delta / tool.output chunks; same as emit
async def record(type, data, *, context=None, db=None, event_id=None, occurred_at=None, **ids)
    # sink=spool: db → emit_after_commit, else emit; returns None
    # sink=db: legacy behavior (wave 1 only)
def record_stream(context, event) -> asyncio.Future      # sink=spool: emit; returns a completed future (result None)
async def flush(context=None) -> str                     # sink=spool: await asyncio.to_thread(emitter.flush); returns "0"
```

`emit` pipeline:
1. `context = context or current()`; no context → `None`.
2. Recording disabled for `context.user_id` → `None` (pause signalling is handled by §5.6, not here).
3. Budget level for the root session (§5.7): blocked/degraded filtering → possibly `None`.
4. Build the event with `prepare_fast(context, {...})` (same validation as `types.prepare()` except it does not call `canonical()`; invalid → count `invalid_event`, rate-limited log, `None`).
5. Serialize with `orjson.dumps(event, default=_json_default, option=orjson.OPT_NON_STR_KEYS)`; failure → count `serialization_failed`, `None`.
6. `emitter.emit_bytes(...)`; returns `event_id` when enqueued.

`emit_after_commit`:
- Resolves the sync session (`db.sync_session` for `AsyncSession`), builds and serializes the event immediately (so later mutation of `data` cannot change it), appends `(bytes, routing)` to `session.info["trajectory_pending_emits"]`.
- A global `after_commit` listener on `sqlalchemy.orm.Session` pops the list and enqueues each item. It ignores commits of nested transactions.
- A global `after_soft_rollback` listener pops and discards the list unless `previous_transaction.nested` is true.
- Sessions that never register pending emits are unaffected (trace DB sessions in embedded mode never register).

Compatibility requirements for wave 1 (`TRAJECTORY_SINK=db` default): the legacy recorder path is untouched and all existing tests pass. With `TRAJECTORY_SINK=spool` the business process performs no trajectory DB access through `record`, `record_stream`, `flush`.

### 5.4 Serialization helper
`_json_default(obj)`: pydantic models → `model_dump(mode="json")`; `datetime` → ISO; `bytes` → `{"availability":"not_recorded","reason":"binary_value"}`; sets/tuples → lists; anything else → `{"availability":"not_recorded","reason":"unsupported_value"}`.

### 5.5 Redaction placement
- Stays in producers: request allowlists (`request_snapshot`, `public_value`), provider output allowlists, stream redactors (`CaptureStreamRedactor`, `StreamTextRedactor`), file tool `sanitize` of before/after text (it feeds sha256 values).
- Moves to the worker: the generic `sanitize(data)` pass that the recorder applied to every event (§8.4).

### 5.6 Pause, resume and baseline without trace tables
Business marker: `SessionExecution.trace_context` (business column, kept).
- First recorded activity for a root session (`trace_context` empty or missing): build the baseline snapshot exactly as `capture_trajectory_baseline_in_tx` does today (history, settings, artifacts as asset references), then `emit_after_commit` `baseline.captured` with event id `evt_baseline_{root_session_id}_0`, and save `trace_context` (with `recording_epoch: 0`).
- Activity while recording is disabled for the owner and `trace_context` is non-empty and not already paused: set `trace_context["recording_paused"] = true` in the same business transaction, `emit_control(..., db=db)` `recording.state` paused.
- Activity when enabled and `trace_context["recording_paused"]` is true: clear the flag, increment `recording_epoch` (E), `emit_control` `recording.state` resumed, and `emit_after_commit` `baseline.captured` with id `evt_baseline_{root_session_id}_{E}`.
- The worker keeps the first event per event id (keep-first dedupe), so duplicates from races are harmless.

### 5.7 Budget file (`control/budgets.json`)

```json
{"version": 1, "generated_at": "2026-09-14T08:00:00Z",
 "sessions": {"<root_session_id>": {"level": "degraded", "reason": "trajectory_bytes", "since": "..."}},
 "users": {"<user_id>": {"level": "degraded", "reason": "user_daily_bytes", "since": "..."}}}
```

- Producers reload it when its mtime changes (checked at most every `TRAJECTORY_BUDGET_REFRESH_MS`, default 5000) and cache it in memory; missing or invalid file → no limits.
- `degraded`: drop `request.delta` events whose `data.mode` is `delta` except the last chunk emitted by `finish()` (producers mark it `data.final=true`), drop `tool.output` chunks except `executor_result` outputs, truncate string `output` values in tool events to 8 KiB (`data.truncated=true`, `data.original_bytes`).
- `blocked`: only lifecycle events pass (`run.*`, `turn.*`, `step.*`, `tool.requested`, `tool.finished`, `request.started`, `request.usage`, `request.finished`, `question.*`, `permission.*`, `job.*`, `recording.gap`); drops are counted with reason `budget` and reported through `gap` controls.

### 5.8 Metadata sync task (`backend/trajectory/meta_sync.py`)
- Runs in the backend process (server and desktop) when `TRAJECTORY_SINK=spool`.
- At start: full snapshot of `sessions`, `users`, `workspaces`, `file_assets` (paged, 500 rows per query, ordered by primary key) emitted as `*.meta` controls. Then every `TRAJECTORY_META_SYNC_SECONDS` (30): rows with `updated_at > cursor` (fallback: full rescan every 10 minutes for tables without `updated_at`).
- Business DB cost bound: at most 4 small indexed/paged queries per interval. Runs outside request paths; any error is logged and retried next interval.
- Immediate controls: `session.deleted` (after `delete_session` commit), `asset.deleted` (after asset deletion commit).

---

## 6. Trace database

### 6.1 Engine and sessions (`backend/trajectory/store/database.py`)

```python
class TraceBase(DeclarativeBase):
    type_annotation_map = {dict: JSONType, datetime: DateTime(timezone=True)}   # JSONType imported from db.base
def init_trace_engine(url: str, *, pool_size: int = 5, max_overflow: int = 5) -> AsyncEngine
def get_trace_engine() -> AsyncEngine
@asynccontextmanager
async def trace_session() -> AsyncIterator[AsyncSession]   # commit on success, rollback on exception, always close
async def close_trace_engine() -> None
def trace_dialect() -> str                                  # "postgresql" | "sqlite"
```

- Never touches `db.base._engine`. PG engines use `pool_pre_ping=True`, `connect_args={"server_settings": {"statement_timeout": "5000", "application_name": "openbox-trace"}}` for request-serving sessions; background jobs may override the statement timeout per transaction (`SET LOCAL statement_timeout`).
- Models: `backend/trajectory/store/models.py` (TraceBase only). Nothing in `db.models` imports them.

### 6.2 Alembic
- `backend/alembic_trajectory.ini`: `script_location = trajectory/store/migrations`, `version_table = trajectory_alembic_version`.
- `env.py` requires `TRAJECTORY_DATABASE_URL` and exits with an error when it is unset or equals `DATABASE_URL`. Never falls back to `DATABASE_URL`.
- Revision `t0001_initial`: all tables below. PostgreSQL-only DDL (extension, partitioning, GIN indexes) guarded by dialect checks; SQLite gets plain tables and btree indexes.
- A test asserts a single head for this chain (copy of `tests/unit/test_migration_heads.py`).

### 6.3 Tables

Types: `str` = `String(64)` unless noted; `json` = `JSONType`; timestamps are timezone-aware.

**`session_trajectories`**
| column | type | notes |
|---|---|---|
| id | String(64) PK | `trj_` + the first 32 hex digits of sha256(`openbox-trajectory\0` + root session id): blobs uploaded before the row exists already use its prefix, across worker restarts |
| user_id | str | |
| session_id | str UNIQUE | root session |
| workspace_id | str | |
| started_at, updated_at | timestamp | |
| last_activity_at | timestamp | max `occurred_at` ingested |
| next_seq | BigInteger | default 1 |
| committed_seq | BigInteger | default 0 |
| projected_seq | BigInteger | default 0 |
| archived_seq | BigInteger | default 0 |
| checkpoint_seq | BigInteger | default 0 (latest checkpoint through_seq) |
| schema_version | Integer | 2 |
| recording_status | String(32) | `recording`,`gap`,`paused`,`deleted`,`expired` |
| recording_epoch | Integer | default 0 |
| event_count | BigInteger | |
| stored_bytes | BigInteger | compressed bytes in blobs + segments |
| budget_level | String(16) | `normal`,`degraded`,`blocked` |
| budget_reason | String(64) nullable | |
| deleted_at, content_expired_at | timestamp nullable | |
UNIQUE(user_id, session_id). Index (last_activity_at).

**`trajectory_events`** (hot, unarchived events only)
| column | type |
|---|---|
| trajectory_id | str FK → session_trajectories.id ON DELETE CASCADE |
| seq | BigInteger |
| recorded_on | Date (partition key) |
| event_id | String(128) |
| type | String(64) |
| version | Integer |
| user_id, session_id, source_session_id | str |
| request_id, call_id, agent_id | String(128) nullable |
| context | json (non-null identity fields) |
| data | json (sanitized; externalized values replaced by references) |
| hints | json nullable (worker-only: `{"preview": {"<field>": "<≤240 chars>"}}`) |
| content_hash | String(64) |
| occurred_at, recorded_at | timestamp |
- PostgreSQL: `PARTITION BY RANGE (recorded_on)`, PK (trajectory_id, seq, recorded_on), daily partitions `trajectory_events_pYYYYMMDD` plus `trajectory_events_default`. Local indexes: (trajectory_id, seq), (trajectory_id, request_id, seq), (trajectory_id, call_id, seq).
- SQLite: PK (trajectory_id, seq), same secondary indexes.

**`trajectory_event_keys`** (idempotency registry; survives archival)
event_id String(128) PK; trajectory_id str; seq BigInteger; content_hash String(64); recorded_at timestamp. Index (recorded_at).

**`trajectory_segments`**
trajectory_id FK CASCADE; from_seq BigInteger; to_seq BigInteger; storage_key Text; event_count Integer; raw_bytes BigInteger; stored_bytes BigInteger; sha256 String(64) (of uncompressed JSONL); compression String(16) (`zstd`); created_at. PK (trajectory_id, from_seq). Index (trajectory_id, to_seq).

**`trajectory_payloads`** (per-trajectory content references; `payload_id` is the public identity used by `$payload`/`$media`)
| column | type | notes |
|---|---|---|
| payload_id | String(64) PK | `pld_` + uuid hex |
| trajectory_id | str FK CASCADE | |
| dedupe_key | String(64) | sha256 hex of `"{sha256}:{media_type}:{source_asset_id or ''}:{storage_kind}"` |
| sha256 | String(64) nullable | uncompressed bytes; null for asset references without a known hash |
| size_bytes | BigInteger | uncompressed |
| stored_bytes | BigInteger | 0 for asset references |
| media_type | String(128) | normalized, truncated to 128 |
| encoding | String(16) | `identity`,`zstd` |
| storage_kind | String(16) | `blob`,`asset` |
| storage_key | Text | blob key, or the asset `oss_key` for `asset` |
| source_asset_id | str nullable, indexed | |
| availability | String(24) | `available`,`deleted`,`expired` |
| first_seq | BigInteger | visibility lower bound |
| created_at, deleted_at | timestamp | |
UNIQUE(trajectory_id, dedupe_key). Index (trajectory_id, sha256).

**`trajectory_records`**
trajectory_id FK CASCADE; record_id String(256); kind String(32); status String(32); agent_id String(128) nullable; message_id String(128) nullable; start_seq BigInteger; end_seq BigInteger nullable; applied_seq BigInteger; projector_version Integer; data json (values > `TRAJECTORY_RECORD_INLINE_BYTES` stored as `$ref`); summary json (record without `data` and `blocks`); search_doc Text (≤ 4000 chars). PK (trajectory_id, record_id). Indexes (trajectory_id, start_seq, record_id), (trajectory_id, message_id), (trajectory_id, kind, status, start_seq), (trajectory_id, agent_id). PostgreSQL: `CREATE EXTENSION IF NOT EXISTS pg_trgm`; GIN (search_doc gin_trgm_ops).

**`trajectory_record_events`**
trajectory_id; record_id String(256); seq BigInteger. PK (trajectory_id, record_id, seq). Rows written by the projector for every (event, target record) pair including implicit targets.

**`trajectory_session_summaries`**: as today (`trajectory_id` PK FK CASCADE, user_id, session_id, workspace_id, last_activity_at, running_status, recording_status, model, applied_seq, statistics json) with the same four indexes.

**`trajectory_checkpoints`**: as today (trajectory_id, through_seq PK; projector_version; state json with `record_pages` payload references; digest; created_at).

**`trajectory_exports`**: as today plus `lease_owner` String(64) nullable, `lease_until` timestamp nullable, `size_bytes` BigInteger nullable.

**Metadata replicas**
- `trajectory_meta_sessions`: id PK; user_id; workspace_id; project_id; parent_id; kind String(32); title Text; status String(32); model String(128); agent String(64); is_deleted Boolean; deleted_at; created_at; updated_at; synced_at. Indexes (updated_at, id), (user_id, updated_at), (workspace_id, updated_at), (parent_id).
- `trajectory_meta_users`: id PK; username; email; role String(32); is_active; is_deleted; updated_at; synced_at.
- `trajectory_meta_workspaces`: id PK; name; updated_at; synced_at.
- `trajectory_meta_assets`: id PK; user_id; workspace_id; session_id; oss_key Text; mime String(128); size BigInteger; status String(32); is_deleted; deleted_at; updated_at; synced_at. Index (session_id).

**Worker bookkeeping**
- `trajectory_ingest_producers`: producer_id String(128) PK; hostname; pid Integer; boot_id; role String(32); started_at; last_n BigInteger; last_seen_at; goodbye Boolean; abandoned Boolean.
- `trajectory_ingest_files`: producer_id String(128); file_name String(64); bytes_consumed BigInteger; lines_consumed BigInteger; done Boolean; updated_at. PK (producer_id, file_name).
- `trajectory_gc_queue`: id BigInteger PK autoincrement; kind String(16) (`key`,`prefix`); storage_key Text; reason String(64); attempts Integer; next_attempt_at timestamp; last_error Text nullable; created_at. Index (next_attempt_at).
- `trajectory_worker_state`: key String(64) PK; value json; updated_at.
- `trajectory_audit_outbox`: id BigInteger PK autoincrement; payload json; attempts Integer; next_attempt_at; created_at.

### 6.4 Readiness
Worker `/health` checks the trace schema (alembic head) and spool accessibility. The business readiness schema (`db/base.py`) removes all trajectory tables in wave 2 (keeps `session_executions.trace_context`, `cron_runs.trace_context`).

### 6.9 Business database retirement (wave 2)
- Business alembic revision after the current head renames the 7 tables to `legacy_trajectory_*` (renaming preserves data; indexes and constraints follow).
- `db/models/trajectory.py` is removed from business metadata; code that still needs the legacy tables is the converter only (it reflects them).
- `python -m trajectory.tools.migrate_legacy` (§8.14) copies data into the trace DB and OSS; after verification `--finalize-drop` drops `legacy_trajectory_*`.
- A follow-up business revision `DROP TABLE IF EXISTS legacy_trajectory_*` is added after production finalize (keeps dev/desktop DBs consistent).

---

## 7. Blob storage

### 7.1 OSS client additions (`backend/core/oss.py`)
All server-side operations use OSS V1 header signatures, a shared `httpx.AsyncClient` (`trust_env=False`, connection pooling), cached credentials (refresh every 10 minutes), and the internal host when `internal=True` and the endpoint is an Aliyun endpoint.

```python
async def put_object(self, key: str, data: bytes, *, content_type: str = "application/octet-stream",
                     forbid_overwrite: bool = False, internal: bool = False, timeout: float = 120) -> str   # etag
async def put_object_file(self, key: str, path, *, content_type: str = "application/octet-stream",
                          forbid_overwrite: bool = False, internal: bool = False) -> str   # etag; Content-MD5 from one chunked pass, streamed body
async def get_object(self, key: str, *, internal: bool = False, timeout: float = 120) -> bytes    # FileNotFoundError on 404
async def head_object_info(self, key: str, *, internal: bool = False) -> dict | None
async def delete_object_key(self, key: str, *, internal: bool = False) -> bool
async def delete_objects(self, keys: list[str], *, internal: bool = False) -> int                 # POST ?delete, ≤1000 keys
async def list_objects(self, prefix: str, *, continuation_token: str | None = None,
                       max_keys: int = 1000, internal: bool = False) -> tuple[list[dict], str | None]  # ListObjectsV2
```
- `forbid_overwrite=True` sends `x-oss-forbid-overwrite: true`; a 409 `FileAlreadyExist` is success.
- `Content-MD5` is sent on PUT.
- Errors raise `OssError(status, code, request_id)` except the documented FileNotFoundError / idempotent cases.
- Existing presign methods keep their behavior.

### 7.2 `backend/trajectory/storage.py`

```python
class BlobStore(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str, if_absent: bool = True) -> None
    async def put_file(self, key: str, path: str | os.PathLike[str], *, content_type: str, if_absent: bool = True) -> None   # streams a local file (exports), bounded memory
    async def get(self, key: str) -> bytes              # FileNotFoundError when missing
    async def exists(self, key: str) -> bool
    async def delete(self, key: str) -> None            # idempotent
    async def delete_prefix(self, prefix: str) -> int
    def list(self, prefix: str) -> AsyncIterator[str]
class OssBlobStore:    ...   # TRAJECTORY_BLOB_PROVIDER=oss; bucket, region, endpoint, internal flag
class LocalBlobStore:  ...   # TRAJECTORY_BLOB_PROVIDER=local; root dir; atomic temp-file + rename writes
class MemoryBlobStore: ...   # tests: counters (puts, gets, deletes, bytes), fault injection hooks, get hooks
def get_blob_store() -> BlobStore
def set_blob_store(store: BlobStore | None) -> None

COMPRESSIBLE_TYPES  # application/json, application/x-ndjson, text/*, application/xml, application/javascript
def encode_blob(content: bytes, media_type: str) -> tuple[bytes, str]    # zstd level 3 when compressible and ≥ 1024 bytes
def decode_blob(stored: bytes, encoding: str) -> bytes
def blob_key(trajectory_id: str, sha256: str) -> str        # f"{prefix}{trajectory_id}/blobs/{sha256}"
def segment_key(trajectory_id: str, from_seq: int, to_seq: int) -> str   # f"{prefix}{trajectory_id}/segments/{from_seq:012d}-{to_seq:012d}.jsonl.zst"
def checkpoint_key(...)                                      # checkpoint pages use blob_key (content addressed)
def export_key(export_id: str, sha256: str) -> str           # f"{prefix}_exports/{export_id}/{sha256}.zip"
def trajectory_prefix(trajectory_id: str) -> str             # f"{prefix}{trajectory_id}/"
```

- `prefix` = `TRAJECTORY_OSS_PREFIX` (default `trajectories/`) for OSS, relative directory for local.
- Asset bytes are read from the business assets bucket (same OSS client, `OSS_BUCKET`) only by the worker, only when an admin opens a payload.

### 7.3 Reference envelopes (stored data and API)
- `$payload` (existing): `{"$payload": {"payload_id", "sha256", "size_bytes", "media_type", "availability"[, "reason"]}}` for externalized whole event data and checkpoint pages.
- `$media` (existing): `{"$media": <payload reference or availability marker>, "source_kind", "original_encoding", "declared_media_type"[, "source_asset_id"]}`.
- `$ref` (new, internal storage and optional API): `{"$ref": {"sha256", "size_bytes", "media_type": "application/json", "kind": "system"|"tools"|"message"|"value", "payload_id"}}` for content-addressed JSON values inside event or record data. API responses expand `$ref` values back to the original JSON unless the caller asks for `expand=refs` (§8.12).

---

## 8. Worker

### 8.0 Package layout

```
backend/trajectory/worker/__init__.py
backend/trajectory/worker/__main__.py     # python -m trajectory.worker: migrations check, uvicorn app on TRAJECTORY_WORKER_PORT
backend/trajectory/worker/settings.py     # WorkerSettings (env parsing, defaults from §13)
backend/trajectory/worker/lock.py         # single-writer lock
backend/trajectory/worker/services.py     # WorkerServices: start/stop of loops; used by app lifespan and embedded mode
backend/trajectory/worker/spool_reader.py # enumerate files, read lines, offsets, abandonment, quarantine
backend/trajectory/worker/ingest.py       # IngestService
backend/trajectory/worker/content.py      # sanitize, externalization, content addressing, media references
backend/trajectory/worker/meta.py         # control record application, ownership checks
backend/trajectory/worker/budgets.py      # budget computation and control/budgets.json
backend/trajectory/worker/notify.py       # trajectory.available publication
backend/trajectory/worker/projection.py   # ProjectionService
backend/trajectory/worker/archive.py      # ArchiveService (segments, partitions, key pruning)
backend/trajectory/worker/retention.py    # RetentionService (content expiry, deletions, GC queue)
backend/trajectory/worker/metrics.py      # counters/gauges registry, /metrics payload, CloudMonitor push
backend/trajectory/worker/app.py          # FastAPI app factory (routes, ws, health, metrics, lifespan)
backend/trajectory/worker/routes.py       # admin HTTP API (moved from api/admin_trajectories.py)
backend/trajectory/worker/ws.py           # admin WS + ticket (moved from api/admin_trajectory_ws.py)
backend/trajectory/worker/embedded.py     # start_embedded_worker()/stop for backend process
backend/trajectory/repository.py          # read functions against the trace DB
backend/trajectory/payload.py             # payload/blob reads, expand, visible_references
backend/trajectory/export.py              # exports (streamed, leased)
backend/trajectory/lifecycle.py           # deletion helpers used by retention
backend/trajectory/auth.py                # admin authorization via backend introspection
backend/trajectory/tools/migrate_legacy.py
backend/trajectory/ops/cms.py             # CloudMonitor custom metric push (stdlib RPC signing)
backend/trajectory/ops/backup.py          # pg_dump upload helper used by deploy scripts
```

### 8.1 Single writer
- PostgreSQL: session-level `pg_try_advisory_lock(hashtext('openbox-trajectory-writer'))` on a dedicated connection held for the process lifetime. SQLite: an `fcntl` lock file next to the database file.
- Without the lock the worker serves read APIs only and retries acquisition every 10 s. `/health` reports `writer: true|false`.

### 8.2 Spool reader
- Poll every `TRAJECTORY_INGEST_POLL_MS` (200). A file is ready when its name ends in `.jsonl`, or it is a `.part` whose mtime is older than `TRAJECTORY_SPOOL_ABANDON_SECONDS` (60).
- Order: among ready files, the oldest mtime first; within a producer strictly by counter (never skip ahead of an unconsumed lower counter unless that file is abandoned-and-consumed).
- Resume from `trajectory_ingest_files.bytes_consumed`. Parse complete lines only; a torn last line of an abandoned `.part` is ignored and reported as producer loss.
- Unparsable line → move the whole file to `quarantine/` with a `.reason` file, count `quarantined_files`, emit producer-loss gaps for sessions seen from that producer in the last 10 minutes.
- Producer tracking: first line `n` must equal `last_n + 1`; otherwise record loss `[last_n+1, n-1]`. A producer whose files are all consumed, has no `goodbye`, and whose newest file is abandoned → `abandoned=true` and loss is reported once.

### 8.3 Ingest transaction
Per batch (≤ `TRAJECTORY_INGEST_BATCH_LINES` lines or ≤ `TRAJECTORY_INGEST_BATCH_BYTES`):
1. Content preparation outside the DB transaction: sanitize, content addressing, media decoding, blob uploads (§8.4). Uploads are idempotent (content-addressed keys, `if_absent=True`).
2. One trace DB transaction: apply controls (§8.5), resolve trajectories, dedupe, allocate seq (`SELECT ... FOR UPDATE` on the trajectory row), insert events/keys/payload rows, update counters, update `trajectory_ingest_files`/`trajectory_ingest_producers`.
3. After commit: delete fully consumed files, enqueue projection for touched trajectories, publish notifications (§8.7).
- Blob upload failure for an event → retry the batch with exponential backoff (1 s → 60 s) while other producers' files continue; after 10 attempts the event's content is replaced by an availability marker `{"availability":"not_recorded","reason":"blob_store_unavailable"}` and a gap is recorded.

Trajectory resolution:
- Key: root `session_id` + `user_id`. Missing row → create (id derived from the root session id, see `session_trajectories.id`), append `trajectory.started` at seq 1 with id `evt_start_{trajectory_id}` and data `{"existing_session": <bool: first ingested event is baseline.captured with non-empty history>, "coverage_start": <occurred_at of first event>, "schema_version": 2}`.
- `workspace_id`: from the event, else the meta session, else `""`.
- Tombstoned (`deleted_at` set) or meta session `is_deleted` → drop the event (count `deleted_drops`).
- Ownership (meta-assisted): when the meta session exists and its `user_id` differs → drop (`ownership_drops`). When `source_session_id` differs from the root and the meta chain (parent_id walk, ≤100 hops) is known and does not reach the root → drop. Unknown meta → accept.

Dedupe (keep-first): look up `trajectory_event_keys`. Same trajectory and hash → duplicate (count, skip). Different hash or trajectory → conflict (count `idempotency_conflicts`, log once per event_id, skip). Hash = `digest(event minus event_id and occurred_at)` computed after sanitize and before externalization (same definition as today).

### 8.4 Content preparation (`content.py`)
Order per event:
1. `data = sanitize(data)` (existing `trajectory.redaction.sanitize`).
2. Compute `hints.preview` for fields the projector previews (`text`, `content`, `input`, `prompt`, `questions`, `requested_arguments`, `arguments`, `summary`, and result fields `output`, `result`, `answers`, `model_output`) using the projector's `_preview` on the unexternalized values.
3. Strip producer helper keys (`media_sources`, `asset_ref`, `source_root_session_id`) after using them.
4. Media:
   - Base64 media in any data (`data:<mime>;base64,` strings, `{"type":"base64","data"}`, `input_audio.data`): decode; sha256; if `media_sources[sha]` names an asset → payload row `storage_kind=asset` (no bytes) and `$media` reference with `source_kind: "asset"`; otherwise store bytes as a blob (identity encoding for non-compressible types) with `source_kind: "inline_non_asset"`. Invalid base64 → `{"$media": {"availability":"not_recorded","reason":"invalid_base64","media_type"}}`.
   - `artifact.recorded` with `asset_ref` → payload row `storage_kind=asset`, `storage_key=oss_key`, `source_asset_id`; set `data.payload` to the payload reference.
   - Service bodies already carry `trajectory-media:<id>` placeholders; `media_inputs` manifests are kept.
5. Content addressing of `request.prepared.data.input` (dict):
   - `system` / `instructions` (when canonical size ≥ 1024) → `$ref` kind `system`.
   - `tools` (≥ 1024) → `$ref` kind `tools`.
   - Each element of `messages` or list-valued `input` (≥ 512) → `$ref` kind `message`.
   - Blob = canonical JSON bytes; payload row per unique content per trajectory (`storage_kind=blob`, `media_type=application/json`).
6. Any other JSON value (at any depth ≤ 6) whose canonical size exceeds `TRAJECTORY_INLINE_BYTES` (65536) → `$ref` kind `value`.
7. After steps 5-6, if canonical(data) still exceeds `TRAJECTORY_INLINE_BYTES` → whole data stored as a blob and replaced by `{"$payload": ref}` (legacy-compatible envelope).
8. `first_seq` of new payload rows = the event's seq (allocated in the transaction; content preparation uses a placeholder that is filled in-transaction). Existing rows keep their earlier `first_seq`.
- Payload rows that already exist with `availability != available` → the new reference is written with that availability (never resurrect deleted content).

### 8.5 Controls (`meta.py`)
- Meta upserts: last-writer-wins by `updated_at` (ignore older).
- `session.deleted`: tombstone trajectory for that root session if present (§8.11) and mark meta session deleted.
- `asset.deleted`: mark meta asset deleted; payload rows with that `source_asset_id` (all trajectories) → `availability=deleted`, `deleted_at`; for `storage_kind=blob` rows whose key is not referenced by any other available row in the same trajectory → enqueue GC key deletion; append `artifact.removed` (`artifact_id`, `availability: deleted`, `reason: explicitly_deleted`) with id `asset:{sha256(root_session_id + ':' + asset_id + ':deleted')}` to each affected live trajectory.
- `recording.state paused`: if trajectory exists and not paused → append `recording.gap {phase:"paused", reason:"recording_disabled", last_recorded_seq}`; status `paused`.
- `recording.state resumed`: append `recording.gap {phase:"resumed", reason:"recording_reenabled", previous_committed_seq}`; status `gap`; increment `recording_epoch`.

### 8.6 Gaps
- `gap` control → for each listed session with an existing trajectory: one `recording.gap` event per run_id (≤ 10) with `run_id` set, plus one without run_id: data `{"phase":"dropped","reason","dropped_events","dropped_bytes","producer_id","request_ids"}`.
- Producer loss (missing `n`, torn tail, quarantined file, abandoned producer) → for sessions seen from that producer within the last 10 minutes (in-memory map, persisted in `trajectory_worker_state` every minute): `recording.gap {"phase":"lost","reason":"producer_lines_lost"|"producer_crashed","producer_id","from_n","to_n"}` with the last known run_id of each session.
- Gap events use deterministic ids `gap:{producer_id}:{n_or_range}:{session_id}[:{run_id}]` for idempotency.

### 8.7 Notifications
After each ingest commit, for each trajectory whose `committed_seq` advanced: publish `trajectory.available` `{user_id, owner_user_id, session_id, trajectory_id, committed_seq}` (plus `deleted: true` for tombstones) through the in-process bus of the worker (the WS lives in the same process). When `REDIS_URL` is set also publish on the Redis bus channel so multiple worker replicas can fan out.

### 8.8 Projection (`projection.py`)
- Loop: every `TRAJECTORY_PROJECTION_BATCH_MS` (250) take trajectories with `projected_seq < committed_seq`; per trajectory read up to `TRAJECTORY_PROJECTION_BATCH_EVENTS` (200) × 5 events in seq order from hot rows (projection always precedes archival; archival only covers seq ≤ projected_seq).
- Reuse `trajectory.projector` (`targets`, `reduce`, `contribution`, `statistics`) unchanged in semantics. Feed events with `$ref`/`$payload`/`$media` references left in place; pass `hints` so `_update` uses `hints.preview[field]` when the candidate value contains references (add an optional `hints` parameter to the projector functions; default behavior unchanged for the TypeScript parity fixtures).
- Load affected records: union of targets + implicit targets. Fix the drift issues: the system-record preload filters by agent_id **and** source_session_id; the interrupt/gap preload loads every non-terminal tool/request/assistant/step record with the matching run_id.
- Write each touched record once per batch: `data` (values > `TRAJECTORY_RECORD_INLINE_BYTES` externalized to `$ref`), `summary`, `search_doc`, `applied_seq`; insert `trajectory_record_events` rows; update the summary row (statistics diff, running_status, model, recording_status, `last_activity_at` = max occurred_at); set `projected_seq`.
- `search_doc` = space-joined `kind`, `record_id`, `title`, `preview`, `result_preview`, `status_reason`, `data.tool`/`data.name`, error messages, truncated to 4000 characters.
- Checkpoints: when `projected_seq - checkpoint_seq >= TRAJECTORY_CHECKPOINT_INTERVAL` build a checkpoint from the full expanded state (records with references expanded), paginate by 100 records, store pages as content-addressed blobs (unchanged pages dedupe), insert the checkpoint row, update `checkpoint_seq`.

### 8.9 Reads (`repository.py`, `payload.py`)
Keep function names, parameters and response shapes from `maps/projection.md` §4 and `maps/api.md` §2. Implementation against the trace DB:
- `get_trajectory`: meta session (deleted → LookupError "Session not found"); trajectory row by (session_id, user_id, not deleted). If the meta session is missing but the trajectory exists, use the trajectory row (meta lag).
- `list_sessions`: same filters/cursor/sort semantics as today, SQL over `trajectory_meta_sessions` ⋈ `trajectory_meta_users` ⋈ `trajectory_meta_workspaces` ⟕ `session_trajectories` ⟕ `trajectory_session_summaries`, including `include_unrecorded` (root sessions without a trajectory).
- `get_session_header`, `_metadata`: same outputs; identity fields from meta tables; recording flags evaluated from env (shared).
- `read_events`: seq ≤ `archived_seq` from segments (download + zstd decode + sha256 verify; in-memory LRU of decoded segments, `TRAJECTORY_SEGMENT_CACHE_BYTES` 128 MiB), seq > `archived_seq` from hot rows; gap and tail checks unchanged; `include_data=true` expands `$ref` (always) and `$payload` (as today) with `visible_references`; blob fetches use a concurrent fetcher (≤16 in flight) and an LRU cache of decoded blobs (`TRAJECTORY_BLOB_CACHE_BYTES` 256 MiB).
- `state_at`: fast path at head from records (expanding references); otherwise latest checkpoint + replay (unchanged).
- `list_records`: fast path from `summary` as today.
- `get_record(record_id, through_seq, expand="full"|"refs")`: when `through_seq == projected_seq` and the record's `applied_seq <= through_seq`: read the single record row, expand (unless `refs`), load events from `trajectory_record_events` seq list (≤ min(H, as_of)) via `read_events` helpers, return the same shape (`record.events`). Otherwise fall back to `state_at` (today's semantics).
- `search`: when `through_seq == projected_seq`: SQL over `search_doc` (`ILIKE '%q%'` escaped; PostgreSQL uses the trigram index) filtered by `start_seq <= H`, ordered by (start_seq, record_id), offset cursor `[H, q, offset]` unchanged, `preview` = 240-character window of `search_doc` around the first match, `seq` = record `as_of_seq` from summary. Otherwise fall back to the state-based search (today's semantics).
- Payload visibility and availability: `first_seq <= H`; `availability`; `storage_kind=asset` additionally requires the meta asset to exist and not be deleted (410 otherwise); bytes for assets come from the business assets bucket (`OSS_BUCKET`) via the worker; no sha256 check for asset references without a hash.
- Remove the `_deleted_payloads` fast-path kill switch; availability is resolved per reference.
- Content-expired trajectories: payload/segment/blob reads raise `FileNotFoundError("Trajectory content has expired")` → 410 `trajectory_content_deleted`.

### 8.10 Archive (`archive.py`)
- Every 30 s: trajectories with `projected_seq - archived_seq >= TRAJECTORY_SEGMENT_EVENTS` (1000), or `archived_seq < projected_seq` and `last_activity_at` older than `TRAJECTORY_SEGMENT_IDLE_SECONDS` (300).
- Segment content: stored event rows as JSON lines (`event_id, trajectory_id, seq, type, version, user_id, session_id, source_session_id, request_id, call_id, agent_id, context, data, hints, content_hash, occurred_at, recorded_at`), ≤ 1000 events and ≤ `TRAJECTORY_SEGMENT_MAX_BYTES` (4 MiB raw).
- Upload (content-addressed name by range; `if_absent=False` because a failed earlier attempt may have left a partial object — verify by reading back the sha256), then in one transaction insert the segment row, set `archived_seq`, delete hot rows in the range.
- PostgreSQL partitions: create partitions for today..today+7 each day; drop partitions older than `TRAJECTORY_HOT_DAYS` (7) when they contain no rows (all archived); otherwise log and export a metric (`stale_hot_partitions`).
- Prune `trajectory_event_keys` older than `TRAJECTORY_DEDUPE_DAYS` (30) whose seq ≤ archived_seq.

### 8.11 Retention and deletion (`retention.py`, `lifecycle.py`)
- Tombstone (session deleted): set `deleted_at`, `recording_status=deleted`; delete hot events, records, record_events, checkpoints, summaries, segments rows; payload rows → `availability=deleted`; exports → `deleted`; enqueue GC `prefix` `trajectory_prefix(tid)`; publish the deleted notification. Keep the trajectory row and payload rows (tombstones reject late events and resurrection).
- Content expiry: trajectories with `last_activity_at < now - TRAJECTORY_CONTENT_RETENTION_DAYS` (180): same as tombstone except the trajectory keeps `recording_status=expired`, `content_expired_at`, the summary row and statistics.
- Export expiry: exports older than `TRAJECTORY_EXPORT_RETENTION_DAYS` (30) → `deleted` + GC key.
- GC queue: process rows with `next_attempt_at <= now` ordered by `next_attempt_at`, ≤ 100 per tick; failures back off exponentially (30 s → 6 h) and keep processing other rows (no starvation).
- Orphan sweep (every 10 min, catches objects no row ever referenced): list the next 1000 keys of the namespace after the cursor in `trajectory_worker_state` `gc.orphan_cursor` (back to the start after a short page); blobs older than 7 days without an available payload row (checkpoint pages are payload rows) and exports without a live export row that have no GC `key` entry get one with reason `orphan_object`, decided in batched queries. Segments and any other object are left to the trajectory's prefix deletion: archive retries rewrite a segment key without the object guard. Prefix entries are never queued; the daily report counts `gc_orphans_queued`.

### 8.12 Admin API and WS (`routes.py`, `ws.py`, `auth.py`)
- Paths, params, responses, error mapping, no-store headers, audit action names: identical to `maps/api.md` §2-4.
- Additive changes:
  - `GET /sessions/{sid}/records/{record_id:path}?expand=full|refs` (default `full`).
  - `GET /sessions/{sid}/blobs/{sha256}?through_seq=H`: JSON body of a `$ref` value visible at H in that trajectory (404 before first_seq or unknown, 410 deleted/expired, 409 corrupt); no-store, nosniff.
  - `GET /sessions/{sid}/payloads/{payload_id}?meta=1`: `{payload_id, availability, media_type, size_bytes, sha256}` without bytes.
  - Header `capabilities.refs = true`.
- Authorization (`auth.py`):
  1. Bearer JWT verified locally with `JWT_SECRET` (type `access`, `sub`, `exp`); Redis `jwt_bl:{jti}` rejection.
  2. `POST {TRAJECTORY_BACKEND_INTERNAL_URL}/api/internal/trajectory/viewer` with `X-Internal-Token: INTERNAL_API_TOKEN`, body `{"user_id","client","sid","jti"}` → `{"user_id","role","is_active","is_deleted","mobile_session_valid","admin_enabled"}`; cached per (user_id, client, sid) for `TRAJECTORY_AUTH_CACHE_SECONDS` (5).
  3. Same outcomes as today: inactive/deleted/invalid mobile → 401; role ≠ admin → 403; admin disabled → 404.
  - Desktop/embedded mode without JWT: same default-admin behavior as today.
  - `revalidate_viewer` after blob reads bypasses the cache.
- Tickets: the worker serves `POST /api/admin/trajectories/ticket` using the shared Redis ticket store format (`auth/ticket.py`), audience `admin_trajectories`.
- WS: identical protocol; accept before closing with 4401/4403; send a WebSocket ping every 20 s (uvicorn `ws_ping_interval`); notification payload sent directly (5 keys) without recomputing the header per wake.
- Audit: records go to `trajectory_audit_outbox` and are delivered in batches to `POST /api/internal/trajectory/audit` (backend writes `audit_logs`); `list` and `view` actions are deduplicated per (viewer, session, action) within 60 s.
- Backend internal endpoints (in `backend/api/internal.py`, guarded like `/api/internal/tunnel-keys`): `trajectory/viewer`, `trajectory/audit`.

### 8.13 Health and metrics
- `GET /health`: `{"status":"ok"|"degraded","writer":bool,"db":bool,"spool":bool,"blob_store":bool,"version"}`; HTTP 200 when the process can serve reads.
- `GET /metrics` (JSON, admin network only; not routed by nginx): counters `ingest_lines`, `ingest_events`, `duplicates`, `idempotency_conflicts`, `deleted_drops`, `ownership_drops`, `gaps_recorded`, `producer_loss_events`, `quarantined_files`, `blob_puts`, `blob_put_bytes`, `blob_put_failures`, `segment_uploads`, `segment_failures`, `gc_deleted`, `gc_failures`, `exports_built`; gauges `spool_bytes`, `spool_files`, `spool_oldest_age_seconds`, `ingest_lag_seconds`, `projection_lag_events`, `archive_lag_events`, `gc_queue_depth`, `trace_db_bytes` (sampled every 5 min), `hot_events_rows`, `trajectories_degraded`, `trajectories_blocked`, `stale_hot_partitions`.
- `trajectory.ops.cms`: pushes selected metrics to CloudMonitor (namespace group from `TRAJECTORY_CMS_GROUP_ID` or dimension `instance=gw2`) using Aliyun RPC signature v1 with credentials from `ALIYUN_CLI_CONFIG`/env; invoked by the host timer (§12).

### 8.14 Legacy converter (`tools/migrate_legacy.py`)
`python -m trajectory.tools.migrate_legacy --business-database-url URL --legacy-blob-path PATH [--dry-run] [--only trj_id ...] [--verify] [--finalize-drop]`
- Reads `legacy_trajectory_*` tables (or the original names when `--source-tables original`) with a read-only transaction.
- Per trajectory: create the trajectory row with the same id/user/session/workspace/started_at/status; copy events in seq order preserving event_id, seq, type, version, context, occurred_at, recorded_at, content_hash; load `$payload` bytes from the legacy `content` column or the legacy blob file (`PATH/<storage_key>`); run content preparation (§8.4) with the original seq; preserve legacy payload_ids referenced by events/checkpoints (map old payload rows to new rows with the same payload_id and first_seq; asset-bound rows become `storage_kind=asset` when the asset still exists in the business `file_assets`, else blobs).
- Sets next/committed seq; projected/archived/checkpoint seq 0 (the worker rebuilds records, checkpoints and segments); exports are not migrated.
- `--verify`: event counts, seq contiguity, content_hash equality, payload sha256 equality for blobs; exits non-zero on mismatch.
- `--finalize-drop`: requires a previous successful `--verify` marker in `trajectory_worker_state`; drops `legacy_trajectory_*` in the business DB.
- Idempotent and resumable (skips trajectories already converted with matching counts).

### 8.15 Embedded mode (`embedded.py`)
`start_embedded_worker(app)` initializes the trace engine (default `sqlite+aiosqlite:///<backend>/.openbox/trajectory.db`), runs trace migrations (SQLite: `TraceBase.metadata.create_all`), starts `WorkerServices`, and mounts the admin routers into the business app. `stop_embedded_worker()` stops services and closes the engine.

---

## 9. Execution runtime fencing and fixes

Source: `maps/runtime.md` §3-4. Required changes:

### 9.1 API (`backend/question/runtime.py`)
```python
class RunRevoked(Exception):          # NOT ValueError, NOT TrajectoryError
    def __init__(self, ticket, reason: str, boundary: str): ...
@dataclass(frozen=True)
class AuxiliaryTicket: session_id: str; user_id: str; generation: int; run_id: str; purpose: str
auxiliary_run: ContextVar[AuxiliaryTicket | None]
def write_verdict(execution, ticket) -> str | None     # "superseded" (generation differs) | "replaced" (other run_id holds row) | None
def start_verdict(execution, ticket, now) -> str | None  # strict owns(): run_id, generation, live lease
def assert_current_locked(execution, session_id: str) -> None   # 0 SQL; checks current_run / auxiliary_run bound to THIS session
async def assert_current(boundary: str, *, progress: bool = False) -> None   # ≤ 1 statement
def revoke(run_id: str, reason: str) -> None           # in-process revoked set (bounded) + trigger_abort(session)
def is_revoked(run_id: str) -> bool
```
- `transaction(session_id, user_id, *, fence: bool = True)` calls `assert_current_locked` after `execution_locked`. Runtime-internal callers pass `fence=False` (`start_run`, `still_current`, `heartbeat`, `finish_run`, `cancel_session`, recovery, continuation/question worker paths).
- `assert_current`: revoked set check; conditional `UPDATE session_executions SET run_progress = (run_progress OR :progress) WHERE session_id=:s AND user_id=:u AND run_id=:r AND generation=:g AND lease_until > :now`; zero rows → classify (one PK SELECT), `revoke`, raise. Auxiliary tickets: PK SELECT, allow when generation equals.
- `still_current` implemented with `assert_current` (returns bool). `heartbeat` uses the conditional update form for `lease_until`.

### 9.2 Insertion points
| Boundary | Location (see maps/runtime.md §4.2) | Check |
|---|---|---|
| Chat writes under the session lock | `runtime.transaction` (covers session.py create_user_message, create_assistant_message, update_message_info, save_part, update_part_data, set_session_status silent path, cron injector transaction) | `assert_current_locked` (write rule) |
| `update_session` field writes | session.py `update_session` | EXISTS fence predicate when a ticket for this session is bound; token/context counters ignore revocation |
| Internal parts / reveals | session/internal_parts.py after `lock_owned_session` | load execution + `assert_current_locked` |
| Step start | agent/loop.py step top (replaces `still_current`) | `assert_current("step")` |
| Before assistant message | agent/loop.py | revoked-set check |
| Chat request start | agent/loop.py before request | `assert_current("request", progress=True)` |
| Any LLM request | agent/llm.py `stream_llm`, `metered_completion` | `assert_current("request")`; auxiliary rule for title/suggestions; skip DB when already checked this step |
| Paid service submit | agent/trajectory.py `capture_service_dispatch` | `assert_current("service")` |
| Tool start | agent/hooks.py (pre-auth becomes revoked check; DB check after permission wait) | `assert_current("tool", progress=True)` |
| Tool streaming output / dispatch loop | hooks.py output callback; processor.py dispatch loop | revoked-set check |
| Subagent spawn | tool/task.py before `_run_child` | `assert_current("spawn")` |
| Cron flush in run | agent/loop.py before flush | `assert_current("flush")`; recording-off injection made atomic (one transaction) |
| Title | agent/loop.py title task | bind `AuxiliaryTicket(purpose="title")`, clear `current_run` in the task |
| Suggestions | agent/loop.py suggestions launch | bind `AuxiliaryTicket(purpose="suggestions")` |

### 9.3 Handling
- `run_loop` treats `RunRevoked` as an abort: no `SESSION_ERROR`, no failed step, finish via `finish_run(aborted=True)` path.
- Replace every `isinstance(e, TrajectoryError)` / `except TrajectoryError` re-raise site classified F or F+R in `maps/producers.md` §3 with `RunRevoked`; delete R-only and defensive-only trajectory handlers. Plain `except ValueError` handlers must not catch `RunRevoked` (it is not a ValueError).
- Fencing works with recording off; `get_run_trace` no longer controls whether run identity is available to fences (fences use `current_run`).

### 9.4 Execution fixes
1. `invalidate_locked`: after recording the terminal fact, set `run_id=None`, `lease_until=None`, `revoke(old_run_id, status)`; keep `run_generation`, `run_origin`, `run_progress`.
2. Terminal facts once per run: in-process guard + deterministic ids `{event_type}:{run_id}`; worker keep-first dedupe makes duplicates harmless. Same for `turn_finish:{turn_id}`.
3. `recover_expired_runs`: per-candidate `try/except Exception` (log with session id, continue). Rows with `run_generation != generation` are silent cleanup (clear run_id/lease; no status change, no error, no publish).
4. `continuation.tick`: recovery, `expire_questions` and resume phases each isolated; `expire_questions` isolates each session; permanent-failure handling no longer treats trajectory errors as `QUESTION_RESUME_FAILED`.
5. `finish_run(..., aborted: bool = False)`: abort-signal exits record status `cancelled`, reason `aborted`.
6. Emits from runtime transactions use `record(..., db=db)` (→ `emit_after_commit` in spool mode).

---

## 10. Producer conversion (business code) — wave 2

Owner: WP-G. Source inventory: `maps/producers.md` §1 and Migration notes A.

- All in-transaction `record(..., db=db)` sites keep calling `record` (dispatches to `emit_after_commit`). Remove every trace-table read and any code whose only purpose was the in-transaction recorder:
  - `session/session.py`: `trajectory_context_in_tx` resolves context only (fencing moved in wave 1); `prepare_trajectory_assets` (reads trace tables, downloads asset bytes) removed; baseline via §5.6; `record_projection_in_tx` asset capture emits asset references; `mark_capture_paused_in_tx` calls replaced by §5.6; `delete_session` emits `session.deleted` control after commit instead of `delete_trajectory_in_tx`.
  - `session/fork.py`: emit `history.forked` with `source_root_session_id`; remove `SessionTrajectory` reads.
  - `session/revert.py`: pause/baseline via §5.6; emits only.
  - `permission/permission.py`: durable reply stored in Redis `perm_reply:{id}` (SETEX 24 h) before publish; `_read_recorded_reply` reads Redis; no trace-table reads.
  - `storage/storage.py` todo events: no `SELECT sessions FOR UPDATE`; read previous value only when recording is enabled; emit after commit.
  - `cron/executor.py`, `cron/injector.py`, `cron/recovery.py`: drop the recording-only session lock; emits after commit.
  - `trajectory/jobs.py`: emit after commit; keep `_trajectory_context` adoption; `operation.late_result` check keeps its `SessionExecution` read.
  - `trajectory/artifacts.py`: producer helpers only (no DB, no bytes): `capture_asset*` emit `artifact.recorded` with `asset_ref` after commit; `revoke_asset_in_tx` → `emit_control(asset.deleted, db=db)`; `read_asset_bytes` stays for business callers only.
  - `api/assets.py`, `sandbox/assets.py`, `tool/image_gen.py`, `tool/video_production.py`, `tool/douyin_publish.py`: no recording-only downloads; asset references.
  - `agent/trajectory.py`: `RequestCapture.start` performs no DB transaction and no media retention; emits `request.prepared` (with `media_sources` when available) and `request.started`; `stream_chunks` emits each chunk without awaiting receipts; `register_owned_media_inputs` / `retain_derived_media_inputs` record OSS-key references bound to the source asset (no downloads, no transient FileAsset rows); `_link_service_job` stays (business link) but only when enabled.
  - `trajectory/files.py`: emit; keep sanitize before hashing.
  - `main.py` (wave 2, WP-E): start emitter + meta sync; embedded worker when mode embedded; no archive worker, no `resume_exports`, no admin routers in external mode; shutdown closes emitter.
- Business DB retirement (§6.9): rename migration, unhook models, readiness schema update, remove imports of `db.models.trajectory` from business modules.
- Default `TRAJECTORY_SINK=spool`; remove the legacy `db` sink code paths (`append_events_in_tx`, `ensure_trajectory_in_tx`, `_append_prepared`, stream queues) from `recorder.py` once no caller remains (tests are adapted in wave 3).
- Meta sync task (§5.8).

---

## 11. Frontend and nginx

### 11.1 nginx (`frontend-v2/nginx.conf`, `frontend-v2/Dockerfile`, `scripts/test-nginx.mjs`)
- `set $trajectory_upstream http://${TRAJECTORY_HOST};` with `ENV TRAJECTORY_HOST=backend:8080` default in the Dockerfile.
- `location ^~ /api/admin/trajectories/ { proxy_pass $trajectory_upstream; ...same headers as /api/... }`
- `location = /ws/admin/trajectories { proxy_pass $trajectory_upstream; proxy_http_version 1.1; Upgrade/Connection headers; proxy_read_timeout 3600s; proxy_send_timeout 3600s; }`
- Keep the gzip settings from PR #35 (already merged on this branch).
- `test-nginx.mjs`: second fixture upstream with alias `trajectory-worker`; asserts routing of both paths (including WS upgrade) and that `/api/echo` still reaches the backend; container starts without `TRAJECTORY_HOST`.
- `vite.config.ts`: dev proxy entries for the two paths before `/api` and `/ws`, target from `VITE_TRAJECTORY_PROXY_TARGET` (default the backend target).

### 11.2 Admin UI (`frontend-v2/src/features/admin-trajectories/`)
- Polling (plan 4.3: an idle session page with a healthy socket makes at most 4 requests per minute): events poll 30 s while the socket is connected, 2 s while disconnected, immediate on visibility; header refetch on a WS hint and 60 s after its last answer while connected (30 s while disconnected); list probe 30 s on the list page, which opens no socket; record detail throttle 2 s; payload `?meta=1` revalidation only while the payload is on screen, with an overdue check when it scrolls back into view, instead of re-downloading bytes every 15 s.
- Lazy request input: RequestInputPanel, SystemPromptPanel, SystemDiffPanel, ToolCatalogPanel and ContentActions request the record with `expand=refs` and resolve `$ref` values on demand through `/blobs/{sha256}?through_seq=H` (cached by sha256, immutable); rendering after resolution is identical to today. The local projection path (`/events`, `/checkpoint`) is unchanged.
- Protocol types: add `$ref` envelope, `expand` param, blob endpoint, `capabilities.refs`.
- Vitest coverage for polling constants, lazy resolution, meta revalidation, and nginx tests.

---

## 12. Operations and deployment assets (`deploy/gw2/`)

- `docker-compose.trajectory.yml` (overlay used with the server compose files):
  - `trajectory-worker` service: image `openbox-backend:${OPENBOX_IMAGE_TAG}` (pinned in override like backend), command `sh -ec "alembic -c alembic_trajectory.ini upgrade head && exec python -m trajectory.worker"`, `env_file: config/backend.env`, environment `TRAJECTORY_DATABASE_URL=postgresql+asyncpg://openbox_trace:${OPENBOX_TRACE_DB_PASSWORD}@postgres:5432/openbox_trace` (dedicated role), `DATABASE_URL: ""` (the business connection string of `backend.env` never reaches the worker), `REDIS_URL`, `TRAJECTORY_WORKER_MODE=external`, `TRAJECTORY_BLOB_PROVIDER=oss`, `TRAJECTORY_BACKEND_INTERNAL_URL=http://backend:8080`, `ALIYUN_CLI_CONFIG=/run/secrets/aliyun-config.json`; volumes `trajectory-spool:/var/lib/openbox/trajectory-spool`, the aliyun secret (read-only); `depends_on` postgres and redis `service_healthy`; healthcheck `curl -fsS http://127.0.0.1:8090/health`; `cpus: 1.0`; `mem_limit: 1g`; restart unless-stopped; logging limits. Service name must not be `backend-worker`.
  - `backend`: add the spool volume and `TRAJECTORY_SPOOL_DIR=/var/lib/openbox/trajectory-spool`, `TRAJECTORY_WORKER_MODE=external`.
  - `frontend`: `TRAJECTORY_HOST: trajectory-worker:8090`.
  - `postgres`: `mem_limit: 2g`, command `postgres -c shared_buffers=512MB -c effective_cache_size=1GB -c shared_preload_libraries=pg_stat_statements -c pg_stat_statements.track=all -c max_connections=200`.
  - volume `trajectory-spool`.
- `scripts/create-trace-db.sh`: idempotent. Role `openbox_trace` (LOGIN; password `OPENBOX_TRACE_DB_PASSWORD` from `/opt/openbox/.env`, sent to psql on stdin in a session with statement logging and `pg_stat_statements` tracking off, never in a process argument list; refuses to run without it) with role defaults `statement_timeout = '5s'` and `work_mem = '32MB'`; database `openbox_trace` owned by the role with `pg_trgm`; `pg_stat_statements` in `openbox` once preloaded.
- The example files `docker-compose.base.example.yml`, `docker-compose.override.example.yml` and `env.example` are sanitized copies of the gw2 files (placeholders only) used to validate the overlay; `.env` keys: `OPENBOX_IMAGE_TAG` (stale, the override pins win), `OPENBOX_DB_PASSWORD`, `OPENBOX_TRACE_DB_PASSWORD`, `COMPOSE_FILE`.
- `scripts/prune-images.sh`: keep images used by any container plus the newest 3 tags per repository; dry-run by default.
- `scripts/pg-backup.sh` + `trajectory/ops/backup.py`: `pg_dump -Fc` of `openbox` and `openbox_trace` inside the postgres container, upload to OSS `backups/postgres/{YYYYMMDD}/` through a presigned PUT generated with the mounted credentials; verification of size; local copy removed.
- `scripts/restore-check.sh` + `trajectory/ops/backup.py download`: restore a backup (downloaded with size and sha256 checks, or a local file) into a scratch database `openbox_restore_check_*`, compare its tables with the dump's table of contents, drop it; never touches the live databases.
- `scripts/push-metrics.sh`: host disk usage, spool size/oldest age, worker `/metrics`, backend OOM kill count (`journalctl -k`), trace DB size, `backend_cpu_percent` and `backend_mem_percent` (`docker stats --no-stream`), `business_trajectory_statements` (`pg_stat_statements` entries of `openbox` matching `trajectory_`; 0 without the extension) → `python -m trajectory.ops.cms push` executed inside the worker container, which also passes through the wave-3 worker metric names.
- systemd units + timers: `openbox-trajectory-metrics.timer` (every minute), `openbox-pg-backup.timer` (daily 03:30 Asia/Shanghai, persistent), `openbox-prune-images.timer` (weekly); installed and uninstalled by `scripts/install-timers.sh`.
- `oss-lifecycle.xml`: `trajectories/` transition to IA after 30 days; `trajectories/_exports/` expire after 30 days; `backups/postgres/` expire after 30 days; abort incomplete multipart uploads after 7 days. Applied with `scripts/apply-oss-lifecycle.sh` (from an operator machine with the aliyun CLI) after reading and merging any existing rules.
- `scripts/setup-alarms.sh`: CloudMonitor rules (contact group `云账号报警联系人`) for host disk ≥ 80 %, spool bytes ≥ 1 GiB, spool oldest age ≥ 60 s, worker health down, gaps per hour > 0, projection lag ≥ 5000, blob put failures ≥ 10 / 5 min, trace DB ≥ 20 GiB, OOM kill > 0, archive lag ≥ 50000 events for 15 min, `hot_partitions` > 10, backend CPU or memory > 90 % for 5 min, business-DB `trajectory_` statements > 0, `events_ingested_24h` > 1,000,000 (INFO).
- Drills: `drill-worker-stop.sh` (15 minutes; the spool drains within 5 minutes of the restart), `drill-blob-outage.sh` (worker env `TRAJECTORY_BLOB_FAULT=put:1.0`, 30 minutes), `drill-spool-full.sh` (backend `TRAJECTORY_SPOOL_MAX_BYTES` temporarily tiny), `drill-delete-session.sh` (deletes an internal test session through the business API, then requires admin API 404/410 and, through `trajectory.ops.deletion`, a tombstoned trajectory with no pending GC and no object under its prefix), `rebuild-trace-db.sh` (restore segments into a scratch database and compare digests).
- Release comparison: `trajectory.ops.latency` compares per-route p95 of the frontend access log `rt=` field and backend `docker stats` samples between a recording-off and a recording-on window.
- `RUNBOOK.md`: release procedure (§12.1), verification, rollback, drills, alarms, retention.
- `k8s/`: add the worker as a sidecar container sharing an `emptyDir` spool in `base.yaml` and `aks.yaml`, plus ingress paths for the two admin routes (marked untested legacy).
- Docs: `docs/DEPLOY.md` section for the new topology; `docs/SESSION_TRAJECTORY_PROTOCOL.md` updated (spool, fail-open contract, envelopes, gaps, new endpoints).

### 12.1 Release order (production)
1. Build `linux/amd64` backend and frontend images from the release commit.
2. Upload via OSS `_deploy-tmp/`, load on gw2, keep previous tags.
3. Backups: config, compose files, `pg_dump openbox`.
4. Create `openbox_trace`; apply postgres tuning (postgres recreate, maintenance window, 0 active sessions check).
5. Start `trajectory-worker` (migrations run; reads spool; recording still disabled).
6. Switch backend (`--no-deps backend`); the business migration drops the old trajectory tables. Verify, then `SELECT pg_stat_statements_reset()` in `openbox`.
7. Delete the old payload files under `trajectories/` of the business `blob-data` volume, keeping `policies/`.
8. Switch frontend (routing to worker).
9. Enable recording for internal users, verify isolation and metrics, then all users.
10. Install timers, lifecycle rules, alarms; run drills.

---

## 13. Configuration reference

| Variable | Default | Used by |
|---|---|---|
| `TRAJECTORY_WORKER_MODE` | `external` if `JWT_SECRET` set else `embedded`; `off` disables the pipeline (no emitter, no metadata sync, no worker) | backend, worker |
| `TRAJECTORY_SPOOL_DIR` | `/var/lib/openbox/trajectory-spool` when it exists, else `<backend>/.openbox/trajectory-spool` | both |
| `TRAJECTORY_EMIT_QUEUE_BYTES` | 67108864 | backend |
| `TRAJECTORY_EMIT_MAX_EVENT_BYTES` | 33554432 | backend |
| `TRAJECTORY_SPOOL_FILE_BYTES` | 8388608 | backend |
| `TRAJECTORY_SPOOL_FILE_MS` | 1000 | backend |
| `TRAJECTORY_SPOOL_MAX_BYTES` | 2147483648 | backend |
| `TRAJECTORY_SPOOL_MIN_FREE_BYTES` | 1073741824 (free bytes the emitter leaves on the spool's file system: event lines are dropped with `disk_full` below it, writer-owned lines below half of it; 0 disables the floor, §5.2) | backend |
| `TRAJECTORY_SPOOL_BLOB_MIN_BYTES` | 1024 (size above which `request.prepared` input values move to spool blobs, §3.5) | backend |
| `TRAJECTORY_BUDGET_REFRESH_MS` | 5000 | backend |
| `TRAJECTORY_META_SYNC_SECONDS` | 30 | backend |
| `TRAJECTORY_DATABASE_URL` | embedded: `sqlite+aiosqlite:///<backend>/.openbox/trajectory.db`; external: required | worker |
| `TRAJECTORY_DB_POOL_SIZE` / `TRAJECTORY_DB_POOL_OVERFLOW` | 5 / 5 | worker |
| `TRAJECTORY_WORKER_HOST` / `TRAJECTORY_WORKER_PORT` | 0.0.0.0 / 8090 | worker |
| `TRAJECTORY_INGEST_POLL_MS` | 200 | worker |
| `TRAJECTORY_INGEST_BATCH_LINES` / `_BYTES` | 2000 / 16777216 | worker |
| `TRAJECTORY_INGEST_MAX_BATCH_FAILURES` | 10 (consecutive failures of one file batch before the file is quarantined with a `recording.gap`; failures while the trace database does not answer and transient ones — timeouts, lock or serialization conflicts, lost connections, a full disk — never count; the counts survive a worker restart) | worker |
| `TRAJECTORY_SPOOL_ABANDON_SECONDS` | 60 | worker |
| `TRAJECTORY_SPOOL_QUARANTINE_MAX_BYTES` | 268435456 (data bytes kept in `quarantine/`; the oldest quarantined files and their `.reason` sidecars are deleted first) | worker |
| `TRAJECTORY_SPOOL_QUARANTINE_RETENTION_DAYS` | 7 (quarantined files older than this are deleted; age from the `.reason` sidecar) | worker |
| `TRAJECTORY_INLINE_BYTES` | 65536 | worker |
| `TRAJECTORY_RECORD_INLINE_BYTES` | 16384 | worker |
| `TRAJECTORY_PROJECTION_BATCH_MS` / `_EVENTS` | 250 / 200 | worker |
| `TRAJECTORY_PROJECTION_BATCH_BYTES` | 8388608 (byte bound of one projection batch) | worker |
| `TRAJECTORY_CHECKPOINT_INTERVAL` | 1000 | worker |
| `TRAJECTORY_SEGMENT_EVENTS` / `_MAX_BYTES` / `_IDLE_SECONDS` | 1000 / 4194304 / 300 | worker |
| `TRAJECTORY_SEGMENT_CACHE_BYTES` / `TRAJECTORY_BLOB_CACHE_BYTES` | 134217728 / 268435456 | worker |
| `TRAJECTORY_HOT_DAYS` / `TRAJECTORY_DEDUPE_DAYS` | 7 / 30 | worker |
| `TRAJECTORY_CONTENT_RETENTION_DAYS` / `TRAJECTORY_EXPORT_RETENTION_DAYS` | 180 / 30 | worker |
| `TRAJECTORY_EXPORT_MAX_BYTES` | 268435456 (size cap of one export archive) | worker |
| `TRAJECTORY_BUDGET_TRAJECTORY_EVENTS` | 50000 (degraded) | worker |
| `TRAJECTORY_BUDGET_TRAJECTORY_BYTES` | 209715200 (degraded) | worker |
| `TRAJECTORY_BUDGET_TRAJECTORY_BLOCK_BYTES` | 1073741824 (blocked) | worker |
| `TRAJECTORY_BUDGET_USER_DAILY_BYTES` | 2147483648 (degraded for the rest of the UTC day) | worker |
| `TRAJECTORY_BLOB_PROVIDER` | `local` | worker |
| `TRAJECTORY_BLOB_LOCAL_PATH` | `<backend>/.openbox/trajectory-blobs` | worker |
| `TRAJECTORY_OSS_BUCKET` / `_REGION` / `_ENDPOINT` / `_PREFIX` / `_INTERNAL` | `OSS_BUCKET` / `OSS_REGION` / derived / `trajectories/` / `true` (asset payload reads of an embedded worker use the public endpoint unless `_INTERNAL` is set) | worker |
| `TRAJECTORY_BACKEND_INTERNAL_URL` | `http://backend:8080` | worker |
| `TRAJECTORY_AUTH_CACHE_SECONDS` | 5 | worker |
| `TRAJECTORY_AUDIT_MAX_ATTEMPTS` / `TRAJECTORY_AUDIT_MAX_AGE_SECONDS` | 30 / 259200 (after either limit an audit outbox record is dead-lettered) | worker |
| `TRAJECTORY_BLOB_FAULT` | unset (drills/tests only) | worker |
| `TRAJECTORY_CMS_REGION` / `TRAJECTORY_CMS_GROUP_ID` | `cn-shanghai` / unset | ops |
| `OPENBOX_TRACE_DB_PASSWORD` | unset; deploy `/opt/openbox/.env`, 16–128 characters of `A-Za-z0-9._~-` (`openssl rand -hex 32`) | overlay (`openbox_trace` role URL), `create-trace-db.sh` |
| `OPENBOX_TRACE_ROLE` / `OPENBOX_RESTORE_DIR` | `openbox_trace` / `/var/backups/openbox/restore-check` | ops scripts |
| existing: `TRAJECTORY_RECORDING_ENABLED`, `TRAJECTORY_RECORD_USER_IDS`, `TRAJECTORY_ADMIN_ENABLED`, `TRAJECTORY_ADMIN_USER_IDS`, `JWT_SECRET`, `INTERNAL_API_TOKEN`, `REDIS_URL` | | both |

Every integer setting has a minimum (at least 1, except `TRAJECTORY_SPOOL_MIN_FREE_BYTES`, where 0 disables the disk floor); a value that is not an integer or is below its minimum falls back to the default with a warning.

---

## 14. Testing

### 14.1 Harness (`backend/tests/support/trajectory.py`, fixtures in `backend/tests/conftest.py` or `tests/integration/conftest.py`)
- `spool_dir` (tmp path, env set), `emitter` (started/closed per test; `reset_emitter_for_tests`).
- `memory_blob_store` (`MemoryBlobStore` installed via `set_blob_store`).
- `trace_db` (SQLite file per test via `init_trace_engine` + `create_all`; PostgreSQL opt-in with `TRAJECTORY_TRACE_TEST_DATABASE_URL` pointing at a disposable database whose name starts with `openbox_trace_test_`).
- `drain()`: flush emitter → run spool reader + ingest + projection (+ optional checkpoints/archive) synchronously until idle → returns counters.
- `worker_client` / `worker_socket`: worker ASGI app with overrides for viewer introspection (fake backend endpoint) and a shared in-memory ticket cache.
- `business_seed`: users, workspaces, projects, sessions + meta sync emission.

### 14.2 Local PostgreSQL for PG-only tests
`docker run -d --rm --name <unique> -e POSTGRES_PASSWORD=trace -p <port>:5432 public.ecr.aws/docker/library/postgres:16-alpine` (image is present locally). Each work package uses its own container name and port (§15). Tests that require PostgreSQL skip when the env URL is absent.

### 14.3 Suites and gates
- Backend: `cd backend && uv run pytest tests -q` must pass except the known failure `tests/unit/test_internal_tunnel_keys.py::test_desktop_preflight_has_stable_503_code` (pre-existing). No new skips for non-PG tests.
- Frontend: `cd frontend-v2 && npm ci && npx vitest run` and `npm run test:nginx` when nginx changes.
- Acceptance tests (wave 3 must implement all 35 from `maps/tests.md` "Acceptance tests to add", plus): business-DB isolation test (SQLAlchemy `before_cursor_execute` listener asserts no statement mentions `trajectory_` during business API flows with recording on), emitter micro-benchmark (`emit` p99 ≤ 50 µs for 2 KiB events, ≤ 2 ms for 512 KiB), chaos tests (emitter write failure, worker absent, blob store failing, spool full) where business requests all succeed.

---

## 15. Work packages

Waves run in parallel inside a wave. Each package works in its own git worktree on branch `wp/<id>` created from the wave base commit given in the prompt, commits its work, and returns a report (§15.4). The orchestrator merges packages into `feat/trajectory-rearch` in the listed order.

### 15.1 Wave 1 (base: current `feat/trajectory-rearch`)
| id | scope | owns (exclusive) | must not touch | PG port |
|---|---|---|---|---|
| `w1-emitter` | §3, §4, §5.1-5.5, §5.7 reader side, `TRAJECTORY_SINK` switch (default `db`) | `trajectory/emitter.py`, `trajectory/spool.py`, `trajectory/recorder.py` (dispatch only), `trajectory/__init__.py`, `trajectory/config.py`, new tests `tests/unit/test_trajectory_emitter*.py`, `tests/unit/test_trajectory_spool*.py`, `trajectory/benchmark_emit.py` | business modules | — |
| `w1-tracedb` | §6.1-6.3, alembic chain, PG partition helpers module `trajectory/store/partitions.py`, model-level tests, migration head test | `trajectory/store/**`, `backend/alembic_trajectory.ini`, `tests/unit/test_trace_*.py`, `tests/integration/test_trace_pg_*.py` | `db/**`, business migrations | 55431 |
| `w1-storage` | §7 | `core/oss.py` (additions only), `trajectory/storage.py`, `tests/unit/test_oss_server_ops.py`, `tests/unit/test_trajectory_storage_blobs.py` | payload.py | — |
| `w1-fencing` | §9 | `question/**`, `session/abort.py`, `session/status.py`, `session/internal_parts.py`, fencing hunks in `agent/loop.py`, `agent/llm.py`, `agent/hooks.py`, `agent/processor.py`, `agent/suggestions.py`, `agent/trajectory.py` (`capture_service_dispatch` check only), `tool/task.py`, `cron/injector.py`, `session/session.py` (fence + `update_session` predicate + removal of fencing inside `trajectory_context_in_tx`), all trajectory-error re-raise sites repo-wide, runtime tests | recorder internals, emitter | 55432 |
| `w1-frontend` | §11 | `frontend-v2/**` | backend | — |
| `w1-ops` | §12 (assets and docs; no production access) | `deploy/**`, `k8s/**`, `docs/DEPLOY.md` (new section only), `trajectory/ops/**` | backend runtime modules | — |

Merge order: storage, tracedb, emitter, fencing, frontend, ops.

### 15.2 Wave 2 (base: merged wave 1)
| id | scope | owns |
|---|---|---|
| `w2-ingest` | §8.1-8.7, §8.14, §5.7 writer side | `trajectory/worker/{__init__,settings,lock,spool_reader,ingest,content,meta,budgets,notify,services}.py`, `trajectory/tools/**`, ingest tests |
| `w2-projection` | §8.8-8.9 | `trajectory/worker/projection.py`, `trajectory/repository.py`, `trajectory/payload.py`, `trajectory/projector.py` (hints parameter only), projection/read tests |
| `w2-archive` | §8.10-8.11, exports | `trajectory/worker/archive.py`, `trajectory/worker/retention.py`, `trajectory/lifecycle.py`, `trajectory/export.py`, tests |
| `w2-service` | §8.12-8.13, §8.15, backend internal endpoints, `main.py` lifecycle | `trajectory/worker/{app,routes,ws,metrics,embedded,__main__}.py`, `trajectory/auth.py`, `api/admin_trajectories.py` and `api/admin_trajectory_ws.py` (become thin re-exports or are removed), `api/internal.py`, `main.py`, service tests |
| `w2-producers` | §5.6, §5.8, §6.9, §10 | business modules listed in §10, `db/models/**`, `db/base.py`, business migrations, `trajectory/{artifacts,jobs,files,producers,meta_sync}.py`, `trajectory/recorder.py` (legacy removal), producer tests |

Interfaces between wave-2 packages are exactly those in §§5-8; a package that needs a function owned by another package in the same wave codes against the spec signature (and may add a minimal local stub in tests only).

### 15.3 Wave 3 (base: merged wave 2)
| id | scope |
|---|---|
| `w3-tests` | harness consolidation (§14.1), adapt/rewrite the 161 legacy trajectory tests per `maps/tests.md`, all acceptance/isolation/benchmark/chaos tests, `scripts/trajectory_dev_server.py` and `trajectory/benchmark*.py` rewrite, full backend and frontend suites green |
| `w3-docs` | protocol doc, runbook consistency, implementation status doc |

### 15.4 Report format (final message of every package)
```
branch: wp/<id>
base: <sha>
head: <sha>
summary: <what was implemented>
files: <changed files>
tests: <commands run and results, counts>
spec_deviations: <each deviation with reason, or "none">
open_issues: <known gaps, or "none">
```

### 15.5 Agent rules
- Work only inside the assigned worktree; commit with clear messages ending with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Do not access production (no aliyun CLI, no OSS, no SSH, no RunCommand). Do not push to remote.
- Do not modify files owned by another package in the same wave; if a change is unavoidable, keep it minimal, mention it under `spec_deviations`.
- Keep behavior of untouched features unchanged; follow surrounding code style; no new dependencies beyond `orjson` and `zstandard` (already added).
- Tests must run locally; PostgreSQL tests use the package's own container name/port and stop it at the end.
