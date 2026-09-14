ers and the DB reads each performs

| Helper | Location | DB reads | Behaviour |
|---|---|---|---|
| `context_for_session(db, user, session, **ids)` | recorder.py:32-40 | `db.get(Session, session_id)` (identity-map cached per AsyncSession) | Ownership is checked first: a missing, deleted or foreign session raises OwnershipError. If `current()` has the same user and `source_session_id == session_id`, it returns `inherited.derive(**ids)` and keeps the root mapping. Otherwise it builds a new root with `workspace_id` from the row. Not wrapped, so DB errors surface raw. |
| `activity_context(db, user, session, *, saved=None, **ids)` | producers.py:11-23 | None, or the single Session read in `context_for_session` | Returns None when disabled. Prefers the `saved` dict over `current()`. A user mismatch raises ValueError. A matching `source_session_id or session_id` returns `derive(**ids)`. A saved context with a different source raises ValueError. Anything else falls back to `context_for_session`. |
| `append_activity` | producers.py:26-31 | activity_context, then `record(db=db)` | Unused |
| `saved_context(ctx=None)` | producers.py:34-36 | none | `(ctx or current()).to_dict()` when enabled |
| `context_for_tool(ctx)` (adapter) | agent/trajectory.py:404-416 | None when `ctx.trace_context` or `current()` is set; otherwise a new transaction plus `context_for_session` | Raises ValueError when the context's user or source differs from the tool ctx. Returns None when disabled. |
| `trajectory_context_in_tx` (session) | session/session.py:500-528 | `db.get(SessionExecution)`: a fencing read when the inherited context has run_id and generation (:510-516), or a read of the saved `trace_context` (:518-527); then possibly `context_for_session` | Returns None when disabled (:503-504). Raises `TrajectoryError("Superseded execution cannot update the chat projection")`. |
| `get_run_trace(ticket)` | question/runtime.py:42-55 | New transaction, `db.get(SessionExecution)` | Returns the saved context only if `run_id` and `generation` match the ticket. Returns None when disabled. |
| `record_job_in_tx` | jobs.py:14-66 | Two `db.get(Session)` calls, `activity_context`, and `db.get(SessionExecution)` for late results | §8.1 |

### 3.4 Persisted contexts
Spool lines and the worker must stay compatible with `to_dict()` and `from_dict()`.

| Location | Written | Read |
|---|---|---|
| `SessionExecution.trace_context` (db/models/question.py:22) | session/session.py:723-724; question/runtime.py:80; question/question.py:133-135 | question/runtime.py:52,95,253,311; session/session.py:519-527 |
| `CronRun.trace_context` (db/models/cron.py:108) | cron/executor.py:643; cron/injector.py:298 | cron/executor.py:714-720; cron/injector.py:265-268,288-289; cron/recovery.py:76-77 |
| `QuestionCheckpoint.continuation["trace_context"]` | question/question.py:132 | question/question.py:115-117 |
| Job `request_data` or `details` keys `_trajectory_context` and `_trajectory_request_ids` | jobs.py:33-34; agent/trajectory.py:295-297 | jobs.py:20,48; video/job_recovery.py:273-276; agent/trajectory.py:256 |
| `PendingPermission.trace_context` (memory and Redis request data) | permission/permission.py:59-78,321 | permission/permission.py:397,437,450 |
| `ToolContext.trace_context` (memory) | hooks.py:73; agent/trajectory.py:500-503 | files.py:12,19; tool/tool.py:165; tool/bash.py:184 |

---

## 4. Write path

### 4.1 `record()` (recorder.py:201-224)
1. `context = context or current()`. With no context it returns None (:203-205).
2. If `not enabled(user)`, it calls `mark_capture_paused_in_tx` in `db` or in a new transaction, then returns None. No content is written (:206-212).
3. Builds `{type, data, **ids, event_id?, occurred_at?}` (:213-217).
4. **With `db`:** `append_events_in_tx(db, context, [item])` returns a `PendingRange` and nothing commits (:218-219). No stream barrier applies, so these writes are ordered only by the row lock.
5. **Without `db`:**
   - `_retire_previous_failed_run` (:220).
   - `flush(context)` (:221), which awaits scheduled chunk receipts and then runs an extra read-only transaction for the watermark. `record()` discards that result.
   - A new `get_db_session()` transaction runs the append and commits (db/base.py:451-470).
   - Returns the committed event dict (:222-224).

### 4.2 `append_events_in_tx(db, context, events)` (recorder.py:175-198)
1. `_retire_previous_failed_run(context)` (:176).
2. `prepare` runs on every event before any write, so validation is all-or-nothing (:177).
3. An event whose `source_session_id` differs from the context's triggers `_validate_owner(db, context.derive(source_session_id=...))` (:179-181).
4. **Lease fencing** applies to `request.started`, `request.delta`, `tool.started`, `tool.output` and `step.started` when the event has `run_id` and `generation` is not None (:182-187):
   - It loads `SessionExecution` for the event's source session with no lock. A missing row passes.
   - It raises OwnershipError "Execution lease is stale; new side effects and output cannot be recorded" when `execution.user_id` differs, or, unless the event is auxiliary, when `run_id` or `run_generation` differs.
   - Auxiliary exception: `data.purpose in {title, suggestions}`, `execution.run_id is None` and `execution.generation == generation` (:185).
5. `ensure_trajectory_in_tx(db, context)` (:188).
6. **Recovered gap** (:189-197):
   - Applies if `_failed_streams[(id(loop), user, root)]` exists and this DB session has not already written it.
   - Appends a `recording.gap` using the *failed stream's* context and its stable `evt_gap_*` id, then adds the key to `session.info["trajectory_recovered_streams"]`.
