# Trajectory map: projection, reads, payloads, media, export and deletion

The trajectory tables live in the business database, and projection runs inside the business write transaction. The admin UI rebuilds records itself from `/checkpoint` plus `/events`, so the new design has to keep those two responses returning full data (or change the protocol version).

## 0. What is in scope

| Item | Lines / size | Notes |
|---|---|---|
| `backend/trajectory/repository.py` | 467 | Reads, projection, checkpoints |
| `backend/trajectory/projector.py` | 345 | Pure v1 reducer. Ported line for line to `frontend-v2/src/features/admin-trajectories/utils/projector.ts` (header comment at :1-8) |
| `backend/trajectory/payload.py` | 271 | Stored content and the archive worker |
| `backend/trajectory/artifacts.py` | 237 | Media and attachment capture, revocation |
| `backend/trajectory/export.py` | 145 | ZIP export |
| `backend/trajectory/lifecycle.py` | 59 | Deletion and cleanup of deleted content |
| `backend/db/models/trajectory.py` | 134 | 7 ORM models |
| `backend/trajectory/fixtures/session_v1.json` | 54,956 B | `old_session_tools_questions_retry_child_agent`: 34 events, 18 records, historical cut at seq 17 |
| `backend/trajectory/fixtures/edge_cases_v1.json` | 41,473 B | `empty_message_invalid_tool_emoji_system_identity`: 16 events, 7 records, cut at seq 13 |
| Fixture keys | — | `name, events, expected_state, expected_statistics, expected_agents, historical{through_seq,state}`. Tests replay them fully, as a prefix, in chunks and resumed from a checkpoint (`tests/unit/test_trajectory_projection.py:11-40`) |
| Migration `f6a8c0e2b4d6_session_trajectories.py` | :17-137 | Creates all 7 tables. Also adds the business columns `session_executions.trace_context` (JSON, server_default `'{}'`) and `cron_runs.trace_context` (:136-137) |
| Migration `c7e9b1d3f5a7_merge_message_center_trajectories.py` | :8 | Empty merge of `f6a8c0e2b4d6` and `a1c2e3b4d5f6`. No other migration touches these tables (grep) |

---

## 1. Schema

All `DateTime` columns are timezone-aware (`db/base.py` type map). `JSON` means `JSONType`: JSONB on Postgres, TEXT on SQLite (`db/base.py:27-47`). "Default" means a Python-side ORM default; the migration has no `server_default` for any of these columns.

### `session_trajectories` (models:12-26, migration:17-32)
| Column | Type | Null | Default / values |
|---|---|---|---|
| id | String(64) PK | no | `trj_`+uuid hex (`recorder.py:69`) |
| user_id | String(64) | no | |
| session_id | String(64) UNIQUE | no | |
| workspace_id | String(64) | no | |
| started_at, updated_at | DateTime | no | |
| next_seq | BigInteger | no | 1 |
| committed_seq | BigInteger | no | 0 |
| projected_seq | BigInteger | no | 0 |
| schema_version | Integer | no | 1 |
| recording_status | String(32) | no | `recording`; also `paused` (`recorder.py:236`), `gap` (`recorder.py:85`, `repository.py:139`), `deleted` (`lifecycle.py:16`) |
| deleted_at | DateTime | yes | tombstone marker |

Constraints: UNIQUE(session_id), unnamed; UNIQUE(user_id, session_id) named `uq_trajectory_owner_session`. There are no foreign keys to sessions, users or workspaces.

### `trajectory_events` (models:29-52, migration:33-55)
- **Columns.** `event_id` String(128) PK (unique across all trajectories). `trajectory_id` String(64) FK → `session_trajectories.id` ON DELETE CASCADE. `seq` BigInteger; `type` String(64); `version` Integer; `user_id`, `session_id`, `source_session_id` String(64); `request_id`, `call_id`, `agent_id` String(128), nullable. `context` JSON (the non-null ID fields); `data` JSON (inline sanitized data, or `{"$payload": ref}`). `content_hash` String(64); `occurred_at`, `recorded_at`. Everything is NOT NULL except the three ID columns.
- **Constraints and indexes.** UNIQUE(trajectory_id, seq) `uq_trajectory_event_seq`. Indexes `ix_trajectory_event_request`, `_call` and `_agent` are each (trajectory_id, X, seq).
- **Those three indexes are unused.** Every event access goes through the primary key: `artifacts.py:46-47` (event_id IN plus a type filter), `permission.py:235`, `session.py:554,607`, `recorder.py:98,131`.

### `trajectory_payloads` (models:55-72, migration:56-76)
| Column | Type | Null | Default / values |
|---|---|---|---|
| payload_id | String(64) PK | no | `pld_`+uuid hex (`payload.py:86`) |
| trajectory_id | String(64) FK CASCADE, indexed `ix_trajectory_payloads_trajectory_id` | no | |
| sha256 | String(64) | no | hash of the raw bytes |
| storage_key | Text | no | set to `""` once the blob is deleted (`lifecycle.py:53`) |
| storage_status | String(16) | no | `pending` (bytes in DB) or `stored` |
| content | LargeBinary | yes | bytes held in the DB until archived |
| size_bytes | BigInteger | no | |
| media_type | String(128) | no | |
| encoding | String(16) | no | `utf-8` for JSON, else `binary`. Written, never read (grep) |
| availability | String(24) | no | `available` or `deleted` |
| first_seq | BigInteger | no | visibility lower bound (§5) |
| source_asset_id | String(64), indexed | yes | |
| created_at | DateTime | no | |
| deleted_at | DateTime | yes | |

Indexes: `ix_trajectory_payload_digest` (trajectory_id, sha256) and `ix_trajectory_payload_pending` (storage_status, availability, created_at). There is **no unique constraint on the dedupe key**.

### `trajectory_records` (models:75-92, migration:77-95)
- **Primary key:** (trajectory_id FK CASCADE, record_id String(256)).
- **Columns:** `kind` String(32); `status` String(32); `agent_id` String(128) null; `start_seq` BigInteger; `message_id` String(128) null; `end_seq` BigInteger null; `applied_seq` BigInteger; `projector_version` Integer; `data` JSON; `summary` JSON (default `dict`); `search_text` Text NOT NULL.
- **`search_text` is write-only:** it is set at `repository.py:126` and never read.
- **Indexes:**
  - `ix_trajectory_record_order` (trajectory_id, start_seq, record_id)
  - `ix_trajectory_record_message` (trajectory_id, message_id)
  - `ix_trajectory_record_filter` (trajectory_id, kind, status, start_seq)

### `trajectory_session_summaries` (models:95-110, migration:96-112)
- **Columns:** `trajectory_id` PK FK CASCADE; `user_id`, `session_id`, `workspace_id` String(64); `last_activity_at`; `running_status` String(32); `recording_status` String(32); `model` String(128) null; `applied_seq` BigInteger; `statistics` JSON.
- **`statistics` keys:** request_count, tool_count, error_count, unknown_count, input_tokens, output_tokens, usage_missing.
- **Indexes:**
  - `_activity` (last_activity_at, session_id)
  - `_owner` (user_id, last_activity_at, session_id)
  - `_workspace` (workspace_id, last_activity_at, session_id)
  - `_status` (running_status, recording_status, last_activity_at)