7. `_append_prepared(db, context, trajectory, prepared)` (:198).

### 4.3 `_retire_previous_failed_run(context)` (recorder.py:373-384)
- Acts only if the queue for `(loop, user, root)` exists and has a `failure`.
- It retires the queue when the new context is a fresh run (`(run_id, generation)` differs) or a fresh unleased request (both `run_id` None and `request_id` differs).
- Retiring means awaiting the shielded commit task and then calling `_remember_failed_stream`.

### 4.4 `_validate_owner(db, context)` (recorder.py:43-63)
1. The owner is `Session[context.session_id]`. Missing, deleted or a different user raises "Trajectory owner session is missing, deleted or mismatched".
2. If `context.workspace_id` is set and differs from the owner's, it raises "Trajectory workspace does not match its owner".
3. It walks `source` up `parent_id` until it reaches `owner.id`:
   - Each hop must exist, not be deleted, and have the same user, or it raises "Trajectory child ownership does not match".
   - A cycle or 100 hops raises "Invalid source session ancestry".
   - Running off the chain (source is not a descendant) raises "Source session is not delegated by the trajectory owner".
4. Returns the owner row.

### 4.5 `ensure_trajectory_in_tx(db, context, baseline=None)` (recorder.py:66-103)
1. `owner = _validate_owner` (:67).
2. `INSERT session_trajectories(id=trj_<uuid>, user_id, session_id=context.session_id (root), workspace_id=owner.workspace_id, started_at=updated_at=now, next_seq=1, committed_seq=0, projected_seq=0, schema_version=1, recording_status="recording") ON CONFLICT DO NOTHING`, using the SQLite or PostgreSQL insert (:28-29,69-77). The comment at :74-76 notes that either unique index may detect the conflict first.
3. `SELECT ... WHERE session_id=owner.id` with `with_for_update(key_share=True)` and `populate_existing` (:78). On PostgreSQL this renders `FOR NO KEY UPDATE`, an exclusive row lock held to the end of the transaction. SQLite ignores it.
4. A different user or `deleted_at` set raises "Deleted or mismatched trajectory cannot be restarted" (:79-80). This is the tombstone check.
5. **Resume** (:81-86), when status is `paused` and `enabled(user)`:
   - Appends `recording.gap {phase:"resumed", reason:"recording_reenabled", previous_committed_seq}`.
   - Sets status `gap`.
   - Stores `session.info["trajectory_resume_baselines"][trj] = "evt_baseline_{trj}_{committed_seq}"`, taken after the gap's seq.
6. **Start** (:87-94), when `committed_seq == 0`: appends `trajectory.started` with id `evt_start_{trj}`, `occurred_at=timestamp` and data `{existing_session: baseline.legacy_session or baseline.existing_session or None, coverage_start: iso(timestamp), schema_version: 1}`.
7. **Baseline** (:95-102), when `baseline` is given:
   - The id is the pending resume id or `evt_baseline_{trj}`.
   - `db.get(TrajectoryEvent, id)`; if it does not exist, appends `baseline.captured` with `data=baseline`.
   - Pops the pending entry.
   - Result: exactly one initial baseline and one per resume. The pending id survives only inside the same DB session. It is not popped on commit, but sessions are single-use.
8. **Root and child mapping.** The container is keyed by the root `session_id`. Child events carry `source_session_id=child`, and children never get their own row (tests/unit/test_trajectory_session_runtime.py:92-113).
9. **Baseline producers:**
   - session/session.py:596-637 builds `{legacy_session, source_session_id, history:[{id, role, parent_id, model, agent, finish, summary, parts}], settings:{model, agent, variant, project_id, workspace_id}, recording_boundary:"new_activity_only", artifacts?}`. It skips the baseline when `evt_baseline_{trj}` exists and the status is not paused (:606-608).
   - jobs.py:35-40 records job adoption as a separate `baseline.captured` with id `job_adopt:{id}`.

### 4.6 `_append_prepared(db, context, trajectory, prepared)` (recorder.py:114-172)
1. `UPDATE session_trajectories SET next_seq=next_seq WHERE id` (:117-118) takes the row lock on PostgreSQL and the database write lock on SQLite.
2. For each item: `data = sanitize(data)`, then `hash = digest(item minus event_id/occurred_at)`. The same id with a different hash in one batch raises IdempotencyConflict "Repeated event ID has conflicting content"; identical duplicates collapse (:119-128).
3. `SELECT trajectory_events WHERE event_id IN (...)`. A row with a different trajectory or hash raises IdempotencyConflict "Event ID was already committed with different content or ownership"; matching rows count as existing (:129-134). The lock is taken first, so under READ COMMITTED concurrent duplicates within one trajectory are seen.
4. For new events (:137-143): `UPDATE ... SET next_seq=next_seq+n, committed_seq=next_seq+n-1, updated_at=now() RETURNING next_seq, committed_seq`, with `start = next_seq - n`.
5. For each new event at `seq = start + i` (:146-161):
   - If `len(canonical(data)) > INLINE_BYTES`, calls `store_json(db, trj, data, first_seq=seq)`. That is a SELECT dedupe on (trajectory, sha256, media_type, source_asset_id IS NULL), then an INSERT of the payload with bytes and `storage_status=pending`, then a flush (payload.py:74-99). A deleted duplicate raises OwnershipError "Removed trajectory content cannot be recreated" (payload.py:82-84).
   - Builds the `TrajectoryEvent` row (§2.6) and `db.add`s it. The projection copy keeps the unexternalized data.
6. Sets the in-memory `next_seq` and `committed_seq`, then `db.flush()` (:162-164).
7. `project_events_in_tx(db, trajectory, events)` runs in the same transaction (:165-166; repository.py:70-146). It reads affected `trajectory_records`, plus message/part/system rows and the running rows touched by gaps or interrupts. It upserts records (`data`, `summary`, `search_text`) and the summary (statistics diff, running_status, model, `recording_status="gap"` on gaps), sets `projected_seq=committed_seq`, and flushes.
8. Stores `session.info["trajectory_notifications"][trj] = {user_id, owner_user_id, session_id (root), trajectory_id, committed_seq}`; the last write wins (:167-169).
9. Returns `PendingRange(trj, first_seq, last_seq, events sorted by seq)`. The events include pre-existing duplicates with their original seqs. When nothing is present, both ends are `committed_seq` (:170-172).

### 4.7 Queries and locks per append (PostgreSQL)

| # | Statement | Lock effect |
|---|---|---|
| 1 | (`record` without db) separate transaction: `SELECT committed_seq` (recorder.py:441-443) | none |
| 2 | `db.get(Session)` for owner and ancestors, twice when a source override is present (:44,57,181) | none |
| 3 | `db.get(SessionExecution)` lease check (:184) | none (unlocked) |
| 4 | `INSERT ... ON CONFLICT DO NOTHING` (:77) | Concurrent first contact waits on the unique index |
| 5 | `SELECT ... FOR NO KEY UPDATE` (:78) | Exclusive row lock until commit |
| 6 | `db.get(TrajectoryEvent, baseline_id)` (:98) | none |
| 7 | `UPDATE next_seq=next_seq` (:117) | Row lock (PostgreSQL) / write lock (SQLite) |
| 8 | `SELECT events WHERE event_id IN` (:131) | Runs after the lock |
| 9 | `UPDATE ... RETURNING` seq block (:139-143) | Allocates a contiguous block |
| 10 | Payload SELECT/INSERT, only for large data (payload.py:79-94) | none |
| 11 | `INSERT trajectory_events` (:159-164) | FK takes KEY SHARE on the parent row |
| 12 | Projection SELECTs, upserts, summary, `projected_seq` (repository.py:72-144) | Under the row lock |
| 13 | `COMMIT` fires `after_commit`, which publishes | — |

Related locks:
- `mark_capture_paused_in_tx`: `SELECT ... FOR NO KEY UPDATE` (:229-230), then steps 7-12 and `db.get(TrajectorySessionSummary)` (:238).
- `delete_trajectory_in_tx`: `FOR UPDATE` (lifecycle.py:11).

Lock order contract (docs/SESSION_TRAJECTORY_PROTOCOL.md:50):
- Business row locks come first, in ascending session id order.
- The trajectory row lock comes last.
- The recorder takes no business locks.
- Seq is never allocated in process or with MAX+1.

### 4.8 Exceptions

| Exception | Where | Why |
|---|---|---|
| TrajectoryError (validation messages, §2.4) | types.py:84-119 | Malformed event |
| TypeError/ValueError (JSON) | types.py:89 | Unserializable data. Wrapped into RecordingError inside wrapped functions; raw from `record_stream`. |
| ValueError | context.py:28-29; producers.py:18,22; agent/trajectory.py:409 | Invalid or mismatched context |
| OwnershipError (6 messages) | recorder.py:34-35,45-62 | Missing, deleted, foreign or undelegated session; forged workspace |
| OwnershipError | recorder.py:79-80 | Tombstone |
| OwnershipError | recorder.py:186-187 | Stale lease |
| OwnershipError | payload.py:82-84 | Recreating deleted content |
| IdempotencyConflict | recorder.py:125-126,132-133 | Same id with different content, or an id owned by another trajectory |
| RecordingError "Trajectory persistence failed: <type>" | types.py:135-138 | DB, commit, blob or any other failure |
| Stored stream failure (re-raised) | recorder.py:312,315,344,349,431 | A failed batch fails every receipt and the next flush |
| Raw DB errors | `context_for_session`, and `mark_capture_paused_in_tx` when called directly | Not wrapped |
| TrajectoryError (business-raised) | session/session.py:516 | Chat fencing |
| RecordingError (adapter-raised) | agent/trajectory.py:114,495,530,629 | Media retention, request input, stream redaction failures |

### 4.9 How business code reacts (fail-closed)
- **Re-raise guards.** 148 `TrajectoryError` lines in 28 files, for example agent/loop.py:1457-1459, agent/llm.py:1412-1414,1866-1868, tool/batch.py:81-84, tool/mcp_tool.py:1034-1035, video/job_recovery.py:144-146. cron/executor.py:195-198 carries an explicit comment against best-effort fallback on a recording outage.
- **Written contract.** Recording failure must not be treated as a reason to continue (docs/SESSION_TRAJECTORY_PROTOCOL.md:54).
- **Rollback coupling.** A failed `record` inside `create_user_message` rolls back the Message and Part rows (tests/unit/test_trajectory_session_runtime.py:69-89).
- **Dispatch gate.** When recording fails, the provider is never called (tests/unit/test_trajectory_runtime.py:87-107).

---

## 5. Streaming