### `trajectory_checkpoints` (models:113-120, migration:113-120)
PK (trajectory_id FK CASCADE, through_seq BigInteger); `projector_version` Integer; `state` JSON; `digest` String(64); `created_at`.

### `trajectory_exports` (models:123-134, migration:122-135)
`id` String(64) PK (`exp_`+hex); `trajectory_id` FK CASCADE, indexed; `viewer_id` String(64); `through_seq` BigInteger; `status` String(24) (pending, running, completed, failed, deleted); `storage_key` Text null; `sha256` String(64) null; `error` Text null; `created_at`, `updated_at`.

### Mismatches and schema issues
| # | Finding |
|---|---|
| M1 | The ORM-only defaults (next_seq, committed_seq, projected_seq, schema_version, recording_status, storage_status, encoding, availability, summary) have no server default. Current inserts always pass explicit values (`recorder.py:70-73`, `payload.py:88-92`, `repository.py:102-105`). |
| M2 | Everything else matches between ORM and migration: types, nullability, FKs, and index names (the `index=True` names equal the migration's names). |
| M3 | UNIQUE(user_id, session_id) is redundant next to UNIQUE(session_id). The recorder handles both conflict targets (`recorder.py:74-77`). |
| M4 | Desktop mode builds the schema with `Base.metadata.create_all` in the single SQLite app DB (`db/base.py:109-123`). The only trajectory upgrade helper patches `trace_context` (`db/base.py:126-135`), so future trajectory column changes have no desktop path. |
| M5 | The business readiness probe requires all 7 trajectory tables (`db/base.py:335-341`; `tests/unit/test_database_readiness.py:188-200`). The models are registered in the shared metadata (`db/models/__init__.py:52-53`). |
| M6 | Queries without a supporting index: <br>• the cleanup query `availability='deleted' AND storage_key!=''` (`lifecycle.py:38-39`) <br>• exports by status (`export.py:134`, `lifecycle.py:40-41`) <br>• records filtered by agent_id (`repository.py:385-387`) <br>• `list_sessions` orders by `coalesce(summary.last_activity_at, sessions.updated_at)` (`repository.py:323,369`), so the summary indexes cannot serve it. |
| M7 | Length risks that Postgres enforces **[P]**: <br>• `record_id` (256) is built from unbounded data fields such as `artifact:{data.artifact_id}` (`projector.py:62-63`), and `files.py:36` builds `file:{source_session_id}:{path}` <br>• `media_type` (128) comes from a data-URL regex (`artifacts.py:205`) <br>Either overflow raises a `RecordingError` inside the business transaction. |

---

## 2. Record model (`projector.py`)

### Constants
- `PROJECTOR_VERSION=1` (`types.py:11`).
- `TERMINAL` = completed, failed, cancelled, denied, timed_out, unknown, interrupted, expired (:7). `ERROR` = failed, denied, timed_out (:8).
- These statuses are **not** terminal: `pending`, `running`, `streaming`, `waiting`, `accepted`, `injected`, `deleted`.
- `empty_state()` (:11-13) = `{projector_version, through_seq:"0", records:{}, unsupported_events:[], coverage_start:None}`.
- `ID_FIELDS` (`types.py:31`) = source_session_id, turn_id, run_id, generation, agent_id, parent_agent_id, step_id, request_id, call_id, parent_call_id, message_id, part_id, caused_by_event_id.

### `targets(event, state=None)`: which records an event touches (:20-73)
`_identity(e, f)` reads the top-level field first, then `data[f]` (:16-17).

| Event | record_id (kind) | Lines |
|---|---|---|
| `trajectory.*`, `baseline.*` | `baseline:{trajectory_id or session_id}` | :25-26 |
| `input.*` | `user:{message_id or event_id}` | :27-28 |
| `message/part` with role user (`data.role` or `data.message.role`) | `user:{message_id or event_id}` | :29-43 |
| `part.committed` where part.type is not None/text/reasoning | none | :32-33 |
| assistant `message.committed` with operation=created and no finish | none | :35-36 |
| assistant message/part, no request_id, state given, assistant rows exist with the same message_id | the one with the highest start_seq | :37-40 |
| assistant part, no request_id, no text/content | none | :41-42 |
| assistant message/part otherwise | `assistant:{request_id or message_id or event_id}` | :43 |
| `request.retry_scheduled` / `route_changed` | `retry:{event_id}` | :45-46 |
| other `request.*` | `request:{request_id}`; `request.delta` also `assistant:{request_id}` | :47-51 |
| `tool.*` | `tool:{call_id}` | :52-53 |
| `turn/run/step/agent.*` | `{family}:{family_id}`; `run.interrupted` and `cancel_requested` also `interrupt:{event_id}`; `run.started` with `resume_of_run_id` also `resume:{run_id}` | :54-61 |
| `question/permission/job/artifact/compaction/takeover` | `{family}:{family_id or data.id or call_id or event_id}` | :62-63 |
| `session.*` | `settings:{event_id}` | :64-65 |
| `recording.gap` | `gap:{event_id}` | :66-67 |
| `operation.late_result` | `late_result:{event_id}` | :68-69 |
| `context.*` | `context:{event_id}` | :70-71 |
| `history/plan/todo/skill/tool_catalog` | `{family}:{event_id}` | :72-73 |

`reduce` adds three implicit targets:
- `request.prepared` → `system:{request_id}` (only when the system prompt or tools changed).
- `request.finished` → `assistant:{request_id}`, if that record exists (:271-276).
- `run.interrupted` / `recording.gap` → every non-terminal tool/request/assistant/step record with an equal `run_id`. `None == None` counts as equal (:277-282).

### `_new_record` (:83-89)
Sets:
- `record_id, kind`, `title=kind`
- `preview`, `result_preview`, `status_reason` = None; `status="pending"`
- all ID_FIELDS copied from the event
- `start_seq=as_of_seq=str(seq)`, `end_seq=None`
- `started_at=occurred_at` (None for tools), `finished_at`, `duration_ms`, `timing_source` = None
- `data={}`, `blocks=[]`, `usage={}`

All seq values are decimal strings.

### `_update` (:138-225), in order
1. `as_of_seq = seq`; any non-null event ID field overwrites the record's (:141-144).
2. `data.update(deepcopy(event.data))`, a shallow merge, except for `request.delta`, `tool.output` and `request.usage` (:147-148). `part.*` sets `data.committed_parts[part_id]` (:149-152). `trajectory.started` sets `data.coverage_start` and `tool.requested` sets `data.requested_at` (:153-156).
3. Status, first matching branch:

| Condition | Effect |
|---|---|
| `request.delta` | `_blocks`; status `streaming` (:157-159) |
| `request.usage` | `_usage` (:160-161) |
| `tool.output` | Drop if chunk_index ≤ `data.output_chunk_index`; mode replace sets, string appends, anything else replaces; set `result_preview` (:162-175) |
| action `started` / `spawned` | `running`, `started_at` (:176-178) |
| `input.accepted` / `input.injected` | `accepted` / `injected` (:179-182) |
| action `requested/asked/submitted/prepared` | `waiting` for permission/question, else `pending` (:183-184) |
| action `finished/resolved/cancelled/expired/interrupted/removed` | Status from `data.status or data.outcome`, else by action (removed→`deleted`, default `completed`). Aliases: success/succeeded→completed, error→failed, timeout→timed_out, rejected→denied. Sets `finished_at`, `end_seq`, merges usage if present (:185-192) |
| `message/part` | user record in pending → `accepted`; otherwise if `message.finish` and not terminal → `failed` if `message.error` else `completed`, plus `end_seq` (:193-200) |
| family outside {request, tool, run, turn, step, agent, question, permission, job, compaction, takeover} | `data.status or completed`, `end_seq` (:201-203) |

4. Kinds interrupt/resume/retry/gap/late_result: `data.status or completed`, `end_seq` (:204-206).
5. `status_reason` = preview of `reason or error`, 500 chars (:207-208).
6. Duration (:209-217):
   - If `data.duration_ms` is present, use it; `timing_source` = `data.timing_source`, else `producer_monotonic` (None when the duration is null).
   - Otherwise, for timed kinds with start and finish times and a status other than denied/unknown, compute it with `timing_source=session_timestamps`.
   - A tool without `started_at` always gets None.
7. `title = data.title or name or tool or tool_name or model or title` (:218).
8. `preview` = the first of text/content/input/prompt/questions/requested_arguments/arguments/summary, 240 chars. It is set when preview is empty or the family is input/message/part. `result_preview` = the first of output/result/answers/model_output (:219-225).

### `_blocks` (:92-121)
- The block list is `data.blocks`, or a legacy single block `{block_id or "text:0", block_type or text, delta}`.
- A block's id is `block_id`, else `"{type}:{index}"`.
- A chunk is dropped if its `chunk_index` is ≤ the stored one.
- The delta is taken from `delta`, `text` or `arguments`. Dicts use `text`/`arguments` or are JSON-encoded; non-strings are JSON-encoded.
- `mode=replace` sets the text; otherwise the delta is appended. Other keys are copied onto the block.
- `preview` = the first 240 chars of text from blocks typed text/output_text/reasoning/reasoning_text, else `"tool calls"`.
- **Every delta updates both `request:` and `assistant:`, so the full streamed text is stored twice.**

### `_usage` (:124-135)
`replace` (the default) copies the incoming usage. `delta` adds numbers and overwrites other values.

### `_system_snapshot` (:228-250)
- Runs only for `request.prepared` whose `data.input` is a dict.
- **system** = `system`, else `instructions`, else the system/developer messages from `messages`/`input`.
- It returns None when there is no system prompt and no `tools` key.
- It compares `{system, tools}` with the latest system record for the same **agent_id and source_session_id**.
- If they differ, it creates `system:{request_id}` with status completed, title "System state" or "System updated", and `data={system, tools, before:{system,tools} or None, source_request_id, capture_level}`. The full prompt, tools and the previous copy are stored again on every change.

### `reduce` / `replay` (:253-290)
- Copy-on-write: a shallow copy of the record map and a deepcopy of each touched record.
- Events with seq ≤ `through_seq` are ignored, which makes replay idempotent.
- An event with `version≠1` or an unknown type is appended to `unsupported_events` as `{seq,type,version}`.
- `trajectory.started` sets `coverage_start`.

### Statistics and agents
- **`contribution(record)` (:301-314):** only for request and tool records. It returns `{request_count or tool_count: 1, error_count (status in ERROR), unknown_count, input_tokens, output_tokens, usage_missing}`. Token names are `input_tokens|prompt_tokens|input` and `output_tokens|completion_tokens|output`.
- **`statistics(state)` (:317-338):**
  - Sums the contributions.
  - `duration_ms` = the union of run [started_at, finished_at] intervals (sorted as ISO strings), or None.
  - Tokens are None unless `request_count > usage_missing`; `usage_complete = not missing`.
  - Also returns `through_seq` and `coverage_start`.
- **`agents(state)` (:341-345):** `[{agent_id, parent_agent_id, source_session_id, name=title, status, record_id}]` for agent records, in map order.

---

## 3. `project_events_in_tx` and the summary table (`repository.py:70-146`)

**Caller:** `recorder._append_prepared` inside the business transaction, right after the event rows are inserted (`recorder.py:165-166`). The events passed in carry the **original sanitized data, even when the stored row holds `{"$payload"}`** (`recorder.py:149-161`). So record rows are not limited by `TRAJECTORY_INLINE_BYTES=65536` (`recorder.py:150`).

| Step | Queries / work | Lines |
|---|---|---|
| 1. Find affected records | union of `targets(event)` without state | :72 |
| 2. Per-event lookups (one query each) | `message/part.committed` with message_id → SELECT ids of assistant rows with that message_id | :74-77 |
| | `request.prepared` → SELECT the latest **full** system row by agent_id (source_session_id not checked) | :78-83 |
| | `request.finished` → add `assistant:{request_id}` | :84-85 |
| | `run.interrupted` / `recording.gap` → SELECT **full** request/tool/assistant/step rows with status in {pending, running, streaming, waiting}, filter `data.run_id` in Python | :86-92 |
| 3. Load | SELECT full rows with record_id IN affected | :62-67, :93 |
| 4. Reduce | state = loaded rows (deepcopy), through_seq = projected_seq; record the old contributions; reduce every event | :94-99 |
| 5. Summary | get or create (running `idle`, recording `recording`, model None, applied_seq 0, zero statistics); totals = old + Σ(new − old contribution) | :100-111 |
| 6. Write records | for **every** record in state: kind, status, agent_id, message_id, start_seq, end_seq, applied_seq = as_of_seq, projector_version, **full data**, `summary` = record without data/blocks, `search_text = json.dumps(data)` | :112-126 |
| 7. Summary fields per event | `run.started` → running. `run.finished/interrupted` → waiting / error (status failed) / idle. `permission.requested` and `question.asked` → waiting. `request.started` with a model → `model`. `recording.gap` → summary and trajectory recording_status = gap | :127-139 |
| 8. Finish | statistics = totals; `applied_seq = committed_seq`; `last_activity_at = now()` (clock time of the projection, not the event time); `projected_seq = committed_seq`; flush. No checkpoint is built here | :140-146 |

Another writer: `mark_capture_paused_in_tx` sets `summary.recording_status=paused` (`recorder.py:237-240`).

**Write cost:** every append that touches a record rewrites the whole row (data, summary and search_text). Each `request.delta` batch rewrites the request and assistant rows with all text so far, which grows quadratically for long streams. Each request's record holds the full input snapshot, and the system record holds the prompt twice.

**Places where the incremental cache can drift from a full replay [P, untested]:**
- **D1:** the system row is preloaded by agent_id only, but the reducer also filters by source_session_id (`repository.py:79-81` vs `projector.py:241-243`).
- **D2:** the interrupt preload covers 4 statuses, but the reducer closes anything non-terminal (`repository.py:91` vs `projector.py:279`). The unknown_count statistic can differ.
- The cache is trusted whenever `applied_seq` and `projector_version` match. Drift would therefore be served by the fast paths and disagree with checkpoints and with the TS replay in the UI.

---

## 4. Read functions

**Shared rules**
- **`watermark(trajectory, requested)` (:45-47):** returns `committed_seq` (0 without a trajectory), or `sequence(requested, maximum=head)`. A value above head or a non-decimal → `TrajectoryError` → HTTP 400 (`types.py:72-78`).
- **Error mapping (`api/admin_trajectories.py:22-35`):** FileNotFoundError→410, LookupError→404, CorruptContent→409, TrajectoryError→400.
- **Cursors (:18-31):** base64url of canonical JSON without padding, at most 8192 chars, always a 3-element list.
- **`_deleted_payloads` (:50-53):** if any payload in the trajectory is not `available`, **every fast path is disabled**.
- **`_cache_changed_after` (:56-59):** true if any record with start_seq ≤ head has `applied_seq > head` or an old projector_version.

| Function | Inputs → output | DB access / cost | Guarantees |
|---|---|---|---|
| `get_trajectory` :34-42 | session_id, optional → (Session, trajectory or None) | Session row from the business table, then trajectory by (session_id, user_id, deleted_at IS NULL) | 404 "Session not found" if missing or deleted; 404 "Session has not started recording" unless optional; reads never create a trajectory |
| `state_at` :241-257 | trajectory, through → full state | **Fast** (through = projected_seq, nothing deleted): SELECT **all** records with full data, deepcopy; valid if every row with start_seq ≤ through has applied_seq ≤ through. **Slow:** latest checkpoint ≤ through, then `read_events` pages of 1000 with expansion, reduced | Fixed watermark. The fast path always returns `unsupported_events=[]`. Slow-path events are expanded, so current deletions apply. With any deleted payload there is no checkpoint, so replay starts at seq 1 |
| `get_checkpoint` :173-189 | at_seq → None or `{through_seq, projector_version, state, digest}` | None if anything is deleted; otherwise the latest same-version checkpoint ≤ at_seq; `expand` on state, then `expand_pages` (one IN query, 8 concurrent downloads) | Each page's sha256 is checked; `digest(state)==row.digest` or 409; a missing page → 409 |
| `create_checkpoint_in_tx` :149-170 | trajectory, optional state → row (added, not flushed) | Existing (trajectory, through_seq) is returned; otherwise state_at(projected_seq); records sorted by (int start_seq, record_id) into pages of 100 → `store_json` (dedupe reuses unchanged pages) | The digest covers the full expanded state. Pages get `first_seq=through_seq`. Stored state = `{…, records:{}, record_pages:[{$payload}]}` |
| `drain_checkpoints` :192-214 | limit 10 → count | Candidates are live trajectories with projected_seq − latest same-version checkpoint ≥ `TRAJECTORY_CHECKPOINT_INTERVAL` (1000), oldest updated_at first. State is computed in one session and the checkpoint written in another | No lock: replicas can collide on the primary key, and the error ends that worker iteration |
| `read_events` :217-238 | after_seq=0, until_seq, limit 500 (API allows 1..2000), include_data → `{events, from_seq, through_seq, until_seq, has_more, committed_seq}` | SELECT seq in (after, until] ORDER BY seq LIMIT limit+1; `expand` per event (at least one SELECT per payload reference, no cache across events) | **Gap check:** each seq must equal the expected next seq, else 409 "sequence gap"; if not `has_more` and the last seq ≠ until → "Committed trajectory tail is missing". `include_data=false` still returns the stored data, just unexpanded, with availability as it was stored. `through_seq` = last seq on the page, or after when empty |
| `list_sessions` :315-378 | user_id, user_query, q, workspace_id, status, recording_status, activity_from/to, include_unrecorded, cursor, limit ≤200, sort `last_activity_desc/asc` → `{items, next_cursor, has_more}` | One SELECT joining **sessions, users, workspaces**, left-joining the live trajectory and summary. Activity = coalesce(summary.last_activity_at, sessions.updated_at). Filters: `ilike` on username/email/id and title/id; status on coalesce(summary.running_status, sessions.status); recording_status computed from env flags (paused when disabled or not selected); `include_unrecorded` adds root sessions (`parent_id IS NULL`) that have no trajectory | Cursor is `[digest(filters+sort), activity ISO with microseconds, session_id]`; it must match the current filters or 400 |
| `_metadata` :260-280 | → session row dict | User/Workspace lookups unless passed in | Statistics come from the summary (duration_ms None; tokens None unless request_count > missing). recording_status is `not_recorded`, `paused` (env disabled for the owner) or the trajectory's value. Falls back to the session's status/model. `committed_seq`, `projected_through_seq`, `through_seq` (= committed) as strings |
| `get_session_header` :283-312 | session_id, through_seq → `_metadata` + `through_seq, statistics, agents, projector_version, capabilities{recording, admin_read:true, export}, unsupported_events` | **Fast** (summary.applied_seq = head = projected_seq, nothing deleted): SELECT data of agent/run records only, with **no ORDER BY**; then the cache-changed check, falling back to `state_at` | For a historical head: running_status from the last root run record by as_of_seq, model from the last request's `data.model` (:302-308). An unrecorded session returns 200 with a null trajectory_id and seq "0". The WebSocket uses this on subscribe and notify (`admin_trajectory_ws.py:54-62,120,147`) |
| `list_records` :381-425 | through_seq, `before` cursor [head, start_seq, record_id], limit ≤500, kind, status, agent_id → `{items (summaries ascending), next_cursor, has_more, through_seq, projector_version, unsupported_events}` | **Fast:** SELECT the `summary` JSON where start_seq ≤ head, keyset DESC LIMIT+1, then the cache-changed check. **Slow:** `state_at` and filtering in Python | The cursor must belong to the same head or 400. Items omit data/blocks. The fast path always returns `unsupported_events=[]`. The record_id tie-break uses DB collation on the fast path and Python ordering on the slow path |
| `get_record` :428-444 | record_id (opaque; path converter), through_seq → `{record:{…full, as_of_seq: head (overwritten), events}, through_seq, projector_version}` | **Always builds the full state** with `state_at(head)`, then reads events from start_seq−1 to min(head, as_of_seq) in pages of 1000 with expansion. An event is kept if the record id is in `targets(event, head_state)` (v1 only), or if the record is assistant/system and the request_id matches | 404 if the record does not exist at head. Events are matched against the state at head, not at event time. The `run.interrupted` / `recording.gap` that closed a tool or request is not included |
| `search` :447-467 | q (1..500), through_seq, cursor [head, q, offset], limit ≤200 → `{items:[{record_id, seq:as_of_seq, kind, preview}], next_cursor, has_more, through_seq}` | **Always builds the full state**; `json.dumps` of every record with a casefolded substring match; offset pagination | Matches JSON keys too. The preview is raw JSON from m−60 to m+180, with the index taken on the casefolded text. Results are ordered by (start_seq, record_id). The cursor is bound to head and q |

**Extra checks in the API**
- **Payload download (`admin_trajectories.py:116-133`):** `read_payload`, then audit, then `revalidate_viewer`. A new session re-runs `get_trajectory` and `validate_payload` and checks sha256(content) = row.sha256. The response is an attachment with `no-store` and `nosniff`.
- **Export download (:184-202):** `_validate_export_content` runs before and after the blob read (:162-173), plus a sha check.
- **Tests:** `test_trajectory_read_races.py:33-71` (role change → 403, token revoked → 401, asset or payload deleted → 410, root deleted → 404) and `test_trajectory_storage.py:229-242` (a stale header must not show future data).

**How the UI uses these endpoints (this is the contract)**
- On open it calls `GET /checkpoint` without `at_seq`, then fetches events after it and folds them with the TS projector (`sync.ts:228-244, 315-338`).
- To seek, it calls `checkpoint(at_seq)` and reads events up to exactly that seq (`sync.ts:417-423, 460-468`).
- A checkpoint is rejected on projector_version, through_seq or shape; **the digest is not checked** (`utils/adapter.ts:82-90`).
- Status codes: 404→not_recorded, 410→deleted (clears the screen), 409→corrupt (`sync.ts:111-124, 340-358`). For payloads, 404/410/409 → pending/deleted/corrupt (`api/queries.ts:207-211`).
- Header, records, record detail, search, payload and export go through TanStack Query (`queries.ts:55-323`).
- **So `/events` data must stay fully expanded, and `checkpoint.state` must contain full records, or client-side replay stops matching.**

---

## 5. Stored content (`payload.py`)

- **Backend choice, `get_storage` (:25-47):**
  - `set_storage` override first (:19-22), then a cached instance.
  - `local` if `blob_provider=="local"` or no `jwt_secret` (desktop uses `.openbox/trajectory-blobs`).
  - `gcs`, or `azure` (needs a connection string); anything else raises RuntimeError.
  - **There is no Aliyun OSS backend.** The config default is `azure` (`core/config.py:548`), and `k8s/aks.yaml:84` sets azure.
- **`upload_bytes` (:50-56):** passes `content_type` if the backend's upload takes it, else `metadata={content_type, sha256}`.
- **`download_bytes` (:59-65):** accepts awaitables or async iterators.
- **Storage keys:**
  - payloads: `trajectories/{trajectory_id}/payloads/{payload_id}/{sha256}` (:87)
  - exports: `trajectories/{trajectory_id}/exports/{export_id}/{sha}.zip` (`export.py:109`)
  - the local backend writes a `{key}.__meta__.json` sidecar (`blob/local_blob.py:23-24`)
- **`reference(row)` (:68-71)** = `{payload_id, sha256, size_bytes, media_type, availability}`.
- **`store_bytes` (:74-95):**
  - Computes sha256 and looks for an existing row by **(trajectory_id, sha256, media_type, source_asset_id, treated as IS NULL when None)**.
  - If that row is deleted → `OwnershipError("Removed trajectory content cannot be recreated")`. If available → its reference, which keeps the **original first_seq**.
  - Otherwise it inserts a row with storage_status pending, content = bytes, availability available, then flushes.
  - Dedupe is per trajectory only, best-effort (no unique constraint), and there is **no compression**.
- **`store_json` (:98-99):** `{"$payload": store_bytes(canonical(value), "application/json")}`. `canonical` = sort_keys, no spaces, `ensure_ascii=False`, `allow_nan=False` (`types.py:64-65`).
- **Where stored content comes from:**

| Writer | first_seq |
|---|---|
| Event data larger than `TRAJECTORY_INLINE_BYTES` | the event's seq (`recorder.py:150-151`) |
| Checkpoint pages | the checkpoint's through_seq (`repository.py:165`) |
| Captured assets | `trajectory.next_seq` (`artifacts.py:97`) |
| Request media | `trajectory.next_seq` passed by the caller (`agent/trajectory.py:218-219, 487-489`) |

- **`validate_payload` (:102-114):**
  - Needs a row with first_seq ≤ through_seq, else LookupError → **404**.
  - availability ≠ available → FileNotFoundError → **410**.
  - If source_asset_id is set, it reads the **business `file_assets` row**; missing, `is_deleted`, `deleted_at` or status deleted → **410**.
- **`read_payload` (:117-125):** bytes come from the DB column or the blob store. A blob FileNotFoundError or a sha mismatch → **409**. Other backend errors become a 500.
- **`expand` (:128-142):**
  - Without `$payload` it just applies `visible_references`.
  - With `$payload` it reads the content. If deleted it returns `{"$payload": {…, availability: "deleted", reason: "explicitly_deleted"}}`. Non-object JSON → 409. Then it applies `visible_references` to the content.
- **`visible_references` (:145-163):** walks the value recursively.
  - Any dict with a string `payload_id` and a `sha256` key triggers a SELECT (cached within one call) filtered by first_seq ≤ through. The copy gets `availability = row.availability`, or `not_recorded` if there is no visible row, plus `reason: explicitly_deleted` when deleted.
  - A dict whose child `payload` is deleted gets `availability: "deleted"` itself.
  - **The current deletion state applies at every historical watermark.**
- **`expand_pages` (:166-187):** one IN query; a missing or unavailable page → 409; 8 concurrent downloads; sha check; each page must be JSON with a `records` dict.
- **`delete_for_asset` (:190-196):** every payload with that source_asset_id, **in every trajectory**, becomes deleted, `deleted_at` is set and the content column is cleared. This runs in the asset deletion transaction.
- **`drain_payloads` (:199-232):**
  1. Session 1 selects pending, available rows by created_at, limit 100, and copies their bytes into memory.
  2. Outside any transaction: check sha, upload, **download again and check sha**.
  3. Session 2 runs a conditional UPDATE (payload_id, sha, available, pending) that clears content and sets `stored`.
  4. If the row is no longer available, the blob is deleted.

  On failure the DB bytes stay as the source of truth (test `test_trajectory_storage.py:186-209`).
- **Archive worker (:238-271):** every 5 s it runs `drain_payloads(100)`, `drain_checkpoints(10)` and `purge_deleted_content(100)`, logging errors. It runs **in every API process**, with no leader election. It is started at `main.py:107-108` and stopped after the export tasks and the recorder flush (`main.py:79-91`).
- **Status values:**
  - row availability: `available` or `deleted`
  - reference-level availability also includes `not_recorded`, `pending` (asset not ready, `artifacts.py:88-89`), and `deleted` with reason `explicitly_deleted` or `source_attachment_deleted`
  - `not_recorded` reasons: `invalid_base64`, `ambiguous_asset_source`, `asset_missing`
  - storage_status: `pending` (bytes in the DB) or `stored` (blob only)
- **What `first_seq` means:** the lowest seq that may reference the content (an exact seq for event data, a lower bound for media). Content is **visible at H only if first_seq ≤ H** (`payload.py:104,156,169`; `export.py:87`; `admin_trajectories.py:166,169`); before that it is 404, or `not_recorded` inside expanded data. Dedupe keeps the earliest first_seq, so reused checkpoint pages stay visible to later checkpoints. `through_seq` is the requested watermark H (≤ committed_seq); expansion inside `read_events` uses `until`, and permission recovery uses the event's own seq (`permission.py:242`).

---

## 6. Media (`artifacts.py`)

### Reference formats
- **`{"$payload": ref}`**: externalized event data and checkpoint pages. The UI checks `payload_id` (`protocol.ts:63-80`).
- **`$media` wrapper** (`artifacts.py:157-199`):
  - `{"$media": ref or {availability, reason, [sha256], media_type}, source_kind: asset|inline_non_asset|ambiguous_asset, original_encoding:"base64", declared_media_type, [source_asset_id]}` (`protocol.ts:82-100`).
  - It never holds base64 or a URL.
- **Service dispatch bodies:** URLs become `"trajectory-media:{payload_id or asset_id}"`, plus a `media_inputs` manifest `{id:{asset_id, payload}}` (`agent/trajectory.py:266-275, 329-332, 464-465`).
- **`artifact.recorded`** (:99-103): `{artifact_id, name, media_type, size_bytes, role, availability, sha256, payload: ref, source_asset_id}`, with event_id `asset:{sha256("{trajectory_id}:{asset_id}:{sha}")}`.
- **`artifact.removed`** (:233-237): `{artifact_id, availability:"deleted", reason:"explicitly_deleted"}`, with event_id `asset:{sha256("{trajectory_id}:{asset_id}:deleted")}`.

### Functions
| Function | Behavior |
|---|---|
| `read_asset_bytes` :18-25 | The only presign use in scope: `presign_get(oss_key, 120s)`, then an httpx GET (no redirects, `trust_env=False`) that reads the **whole file into memory**. Callers: `api/assets.py:280-281` (upload completion, up to `_MAX_SIZE` = 1 GiB at `api/assets.py:37`), `agent/trajectory.py:167,212`, `tool/image_gen.py:686`, `sandbox/assets.py:195`, `tool/video_production.py:787` |
| `_retained_asset` :28-51 | Payloads (content not loaded) joined to the live trajectory for (session_id, user_id) with that source_asset_id, optionally the same sha, newest first_seq first. In groups of 200, checks for an `artifact.recorded` event with the computed id. This keeps derived frames stored under a source id from being reused as the source |
| `prepare_asset_ids` :54-76 | Runs outside the transaction. Skips deleted or not-ready assets. The owner or workspace must match, else OwnershipError. Skips assets already kept in the root session; fetches bytes for the rest |
| `capture_asset_in_tx` :79-104 | None if there is no context or recording is off. Checks ownership. Deleted → `{artifact_id, availability:deleted}`; not ready → pending. An existing version is reused. Missing bytes → RecordingError. Otherwise it ensures the trajectory, **copies the bytes** (`media_type=asset.mime`, `source_asset_id`) and records `artifact.recorded` in the same transaction |
| `capture_asset_ids_in_tx` :107-118 | Per unique id: missing asset → `{availability:not_recorded, reason:asset_missing}`. Callers: `session/session.py:583-585, 633-636`; `agent/trajectory.py:240`; `tool/image_gen.py:362-364` |
| `capture_result_asset_in_tx` :121-131 | Builds context from the saved trace or activity context, adds request_id, role `result`. Callers: `tool/image_gen.py:510-511, 707`; `tool/douyin_publish.py:131-132`; `sandbox/assets.py:217` |
| `retain_request_media_in_tx` :134-219 | Ensures the trajectory, then walks a copy of the snapshot (the SDK body is untouched, test `test_trajectory_media_deletion.py:53`). It handles `data:<mime>;base64,` strings, `{type:"base64", data}` dicts and `input_audio.data` (`audio/{format or wav}`). For each item: invalid base64 → not_recorded. It looks for a source: the caller's sha→asset map, or a single existing asset-bound payload with that sha. Several sources with no binding → ambiguous marker. A missing or deleted source → deleted marker, **never stored again**. Ownership is checked. It reuses the existing reference if possible, otherwise `store_bytes` **copies** the content (bound to the source, or unbound as `inline_non_asset`). Callers: `agent/trajectory.py:483-490` (`request.prepared` input, same transaction) and `:218-221` (sampled frames bound to the source video) |
| `revoke_asset_in_tx` :222-237 | Selects trajectories that have payloads for the asset (deleted ones included), calls `delete_for_asset`, then records `artifact.removed` for each trajectory whose session still exists, in the **same transaction**. That call is subject to `enabled()`, and a failure raises RecordingError and **aborts the asset deletion**. Callers: `api/assets.py:292-293` (oversized upload) and `:433-434` (DELETE), before commit; the OSS object is deleted after commit (`:436-440`) |

### How asset deletion reaches reads
- **Payload endpoint:** payload rows flip to deleted, so the endpoint returns 410.
- **Expanded data:** `visible_references` marks the references and their parent dicts deleted.
- **Caches and checkpoints:** `_deleted_payloads` turns off all caches and checkpoints for that trajectory.
- **Exports:** exports created before the deletion become invalid (`deleted_at > created_at`), as does any available payload whose source asset is gone (a join with `file_assets` at `admin_trajectories.py:162-173`). New exports list the deleted payloads under `missing_payloads`.
- **Later captures:** they produce deleted markers.
- **Blob cleanup:** `purge_deleted_content` removes the blobs.

Tests: `test_trajectory_media_deletion.py:23-103`, `test_trajectory_boundaries.py:195-222`.

---

## 7. Export (`export.py`)

- **Start:**
  - `create_export` (:27-33) inserts a pending row with `through_seq = watermark(body.through_seq)` (`admin_trajectories.py:146`).
  - The build runs as a FastAPI BackgroundTask **in the API process** (:149).
  - `resume_exports` restarts pending/running jobs at startup when admin reads are enabled (`main.py:115-117`; `export.py:131-136`).
  - `stop_exports` cancels tasks (:139-145).
- **`_build_export` (:50-128)**, built in memory with `ZIP_DEFLATED`:

| Entry | Source |
|---|---|
| `events.jsonl` | `read_events` pages of 1000 with `include_data=False`: canonical `event_dict` lines holding the **stored** data (unexpanded `$payload`, availability as stored) |
| `statistics.json` | `canonical(statistics(state_at(through)))` |
| `payloads/{payload_id}` | **Every** payload with first_seq ≤ through (externalized event JSON, checkpoint pages, media copies) via `read_payload`. FileNotFoundError or LookupError → `missing_payloads[{payload_id, availability, reason: exception class}]`; CorruptContent fails the whole export |
| `manifest.json` (written last) | `{format:"openbox.session-trajectory", version:1, projector_version:1 (hard-coded), trajectory_id, through_seq, created_at, files:[{path, sha256, size_bytes, [payload_id, media_type]}], missing_payloads, user_id, session_id, coverage_start, recording_status (current value, not as of through), gaps:[{record_id, seq:start_seq, data}], complete: no missing and no unsupported and no gaps, unsupported_events}` |

- **Finish:**
  1. `assert_admin` before and after the build.
  2. Hash the zip, upload, **download again and check the hash**.
  3. Lock the trajectory and export rows FOR UPDATE. If either is deleted, delete the blob; otherwise set `completed`, `storage_key`, `sha256`.
  4. Any exception → `failed` with `error` = exception class name.
- **`export_status` (:36-38):** `{export_id, status, through_seq, error, download_url}`; the download URL is a relative API path, only when completed.
- Everything is held in memory (events and payload bytes).
- Tests: `test_trajectory_storage.py:160-184, 244-270, 356-388`.

---

## 8. Deletion

### `delete_trajectory_in_tx(db, session_id, user_id)` (`lifecycle.py:10-28`)
Called inside the session deletion transaction (`session/session.py:340-341`).

1. SELECT the trajectory by session_id FOR UPDATE (deleted ones included). If there is none, return: child sessions have no trajectory of their own, and their events stay in the parent's.
2. If the owner differs → OwnershipError, which **aborts the session deletion**.
3. Set `deleted_at` and `recording_status=deleted`. The row stays as a tombstone; `ensure_trajectory_in_tx` refuses to restart it (`recorder.py:79-80`).
4. **Hard DELETE** events, records, checkpoints and summaries.
5. Payload rows stay as tombstones: deleted, `deleted_at` set, content cleared, `storage_key` kept for cleanup.
6. Exports set to `deleted`.
7. After commit, publish `{user_id, owner_user_id, session_id, trajectory_id, committed_seq, deleted:True}` (`recorder.py:243-253`).

### `purge_deleted_content(limit=100)` (`lifecycle.py:31-59`)
- One session selects up to 100 deleted payloads with `storage_key != ""` and up to 100 deleted exports with a storage key.
- Blobs are deleted outside any transaction.
- On success, a new session sets payload `storage_key=""` or export `storage_key=None`, if still deleted.
- Errors are skipped silently, and there is no ordering or retry counter, so up to 100 permanently failing rows can block the rest **[P]**.
- Rows are never removed; the tombstones back the rule that deleted content cannot be recreated.

### What readers see
- `get_trajectory` returns 404 because the session is deleted, and the WebSocket sends `SESSION_NOT_FOUND`.
- The UI clears everything on a 410, or on a 404 after it had loaded (`sync.ts:340-358`).
- Deletion wins over an export in progress (`test_trajectory_storage.py:356-388`).

---

## 9. Other hazards found (verified by reading unless marked [P])

1. **One deleted payload changes read cost for the trajectory.** Header, record list, record detail, search and `state_at` all replay from seq 1 with per-event payload queries. `drain_checkpoints` still builds a checkpoint every 1000 events by replaying from 0, and those checkpoints are never used (`repository.py:174-175, 243, 288, 383`).
2. **Record detail and search always build the full state** (`repository.py:430, 449`).
3. **Record rows have no size limit and duplicate content:** `data` plus `search_text`, blocks in both request and assistant records, and `before` in system records.
4. **Asset and media bytes are copied into DB storage inside business transactions** (up to 1 GiB on upload completion).
5. **Event reads run one payload query per reference.**
6. **Fast paths disagree with slow paths in small ways:** `unsupported_events` is always `[]` on fast paths; header agent order is not deterministic (no ORDER BY); `last_activity_at` is projection clock time; D1 and D2 from §3.

---

## Migration notes

### (a) Move the tables to a separate trace database

**Moves to the worker (own engine, metadata and alembic):** all 7 tables, plus `repository.py`, `projector.py`, `payload.py`, `export.py`, `lifecycle.py`, `api/admin_trajectories.py`, `api/admin_trajectory_ws.py`, the archive loop and `resume_exports`.

**Business code that reads or writes these tables today and will break:**

| Site | Dependency now | Needed instead |
|---|---|---|
| `recorder.py:165-166` | Projection runs in the business transaction | Worker projects asynchronously in batches |
| `permission/permission.py:215, 226-247` | Recovers a committed approval from the `permission.resolved:{id}` event via `expand` when the Redis wake-up is lost | Must read business permission/question state; this correctness path cannot wait on asynchronous recording |
| `session/session.py:538-556, 602-608` | "Baseline already captured?" via SessionTrajectory and `evt_baseline_{id}` | A business-side marker, or always emit a deterministic baseline event_id and let the worker keep the first |
| `session/fork.py:178-185` | Puts the source trajectory id and committed_seq into `history.forked` | Emit session ids; the worker resolves trajectory and seq |
| `session/session.py:340-341` | Synchronous cascade; an OwnershipError aborts session deletion | Emit `session.deleted` after commit; worker tombstones; never fails the business write |
| `api/assets.py:292-293, 433-434` | Synchronous revocation plus `artifact.removed` in the asset transaction | Emit `asset.deleted` after commit; worker flips references |
| `artifacts.py:28-76` (`_retained_asset`, `prepare_asset_ids`) | Decides whether to skip a network fetch using trace tables | Not needed once assets are recorded as references |
| `db/base.py:109-123, 335-341`; `db/models/__init__.py:52-53`; `main.py:79-117` | Shared `create_all`, readiness probe, metadata and startup hooks | Separate metadata, readiness check and worker lifespan; decide what desktop/SQLite does |

**Reads that currently join business data:**
- `get_trajectory` needs `sessions.is_deleted` and `user_id` (`repository.py:35-39`).
- `list_sessions` joins sessions, users and workspaces, and uses title, status, parent_id, updated_at, model and agent (`repository.py:323-370`).
- `_metadata` needs username, email and workspace name (`repository.py:260-280`).
- `validate_payload` and `_validate_export_content` need `file_assets` liveness (`payload.py:109-113`, `admin_trajectories.py:167-171`).
- `revoke_asset_in_tx` needs sessions (`artifacts.py:230-231`).
- Admin auth reads `users` (`trajectory/auth.py:44-54`); token revocation uses the Redis cache (`admin_trajectory_ws.py:48`); audit rows go to business tables (`admin_trajectories.py:38-39`, `admin_trajectory_ws.py:125-131`).
- The env flags `enabled` and `selected_user_ids` are evaluated at read time (`repository.py:274, 310, 348-351`).

The worker needs either denormalized session, owner, workspace and asset state fed by emitted facts, or read-only access to the business DB. `include_unrecorded` (listing sessions with no trajectory) cannot work without reading business sessions; decide whether to keep it.

**Seq meaning changes.** `committed_seq` is currently the business commit point. With the worker assigning seq it becomes "ingested", and `projected_through_seq` can lag behind it. Reads at H ≤ committed with projection < H already fall back to replay (`state_at`), so that part still works. WebSocket notifications must come from the worker; the bus has an optional Redis fan-out (`bus/bus.py:1, 65-82`). Keep the `applied_seq` guard against stale cached data (`test_trajectory_storage.py:229-242`).

**Deletion windows.** Today reads check business deletion synchronously. With asynchronous delete events there is a window where a deleted session or asset is still readable, unless the worker also checks liveness or the business side publishes deletions before committing.

**Tests to rework:** `tests/integration/test_trajectory_storage.py:27-58` builds both schemas in one DB; also the readiness tests and `test_trajectory_migration.py:8-22`.

### (b) Stored bytes in OSS, content-addressed, zstd

**Today:**
- one row per trajectory
- a key per payload_id, raw bytes, the DB column as the "committed means readable" staging area
- dedupe on (trajectory, sha, media_type, source_asset_id)
- no compression; `encoding` unused
- no OSS backend in `get_storage`

**Proposed layout.** Use a global CAS key (for example `sha256/{aa}/{sha}.zst`, sha of the **uncompressed** bytes). Keep a per-trajectory reference table with:
- ref id (keep the `payload_id` shape used by `$payload` and `$media` and by the UI)
- trajectory_id, sha256, size_bytes (uncompressed), media_type, compression
- first_seq, availability, deleted_at, source_asset_id

Visibility (first_seq) and deletion stay per trajectory while bytes are shared.

**Must keep:**
- The sha256 check of uncompressed bytes on every read path (`payload.py:123, 178`; export; API re-checks).
- 404 before first_seq, 410 when deleted, 409 when corrupt.
- The rule that deleted content cannot be recreated (`payload.py:82-84`); with shared blobs it has to be enforced on references.
- The upload-then-verify step (`payload.py:219-220`), or use OSS Content-MD5 instead.

**Physical deletion becomes reference counting:** a blob can go only when no available reference remains anywhere. The media deletion test expects no available image payloads after the source is deleted (`test_trajectory_media_deletion.py:101-103`). A different trajectory's unbound copy of identical bytes would keep the blob alive; **decide whether that is acceptable for privacy.**

**Prompt dedupe (system, tools, each message once).** This replaces:
- the repeated system/tools/`before` copies (`projector.py:246-249`)
- the full input stored on every `request.prepared` event and request record

Because the client replays events, `/events` must still return fully expanded values, or the TS projector and protocol must learn the new references (bump projector_version, regenerate fixtures, update `docs/SESSION_TRAJECTORY_PROTOCOL.md:60-105`).

**Media from `file_assets` as references, not copies.** This removes the `read_asset_bytes` copies (including the 1 GiB upload path).

**Open questions:**
- Assets have no stored hash, so what does the digest check become?
- Can an object change under the same `oss_key`? `register_owned_media_inputs` looks assets up by key (`agent/trajectory.py:159-163`).
- The payload endpoint would stream or presign from the asset while it is live and return 410 after deletion.
- The `artifact.recorded` event id is derived from the sha (`artifacts.py:44, 102`) and needs an asset-based identity.

**Still copied:** inline, non-asset base64 media, and sampled frames. Frames keep their explicit source binding (`artifacts.py:160-176`) so deletion still propagates.

Checkpoint pages become CAS blobs, and the state digest check stays. Once the spool holds large values, it is the staging area, so bound spool file sizes.

### (c) Records keep a summary plus references; read one record by index

**What the wire returns today:**
- `/records` rows: every field except data and blocks, including usage, preview (240 chars) and status_reason (500 chars). These can become summary columns.
- `/records/{id}` and `/checkpoint`: full `data`, `blocks` and `events`.
- The UI replay produces full records.

Either resolve references back to full records at read time, or change the protocol.

**Reading one record at a historical H.** The cache is only valid at head (`applied_seq ≤ H`). The reducer also makes records depend on each other:
- assistant chosen by message_id through state (`projector.py:37-40`)
- `_system_snapshot` compares against the previous system record
- `run.interrupted` / `recording.gap` change other records
- `request.finished` updates the assistant record

Store an event→record_ids index at projection time and rebuild from those events only. This replaces scanning [start_seq, as_of_seq] with head state (`repository.py:437-443`), and makes the returned event list exact, including the events that closed the record. Otherwise keep record versions.

**Search.** Removing `search_text` changes behavior: today any substring of the full record JSON matches, including keys, casefolded; the preview is a JSON slice; `seq` = `as_of_seq`; cursors are offsets bound to (H, q). Define the new tsvector/pg_trgm fields, keep the fixed watermark (start_seq ≤ H with an applied_seq guard or fallback), and make sure deleted content drops out of the index.

**Remove the `_deleted_payloads` fast-path kill switch.** Keep availability on references and resolve it at read time, like `visible_references`.

**Move summary maintenance to the worker** (`repository.py:100-143`). Use the event's time for `last_activity_at`. Keep the statistics contract (usage_missing, nullable tokens, duration from run intervals). Fix D1 and D2, or check the cache against fixture replay in tests.

### (d) Archive cold events to OSS segments

- **Reading across hot and cold.** `read_events` must keep checking for gaps and the missing tail (`repository.py:226-235`). Keep a segment manifest per trajectory: first_seq, last_seq, key, sha256 (compressed and uncompressed), count, compression. Reading up to `until` may require scanning part of a segment. `state_at`, UI open/seek, `get_record` and export all read tails from segments, so align segment size with the checkpoint interval (1000).
- **Digests.** Events have `content_hash` (the idempotency digest, `recorder.py:123`), but no read ever verifies it. A segment's sha256 must be checked on read, with 409 on mismatch.
- **Deletion.** Trajectory deletion must delete segment objects through the cleanup queue (today it hard-deletes rows, `lifecycle.py:19-20`). Asset deletion needs no rewrite **only if media is never inline in events** (`retain_request_media_in_tx` guarantees references), but prompts and file-diff text stay inline. `visible_references`-style availability must be applied to events read back from segments.
- **Idempotency lookups.** The global event_id primary key (`models:31`) is used for dedupe, `IdempotencyConflict`, artifact identities (`artifacts.py:46-47`), baseline checks (`session.py:554, 607`) and permission recovery (`permission.py:235`). After archival, keep an event_id → (trajectory, seq, hash) index in the trace DB.
- **Export** must stream from segments instead of building the ZIP in memory (`export.py:64-105`).

### Risks and open questions
1. The UI's replay must match server records at every watermark (TS projector plus shared fixtures). Any reference-only data in events or checkpoints is a protocol change.
2. Business code currently reads its own writes synchronously: permission recovery, baseline check, fork seq and deletion cascades.
3. Does global content addressing break the per-trajectory deletion guarantee?
4. What replaces the digest check for asset references, and are OSS keys immutable?
5. Desktop mode (single SQLite, no JWT secret, local blobs): separate DB and worker, or unchanged?
6. Existing data: event data is inline or `$payload`; bytes live in DB columns and in per-payload blob keys. Records, checkpoints and summaries can be rebuilt from events (projector_version 1); exports can be dropped.
7. `list_sessions` needs business session, user and workspace data (including `include_unrecorded`); the worker's env flags must match the API's.
8. The fixes for D1, D2 and cleanup starvation should land before caches or summaries are trusted in the worker.