### 5.1 `record_stream(context, event)` (recorder.py:387-410)
1. **Disabled:** returns `create_task(record("recording.gap", {}, context))`. `record` sees the disabled flag, so only the pause marker is written and the receipt resolves to None (:394-395).
2. `key = (id(loop), user_id, root session_id)` (:396).
3. `item = prepare(...)` and `size = len(canonical(item))` run synchronously (:397-398).
4. **Oversize** (`size > min(PENDING, TOTAL)`): `create_task(record(type, data, event_id, occurred_at, IDs))`. This passes the flush barrier and uses its own transaction (:399-404).
5. **Otherwise:**
   - `_streams.setdefault(key, _StreamQueue(context))`. The queue keeps the *first* context it saw.
   - `task = create_task(recording_boundary(stream.enqueue)(item))`, tracked in `stream.receipts` until done.
   - Returns the task as the receipt, which resolves to the committed event dict (stored form: large data appears as `{"$payload": ref}`) (:405-410).

### 5.2 `_StreamQueue` (recorder.py:291-359) and `_Capacity` (:265-288)
- **`enqueue`** (:302-323):
  - Acquires global capacity for the event loop. `_Capacity.acquire` waits while `used and used+size > TOTAL` (:270-275).
  - Under the queue condition, waits while `pending_bytes and pending_bytes+size > PENDING`, raising any stored failure.
  - Appends `(item, size, future)` and starts the commit task if it is not running.
  - Awaits `shield(future)`. On error it releases capacity and re-raises.
  - The docstring says memory pressure blocks producers and never discards chunks (:388-391).
- **`commit`** (:325-359):
  - Sleeps `BATCH_MS` once.
  - Loop: take all items; `get_db_session()` plus `append_events_in_tx(db, self.context, items)`; set `watermark = through_seq`; resolve futures by event_id; decrement `pending_bytes`; release capacity; exit when empty.
  - On any `BaseException`, including cancellation: stores `failure`, fails the batch futures and every queued future, zeroes `pending_bytes`, releases capacity for all of them, and returns. The queue stays failed.

### 5.3 `flush(context=None)` (recorder.py:413-444)
- **No context and no current binding:** flushes every queue on this loop and returns "0" (:415-419).
- **Otherwise:**
  - Awaits the shielded pending receipts, including tasks that have not yet run a first turn (:424-427), then the commit task.
  - On failure: retires the queue to `_failed_streams` and re-raises (:430-438).
  - Removes an idle queue (:439-440).
  - Runs a new transaction `SELECT committed_seq` and returns it as a string (:441-444).
- This is the barrier that keeps a standalone structural `record()` behind already-scheduled chunks (tests/integration/test_trajectory_storage.py:211-219).

### 5.4 Failure lifecycle
- **`_remember_failed_stream`** (:366-370): `_failed_streams.setdefault(key, {context, event_id: evt_gap_<uuid>, error_type, watermark})`, then removes the queue.
- **Retirement:**
  - A fresh run or fresh unleased request retires the failed queue (§4.3).
  - For the same run, the next standalone `record()` sees `flush` raise the stored failure once (and retire the queue).
  - The next successful append then writes `recording.gap {reason:"stream_persistence_failed", phase:"recovered", error_type, last_committed_seq, last_stream_receipt_seq: watermark, uncommitted_output:"not_recorded"}` before its own events (:189-197).
- **`after_commit`** pops the recovered keys from `_failed_streams` (:248-249).
- **Test expectations** (test_trajectory_storage.py:272-291): types `[trajectory.started, request.started, recording.gap, request.started]`, `last_committed_seq == "2"`, and capacity used returns to 0.
- **Global backpressure test** (test_trajectory_storage.py:312-343).

### 5.5 How producers use receipts and watermarks
- **`RequestCapture.stream_chunks`** (agent/trajectory.py:539-603):
  - A reader task pulls the provider stream, redacts each chunk through `chunk_data` and `CaptureStreamRedactor` (:507-530), and waits on a producer byte budget of `TRAJECTORY_PENDING_BYTES` (:555-569) and a 32-slot queue (:553).
  - It calls `record_stream` with id `request:{rid}:chunk:{n}` and the observed time (:570-573).
  - **The consumer awaits each receipt before yielding the chunk** (:586-595). Reader errors arrive through a sentinel (:579-590). Closing cancels the reader and closes the provider stream (:596-603).
  - Tests: test_trajectory_runtime.py:242-271 (read-ahead fills a batch, delivery waits) and :274-297 (close records cancelled).
- **Non-stream path:** `chunk()` awaits `record("request.delta")` (:532-537). `finish()` records a redaction-finalize control delta and then `request.finished` (:617-646).
- **Tool output uses `record`, not `record_stream`:**
  - hooks.py:166-177 awaits `tool.output` before the `PART_UPDATED` publish.
  - tool/bash.py:183-196 records output beyond `MAX_STREAM_OUTPUT`.
  - tool/tool.py:180-186 records the full result before truncation.
- **Watermarks:** `flush()` returns the committed seq, `_StreamQueue.watermark` holds the last committed batch, and `committed_seq` travels in notifications.
- **Shutdown:** `main.py:_shutdown_trajectory` runs `stop_exports` → `flush()` → `stop_archive_worker` (main.py:79-89).

---

## 6. Commit hooks, notifications, pause

### 6.1 Hooks (recorder.py:243-262)
Both hooks are registered at import time on the global `sqlalchemy.orm.Session` class.
- **`after_commit`:**
  - Returns early inside a nested transaction (:245). No production code uses `begin_nested`; the only hit is :245.
  - Pops `trajectory_notifications` and publishes each entry.
  - Pops `trajectory_recovered_streams` and removes those keys from `_failed_streams`.
- **`after_soft_rollback`:** pops `trajectory_notifications`, `trajectory_resume_baselines` and `trajectory_recovered_streams`. It also fires on savepoint rollback; recovery then relies on watermark polling (:258-259).

| `session.info` key | Set | On commit | On rollback |
|---|---|---|---|
| `trajectory_notifications {trj: payload}` | recorder.py:167-169; lifecycle.py:26-28 | Popped and published | Popped |
| `trajectory_resume_baselines {trj: baseline_id}` | recorder.py:86 (consumed :96-102) | **Not popped** | Popped |
| `trajectory_recovered_streams {key}` | recorder.py:197 | Popped and cleared | Popped |

### 6.2 `trajectory.available`
- **Payload:** `{user_id, owner_user_id (same value), session_id (root), trajectory_id, committed_seq: str}`, plus `deleted: True` from deletion (lifecycle.py:26-28). One per trajectory per commit.
- **Transport:** `bus.publish` dispatches locally and synchronously, then schedules a fire-and-forget Redis `bus:events` broadcast (bus/bus.py:65-97).
- **Consumers:**
  - api/admin_trajectory_ws.py:159 subscribes, coalesces per session, and re-reads the header from the DB before sending `_watermark(header)` (:141-152).
  - The ordinary chat WS drops every `trajectory.*` event (api/ws.py:247).
  - Frontend: `TrajectoryWatermark` (frontend-v2/src/shared/ws/events.ts:86-97) and useTrajectorySocket.ts:34.
- **Contract:** the notification carries only a watermark. Lost notices are recovered by HTTP catch-up (docs/SESSION_TRAJECTORY_PROTOCOL.md:111).

### 6.3 `recording.gap` variants and projection

| Variant | Emitter | Data | Id |
|---|---|---|---|
| paused | recorder.py:233-236 | `{phase:"paused", reason:"recording_disabled", last_recorded_seq}` | random |
| resumed | recorder.py:81-86 | `{phase:"resumed", reason:"recording_reenabled", previous_committed_seq}` | random |
| recovered | recorder.py:192-196 | §5.4; written with the failed stream's context | `evt_gap_<uuid>`, stable per failure |
| process_restarted | cron/recovery.py:81-82, after `job.finished {status:"unknown"}` | `{job_id, reason:"process_restarted", result_availability:"unknown"}` | random |

How gaps are projected:
- A `gap:<event_id>` record is created (projector.py:66-67).
- Non-terminal tool, request, assistant and step records with the **same `run_id`** become `unknown`. Paused and resumed gaps have no run_id, so they match run-less records (projector.py:277-281; repository.py:86-92).
- `recording_status` becomes `gap` (repository.py:138-139).
- The export manifest lists the gaps with `complete=false` (export.py:100-101; test_trajectory_storage.py:266-270).

`recording_status` values:
- `recording` on insert (recorder.py:73).
- `gap`, set by projection or resume.
- `paused` (recorder.py:236).
- `deleted` (lifecycle.py:16).
- Nothing ever returns a trajectory to `recording`. The admin view shows `paused` when the owner is not enabled (repository.py:274,346-352).

### 6.4 `mark_capture_paused_in_tx` (recorder.py:227-240)
1. `SELECT` the trajectory by (session_id, user_id, not deleted) `FOR NO KEY UPDATE`.
2. If there is no trajectory or it is already paused, return. The marker never creates a container and is written once per pause period.
3. Append the paused gap with a context that has no IDs.
4. Set status `paused` on the trajectory and on the summary (`db.get(TrajectorySessionSummary)`).

Callers:
- The disabled `record` path, which costs one DB round trip per call even while disabled.
- session/session.py:591-592, when `record_projection_in_tx` has no context.
- session/session.py:737-738, when user input arrives with no trace.
- session/revert.py:26 and session/fork.py:196-197.

A child `session_id` matches no row, so a child call writes no marker. Resume happens inside `ensure` on the first enabled contact (§4.5 step 5). The contract is described at docs/SESSION_TRAJECTORY_PROTOCOL.md:48.

### 6.5 Deletion (lifecycle.py:10-28)
- Takes `FOR UPDATE`. A foreign owner raises OwnershipError.
- Sets `deleted_at` and `status=deleted`, leaving a tombstone.
- Deletes events, records, checkpoints and the summary.
- Marks payloads `availability=deleted` with `content=None`, and exports `status=deleted`.
- Queues the `deleted` notification.
- Called in the session-delete transaction (session/session.py:340-341). Afterwards `ensure` raises (recorder.py:79-80; test_trajectory_storage.py:220-226).

---

## 7. Redaction

### 7.1 Layers

| Layer | Where | What | When |
|---|---|---|---|
| Request allowlist | agent/trajectory.py:20-73 (`REQUEST_FIELDS`, `PRIVATE_FIELDS`, `public_value`, `request_snapshot`) | Keeps only public kwargs; drops `_`-prefixed and private keys; SDK objects become `not_recorded`; schema subtrees go through `sanitize(_schema=True)` | Producer, before `record` |
| Provider output / service bodies | agent/trajectory.py:76-90,119-126,266-281 | Field allowlists; owned media URLs become `trajectory-media:<id>` | Producer |
| Stream chunks | `CaptureStreamRedactor` (stream_redaction.py:416-473) via agent/trajectory.py:450-451,522-530 | Stateful per-block redaction; raw provider text becomes `$stream_blocks` references or path-scoped inline sanitized deltas; complete fields get `_complete_value`; a finalize control chunk is emitted | Producer, per chunk, before enqueue |
| Tool output | `StreamTextRedactor` via hooks.py:76-77,109-110,169; tool/tool.py:174-183; tool/bash.py:186-192 | Replace or delta modes; `final=True` at finish | Producer |
| File versions | files.py:20-21 | `sanitize(before/after)` before sha256 and diff | Producer |
| Generic | recorder.py:122 | `sanitize(data)` on every event, before hash, externalization, DB, projection and notification (redaction.py:1) | Recorder, inside the transaction, under the row lock |

### 7.2 `sanitize` rules (redaction.py:73-107)
- **Keys.**
  - Normalized as `re.sub("[^a-z0-9]", "", key.lower())` (:23-24).
  - A key in `_PRIVATE` (:6-16), or starting with `__private`, keeps the key with value `"[REDACTED]"` (:85-87).
  - `_PRIVATE` holds credentials plus provider-private replay state: encryptedcontent, reasoningsignature, responsechainid, canonicaltoolid, wiretoolname, providerbinding*, providerdialect, streamseq, providermetadata, providerreplay, provideraccountid, ticket.
- **Schema mode.**
  - Entered at keys schema, parameters, inputschema, outputschema or jsonschema when the child is a schema node (:67-70,89).
  - A private-named property whose value is a schema node stays visible (:79-81), but its default, example(s), const and enum are redacted (:82-84).
- **Strings.**
  - JSON-looking text (`{`, `[` or `"`) is parsed, sanitized and re-dumped with `ensure_ascii=False`, which changes formatting (:95-102).
  - URLs: query keys that are private, prefixed `x-amz`/`x-oss`/`x-goog`, or named sig/se/sp/sv are redacted; userinfo is stripped; an unparsable URL becomes `[REDACTED URL]` (:27-35,103).
  - `Bearer`/`Basic` tokens (:17,104).
  - `sk-` followed by at least 12 characters, and 3-part `eyJ` JWTs (:18,105).
  - Assignments `api_key|access_token|refresh_token|password|passwd|secret|authorization|cookie|signature|token` followed by `=` or `:`: the value is redacted up to the matching quote (escape-depth aware) or up to one of `,;}]\n\r& ` (:19,38-64).

### 7.3 Stream redaction (stream_redaction.py)
- The design rule is that an incomplete credential prefix must never be published and then removed later. There is one scanner per stream and no global cache (:1-5).
- **`_Scanner`** (:46-269):
  - Withholds ambiguous prefixes `sk-`, `eyj`, `http://`, `https://` (:19,106-123).
  - Redacts `sk-` and `eyJ` tokens of any length (:108-110,197-201).
  - Handles sensitive words followed by quoted, structured, unquoted or header-until-EOL values (:124-196), plus bearer/basic (:133-136).
  - Normalizes quoted keys, including spaced and escaped names (:83-103).
  - Decodes JSON escapes lexically and carries at most a 16-character escape suffix across chunks (:218-254).
  - Buffers URLs to the next delimiter, then applies `_safe_url` (query redaction, sanitized values, stripped userinfo, `sk-`/`eyj` fragments) (:27-43,202-215). A URL over 16 KiB is redacted and the rest discarded (:212-214).
- **`redact`** (:283-294):
  - Replace mode resets the scanner.
  - A delta after `final` raises ValueError.
  - Returns `redaction: {version:1, sanitized, pending_prefix}`.

### 7.4 Producer or worker?
- **Must run in the producer before any JSONL write.**
  - Stream redactors hold per-stream state, and the unredacted fragments must never reach the shared volume.
  - The allowlists exclude headers and keys at the source.
  - Generic `sanitize` covers raw secrets that can appear in tool args, OSS-signed URLs, Authorization headers inside `requested_arguments`, and error strings (`str(error)` at agent/trajectory.py:639, hooks.py:95-102).
  - files.py handles full file text.
  - `sanitize` can run in the writer thread, since the in-memory queue is not disk, but it must run before the write. That still costs GIL-bound CPU, which today runs inside the DB lock (recorder.py:121-122).
- **Hash coupling.** `content_hash` is computed over sanitized data. A worker that recomputes it needs the identical redaction version; with keep-first dedupe the hash only feeds conflict telemetry.
- **Worker-side is fine** only as defense in depth. Re-sanitizing is idempotent in practice: markers survive, and a JSON re-dump is stable after the first pass.

---

## 8. `jobs.py` and `files.py`

### 8.1 `record_job_in_tx(db, job, *, submitted=False, session_id=None)` (jobs.py:14-66)
1. Returns None when disabled (:16-17).
2. `field` is `request_data` if the attribute exists, else `details`; `saved = metadata["_trajectory_context"]` (:18-20).
3. `source_session = saved.source_session_id or session_id or job.session_id`. With none, returns None (:21-23).
4. Loads `Session[source]` and the root (`Session[saved.session_id]`, or the source itself). If either is missing or deleted, returns None silently, so late callbacks cannot revive a deleted trace (:24-29).
5. `context = activity_context(db, job.user_id, source, saved=saved)` (:30-32).
6. With no saved context, the job adopts the current one: it writes `context.to_dict()` into the job metadata (a business write). If the job is not new, it also records a `baseline.captured` adoption event with id `job_adopt:{id}` (:33-40).
7. **Type:** `job.submitted` when submitted; `job.finished` when status is in `_TERMINAL` (completed, failed, cancelled, published, draft, expired, unknown, timed_out; :11); otherwise `job.progress` (:41).
8. **Data** (:42-55):
   - `job_id`, `job_type` (kind or platform), `status` (published and draft map to completed), `result_state`, `model`, `error`, `output_asset_id`, `provider_task_id`, `provider_request_ids`.
   - Submitted: `input` (metadata minus `_trajectory_*`) and `prompt` (or title).
   - Otherwise: `result` (`result_data`, or `{item_id, video_id, details}`).
9. **Id** `job:{id}:{sha256(canonical(data))[:24]}` (:56-57). Identical observations dedupe; any change in the data, including a new request id, creates a new event.
10. **Late result** (:58-65): when terminal and `context.run_id` is set, but `SessionExecution.run_id` differs or the row is missing, it also records `operation.late_result` with id `job_late:{id}:{digest}`.

All records pass `db=db`, so the job's state and its trajectory fact commit atomically. Test: tests/integration/test_trajectory_boundaries.py:152.

### 8.2 `captures_files(ctx)` and `record_file_change` (files.py:11-42)
- `captures_files` = `bool((ctx.trace_context or current()) and enabled(ctx.user_id))`.
- `record_file_change` is called after a successful write.
  1. Sanitizes `before` and `after` (:20-21).
  2. Each version is `{availability:"available", text, sha256 of the retained text, size_bytes, source:"executor_content", hash_scope:"retained_content", redacted: text != original}`, or `{availability:"absent"}` for the after-state of a delete, or `not_recorded` (:23-29).
  3. Computes a unified diff of the redacted texts (:30-34).
  4. Records `artifact.recorded {artifact_id:"file:{source_session_id}:{path}", artifact_type:"file_diff", name, path, operation, media_type:"text/plain", source_kind:"file_tool", before, after, diff, availability, capture_level:"executor_content"}` without `db` and without an event_id. It gets its own transaction plus the flush barrier and is not idempotent (:35-42).
- **Quirks:**
  - A JSON file is re-serialized by `sanitize`, so `text` differs from the real bytes and `redacted` is true even with no secret in it.
  - One event can carry up to about 3x the file size (before, after and diff). Payloads above `INLINE_BYTES` are externalized.

---

## Migration notes

### A. Where each component moves

| Current | Target |
|---|---|
| `prepare()` validation, `event_id` and `occurred_at` stamping (types.py:81-120) | Producer `emit()`. Invalid events are dropped and counted, never raised. |
| All redaction layers (§7.1) | Producer, before the spool write |
| `enabled()` gating and `mark_capture_paused_in_tx` (4 business sites plus the `record` path) | Producer emits a content-free `recording.gap{phase:"paused"}` marker, deduped per (root session, process). No DB access. |
| `record(db=...)`: 39 in-transaction calls | Stash in `session.info` and enqueue from `after_commit`; discard on `after_soft_rollback`. Reuse the `trajectory_notifications` pattern at recorder.py:167-169,243-262, but pop on commit, unlike `trajectory_resume_baselines`. |
| `record()` standalone (38 calls) and `record_stream` (1) | Direct `emit()` |
| `ensure_trajectory_in_tx`, `_validate_owner`, `_append_prepared`, `store_json`, `project_events_in_tx`, notifications | Worker |
| Lease checks (recorder.py:182-187; session/session.py:505-516) | question/runtime.py and session code, independent of recording |
| `delete_trajectory_in_tx` (session/session.py:340-341) and `payload.delete_for_asset` | Control events emitted after the business commit; worker applies tombstones |
| Archive worker, checkpoints, purge, `resume_exports` (main.py:106-117) | Worker process |
| `_StreamQueue`, `_Capacity`, receipts, `flush`, `_failed_streams` | Removed. Replaced by a bounded queue plus writer thread; overflow records a gap. |

### B. Behaviours the worker must replicate
1. **Seq.** Per-root contiguous seq starting at 1, allocated by a single writer. Readers raise `CorruptContent` on holes (repository.py:225-235). Watermarks (`next`, `committed`, `projected`) are decimal strings in the API.
2. **Container.** Keyed by the root `session_id`, unique on (session_id) and (user_id, session_id) (db/models/trajectory.py:16,26). `workspace_id` comes from the root session row. Child events join the parent's trajectory through `source_session_id`, and broken ancestry or a deleted child rejects the event (recorder.py:49-62).
3. **Start event.** `trajectory.started` is seq 1 with id `evt_start_{trj}` and `coverage_start` equal to `started_at` (recorder.py:87-94).
4. **Idempotency.** Global `event_id`, first write wins, duplicates are silent. A conflicting hash is telemetry, not an exception. Keep the §2.8 ids stable. `asset:` ids embed `trajectory_id` (artifacts.py:102), so the producer cannot compute them and they must be rekeyed, for example on root session id.
5. **Baselines.** One `evt_baseline_{trj}` initially, and one `evt_baseline_{trj}_{seq}` after each resume. Producers today decide whether to build one by reading trajectory tables (session/session.py:553-557,606-608). The worker must accept baseline candidates and keep only the first per recording epoch.
6. **Gaps.** Paused, resumed and recovered phases, cron `process_restarted`, plus new overflow and writer-failure gaps. Projection semantics are in §6.3; overflow gaps should carry the `run_id` or request ids of dropped events so in-flight records become `unknown`.
7. **Pause.** At most one marker per pause period; a pause never creates a container. On the first enabled event, write the resumed gap before a new baseline. Status machine: recording → gap/paused, never back to recording; deleted is terminal.
8. **Tombstones.** Drop events for deleted trajectories (today recorder.py:79-80 and jobs.py:27-29). Never recreate deleted payload content (payload.py:82-84).
9. **Payload visibility.** A payload is visible only at `through_seq >= first_seq` (payload.py:102-104). Producers currently pass `trajectory.next_seq` (artifacts.py:97; agent/trajectory.py:217-219,487-490); the worker must assign it on apply.
10. **Notification.** Same payload, including the `deleted` variant, coalesced per trajectory per trace-DB commit. The WS contract stays the same.

### C. Must stay in the producer
- TraceContext, contextvars, `derive`, and every persisted context (§3.4). Each spool line must carry `context.to_dict()`. The worker needs a tolerant parser, because `from_dict` rejects unknown keys (context.py:39-41).
- Redaction (§7), `event_id` and `occurred_at` stamping at observation time, `enabled()` gating, and `after_commit` binding.
- Job context adoption into business job rows (jobs.py:33-34).
- Fencing, which must work with recording disabled:
  - Existing non-recorder checks: `still_current`, `owns` and `heartbeat` (question/runtime.py:130-213), and callers at hooks.py:131-133,195; agent/loop.py:414,1156,1448; agent/suggestions.py:77.
  - Gaps today:
    - LLM request dispatch (the `request.started` gate, including the title/suggestions exception at recorder.py:185).
    - Stream chunk delivery and tool-output updates.
    - Chat part and message projection writes (session/session.py:505-516), which currently raise `TrajectoryError` and need a business exception type.
    - `get_run_trace` returns None when disabled (question/runtime.py:45-46), so contexts lose `run_id` and fencing silently turns off.

### D. Can be dropped
- recorder.py:265-444: stream queue, capacity, receipts, `flush`, failed-stream and recovered-gap machinery. Also the oversize direct path (:399-404) and producer read-ahead with receipt awaits (agent/trajectory.py:553-595).
- `recording_boundary` and `RecordingError`, and the 148 `TrajectoryError` re-raise guards once `emit` never raises.
- `PendingRange` and `record` return values (only assigned at session/session.py:587, never consumed).
- DB staging of payload bytes and the in-process archive worker (payload.py:88-94,238-271; main.py:107-108).
- The DB-backed `mark_capture_paused_in_tx`, `append_activity` (dead) and `saved_context`.
- The in-transaction projection (recorder.py:165-166) and `search_text` (repository.py:126).
- The producer-side `TRAJECTORY_PENDING_BYTES` budget.

Knob mapping:
- `TOTAL_PENDING_BYTES` → the emit queue bound.
- `BATCH_MS` → writer and worker batching.
- `INLINE_BYTES` → the worker blob threshold.
- `CHECKPOINT_INTERVAL` → worker segment and checkpoint cadence.

### E. What breaks
- **Business reads of trajectory tables:**
  - permission/permission.py:215,226-250: durable approval fallback. It polls `TrajectoryEvent permission.resolved:{id}` about once a second and expands its payload. The reply is recorded before the future is set (:398-402). Test: tests/integration/test_trajectory_boundaries.py:225. This needs a business-side persisted reply.
  - session/session.py:538-557,602-608: baseline existence and paused checks.
  - session/fork.py:178-184: reads the source trajectory id and `committed_seq` into `history.forked`.
  - artifacts.py:96-102 and agent/trajectory.py:217-219,487-490: need the trajectory id and `next_seq` synchronously.
- **Cross-database join:** repository.py:324-329 joins business sessions, users and workspaces with trajectory tables. The readiness schema also lists trajectory tables in the business DB (db/base.py:335-341).
- **Tests encoding the current semantics:**
  - Must be inverted or rewritten: test_trajectory_runtime.py:48-84, :87-107, :242-271; test_trajectory_session_runtime.py:69-89 (inverted); test_trajectory_storage.py:61-80, :82-90, :92-106, :108-135, :211-227, :244-270, :272-291, :312-343; test_trajectory_process_crash.py with benchmark_process_crash.py.
  - Must also pass with recording disabled: test_trajectory_session_runtime.py:116-136.

### F. Risks
1. **Ordering.** Seq becomes arrival order. After-commit facts, such as `part.committed` or `run.started`, can land after deltas or tool outputs that were emitted immediately, and multiple processes share the spool. The current flush barrier covers only standalone calls, never in-transaction ones. The projector's assumptions (finished after deltas, gaps matched by `run_id`) must tolerate reordering. `occurred_at` has millisecond precision (types.py:61).
2. **Loss.** The in-memory queue and the writer buffer die on a hard kill, whereas today committed facts are durable. A per-process epoch marker is needed so the worker can write `process_restarted` gaps.
3. **Changed overflow policy.** The documented rule "block producers, never discard" (recorder.py:388-391; docs/SESSION_TRAJECTORY_IMPLEMENTATION_STATUS.md:163) becomes drop plus gap.
4. **Replay duplicates.** Most events use random ids, which relied on transactional atomicity. The worker must commit spool offsets atomically with trace-DB writes.
5. **Fencing regression** if the recorder checks are removed before runtime equivalents exist.
6. **Ownership.** Without business-DB reads, the worker cannot enforce ancestry, the deleted flag or workspace matching (recorder.py:43-63).
7. **CPU and config.** Redaction CPU competes with the event loop and the GIL. `enabled()` and `admin_enabled()` read env per call, so backend and worker must share the `TRAJECTORY_*` env.
8. **Spool size.** `request.prepared` snapshots can hold inline base64 media, and file events hold about 3x the file size. Owned media must already be references when emitted.

### G. Open questions
1. Does ownership and ancestry validation run in the producer (emitting validated root, workspace and parent claims) or in a worker with read-only business-DB access?
2. How does the producer decide to build a baseline without trajectory tables: always emit one on first contact, or keep a small business-DB marker such as a recording epoch?
3. What replaces the permission reply fallback?
4. Under keep-first, are idempotency conflicts only logged, or recorded as gaps?
5. What is the spool layout per process, and what ordering guarantee holds per trajectory?
6. How are delete control events ordered against late events (epoch or timestamp)?
7. What drain timeout replaces shutdown `flush()`?
8. What are the forward-compatibility rules for `generation` types and unknown context keys in spool lines?