# Trajectory producers and error handling in `backend/` (excluding `backend/trajectory/` and `backend/tests/`)

Terms used below:
- **db**: the call is `record(..., db=session)`. The event is appended inside the caller's transaction and commits with the business write.
- **own tx**: the call is `record(...)` with no `db`. It first waits for that session's pending stream chunks (`flush`), then opens its own transaction and waits for the commit. The committed event is the receipt.
- **stream**: `record_stream`. It returns a task, and the caller awaits that task as the receipt.
- **R1** and **R2**: the two fences currently enforced by the recorder (defined in §3).

## 0. Recorder contract that producers rely on (context, all in `trajectory/`)

- **`record()`** (recorder.py:201-224)
  - `context=None` does nothing (203-205).
  - If recording is disabled for the user, it still writes the paused marker, in the caller's transaction or its own (206-212, 227-240).
  - With `db`, it appends in the caller's transaction and returns a `PendingRange` (218-219).
  - Without `db`, it calls `_retire_previous_failed_run` and `flush(context)`, then commits in its own transaction and returns the event dict (220-224).
  - It is wrapped by `recording_boundary`: any exception that is not a `TrajectoryError` becomes `RecordingError` (recorder.py:446-450; types.py:127-139).
- **`append_events_in_tx`** (175-198) does all of the following inside the business process:
  - `prepare` validation, which raises `TrajectoryError` (types.py:81-120).
  - Source-session ancestry check per event (179-181; 43-63).
  - The **run fence R1** (182-187).
  - `ensure_trajectory_in_tx`: upsert plus `FOR UPDATE KEY SHARE`, `trajectory.started`, baseline, resume gap (66-103).
  - Recovery of a failed stream as a `recording.gap` (189-197).
  - `_append_prepared` (114-172):
    - row lock on `session_trajectories`; `sanitize`; `IdempotencyConflict` (126, 133)
    - seq assignment; data over 65536 bytes goes to `store_json` as bytes in `trajectory_payloads.content` (150-151)
    - `TrajectoryEvent` insert
    - **inline projection** `project_events_in_tx` (165-166; repository.py:70-147 writes `trajectory_records` including `search_text` at 126, plus `trajectory_session_summaries`)
    - a notification hint published as bus `trajectory.available` from a **global** SQLAlchemy `after_commit` listener (167-169, 243-262)
- **`record_stream`** (387-410): one `_StreamQueue` per (loop, user, session).
  - Limits: `TRAJECTORY_PENDING_BYTES` 4 MiB, `TRAJECTORY_TOTAL_PENDING_BYTES` 64 MiB, batch timer `TRAJECTORY_BATCH_MS` 50 ms (265-326).
  - Back-pressure **blocks the producer**; nothing is dropped (270-275, 310-313).
  - A failure poisons the queue (340-353) and later becomes a `recording.gap` (366-385, 189-197).
- **Error classes** (types.py:34-47, 123-124): `TrajectoryError(ValueError)`, with subclasses `OwnershipError`, `IdempotencyConflict`, `CorruptContent`, `RecordingError`. Because the base class is a `ValueError`, any plain `except ValueError` catches all of them.
- **Flags** (config.py:19-24): `TRAJECTORY_RECORDING_ENABLED` plus `TRAJECTORY_RECORD_USER_IDS`; admin flags are separate.
- **Identity**: the `TraceContext` dataclass (context.py:8-41) and the ContextVar accessors `bind`/`current` (44-57).

---

## 1. Import and call-site inventory

### 1.1 Session and chat projection

| Site | Call → event(s) | Tx | Bound business action | On failure: today → target |
|---|---|---|---|---|
| session/session.py:340-341 | `delete_trajectory_in_tx`: no event. Deletes events, records, checkpoints and summaries; nulls payload content; marks exports deleted; queues a notification (lifecycle.py:10-28). | db | `delete_session` soft delete and run invalidation (320-344) | The delete rolls back → emit_after_commit a deletion fact; retention must be reliable |
| session/session.py:390-401 | `record_projection_in_tx` → `session.settings_changed` {before, after} for keys title/model/variant/agent/project_id/directory/revert | db | `update_session` UPDATE | The update rolls back (R2 can also raise here) → emit_after_commit |
| session/session.py:500-528 | `trajectory_context_in_tx`: **fence R2** (510-516); otherwise resolves the context from `SessionExecution.trace_context` (518-527) or `context_for_session` | read | every projection write | Raises before the write → replace with an independent runtime check |
| session/session.py:531-561 | `prepare_trajectory_assets`: calls `trajectory_context_in_tx` (547); reads `SessionTrajectory` and `TrajectoryEvent` (551-555) and file parts (556-560); `prepare_asset_ids` downloads the bytes from OSS (artifacts.py:54-76) | own read session | runs before save_part, update_part_data, create_user_message, revert, fork, cron | The operation aborts → remove; record references only |
| session/session.py:570-593 | `record_projection_in_tx`: `record(type, data, db)` (587); `capture_asset_ids_in_tx` → `artifact.recorded` role attachment, bytes stored (582-586); `plan.changed` for plan parts (588-589); `mark_capture_paused_in_tx` when disabled (591-592) | db | all rows below | Rollback → emit_after_commit |
| session/session.py:596-637 | `capture_trajectory_baseline_in_tx`: checks the trace DB for an existing baseline (603-608); reads the full message and part history (609-619); captures baseline_input assets (630-636); `ensure_trajectory_in_tx(baseline=)` → `trajectory.started`, `baseline.captured` | db | first activity after enablement | Rollback of the business write → worker-side baseline |
| session/session.py:682-738 | prepare (682); context (684); derives turn_id = msg_id (686-690); baseline (691); writes `execution.trace_context` (721-724); `turn.started` (726, non-synthetic only); `input.accepted` or `input.injected` (727-730); `message.committed` (731-733); `part.committed` (734-735); paused marker (737-738) | db (`runtime.transaction`) | `create_user_message`: Message and Part rows plus `invalidate_locked` (generation + 1) | Prompt rejected → emit_after_commit |
| session/session.py:800-801 | `message.committed` created | db | `create_assistant_message` | Rollback → emit_after_commit |
| session/session.py:848-853 | `message.committed` updated; `context.replaced` when summary and finish=stop | db | `update_message_info` | Rollback → emit_after_commit |
| session/session.py:901-923 | prepare the file asset (901-902); `part.committed` (plus artifact or plan events) | db | `save_part` | Part lost → emit_after_commit |
| session/session.py:1079-1080 | `history.regenerated` | db | `regenerate`: hard delete | Rollback → emit_after_commit |
| session/session.py:1159-1160 | `history.reverted` dismiss_failed_turn | db | `delete_failed_turn` | Rollback → emit_after_commit |
| session/session.py:1203-1216 | prepare; `part.committed` updated | db | `update_part_data` | Rollback → emit_after_commit |
| session/revert.py:20-41 | Disabled: paused marker (22-27). Enabled: prepare (31), `context_for_session` (33), baseline (34-35), `history.reverted` requested with id `{op}:requested` (39-40) | db (a dedicated transaction, no business row) | runs before `snapshot.restore` (116, 145) | The restore never runs → emit |
| session/revert.py:44-51 | `history.reverted` completed/failed, id `{op}:finished` | own tx | after the restore | Re-raised (126, 156) → emit |
| session/fork.py:75-197 | prepare (80-81); in the copy transaction: flush (173), source context (174), source baseline (176-177), **reads `SessionTrajectory`** for `source_trajectory_id` and `source_through_seq` (178-186), `history.forked` outgoing `fork:{new}:source` (187-188), destination baseline (189-192), `history.forked` incoming `fork:{new}:destination` (193-194); paused marker (196-197) | db | message and part copy (the new session row from 49-57 is already committed) | Copy rolls back → emit_after_commit; the worker resolves the trajectory id and seq |
| storage/storage.py:56-96 | `enabled()` with no user (71); **`SELECT sessions FOR UPDATE`** (75-76); `activity_context` (78); reads the previous value (79-82); `todo.changed` {before, after, items} (93-96) | db | kv_store todo upsert (session/todo.py:45-47; session/abort.py:120-121) | Rollback → emit_after_commit; drop the lock |

### 1.2 Run, turn, question and permission

| Site | Call → event(s) | Tx | Bound action | Failure today → target |
|---|---|---|---|---|
| question/runtime.py:42-55 | `get_run_trace`: DB read of `trace_context`, validated against the ticket | read | run_loop start | identity |
| question/runtime.py:58-87 | `activity_context` (66); `turn.started` origin continuation/execution (72-77); writes `execution.trace_context` (80); `run.started` `run_start:{run}` (81-86) | db (`start_run` tx 141-184) | lease acquisition | Run not started → emit_after_commit |
| question/runtime.py:90-103 | `run.finished` / `run.interrupted`, id `{type}:{run}` | db | `finish_run` 249-250; `invalidate_locked` 285-289; `recover_expired_runs` 333-335 (lease_expired) | Lease not released → emit_after_commit |
| question/runtime.py:251-256 | `turn.finished` `turn_finish:{turn}` (non-cron sessions) | db | `finish_run` | emit_after_commit |
| question/runtime.py:270-279 | `checkpoint_context` + `question.cancelled` | db | `invalidate_locked` | emit_after_commit |
| question/runtime.py:306-311 | `run.cancel_requested` | db | `cancel_session` | emit_after_commit |
| question/question.py:109-136 | `activity_context` (116-119); `baseline.captured` preexisting_question `question_adopt:{id}` (126-131); writes `continuation.trace_context` (132); writes `execution.trace_context` (133-135) | db | any checkpoint mutation | emit_after_commit |
| question/question.py:139-160 | `question.*` `{type}:{id}:{draft_revision}` (144-150); `takeover.requested` / `takeover.finished` `takeover:{type}:{id}` (151-159) | db | the callers below | emit_after_commit |
| question/question.py:240, 246-248 | `question.asked`; `part.committed` for the waiting part | db | `ask` | emit_after_commit |
| question/question.py:309-312 | `question.resolved` | db | `_resolve` | Today → HTTP 422 (api/questions.py:31-32) → emit_after_commit |
| question/question.py:342-346 | `question.draft_saved` | db | `save_draft` | same |
| question/continuation.py:105-143 | `part.committed` (122); `tool.finished` `question_tool_finish:{id}` (123-127); `input.injected` `question_inject:{id}` (130-133); `message.committed` (136-139); `session.settings_changed` (140-143) | db | `apply_answers` (exactly once, via `row.applied`) | Today a permanent resume failure (237) → emit_after_commit |
| question/continuation.py:178-183 | `question.cancelled` reason expired | db | `expire_questions` | emit_after_commit |
| permission/permission.py:59-76 | `_trace_permission`: its own transaction; `activity_context`; `record(..., db, event_id "{type}:{id}")` | db in a dedicated tx | none | Raises out of `ask` / `reply` → emit |
| permission/permission.py:79-91, 300, 303 | `_trace_rule`: `permission.requested` plus `permission.resolved` for policy allow/deny. **Two transactions per matched pattern, on every tool call** (hooks.py:322-330) | own txs | tool authorization | Tool blocked → emit |
| permission/permission.py:321-333 | `permission.requested` (user). The trace is kept in `PendingPermission` and in the Redis `perm_req:{id}` JSON. | own tx | before PERMISSION_ASKED (342) | emit |
| permission/permission.py:351-355 | `permission.expired` run_cancelled | own tx | CancelledError path | Replaces the cancellation → emit |
| permission/permission.py:398-402, 437-440, 450-453 | `permission.resolved` (user, policy "always", reject cascade) | own tx | `reply`, before wake-up and Redis publish | Waiter never woken → emit |
| permission/permission.py:215, 226-247 | **Reads** `TrajectoryEvent permission.resolved:{id}` and `expand`s its payload as the wake-up fallback | read | `_wait_via_redis` | Business correctness depends on trace data → replace |

### 1.3 Cron

| Site | Call → event(s) | Tx | Bound action | Failure today → target |
|---|---|---|---|---|
| cron/executor.py:17-22 | `bind(None)` | none | scheduler isolation | identity |
| cron/executor.py:64-74, 95-101, 260 | When enabled, the temp session is created before the run entry (69-71); `bind(trace)`; `temp_trace` (95); `bind(temp_trace)` (100) | none | cron run | identity |
| cron/executor.py:614-653 | prepare (623-624); **session lock only when enabled** (626-629); context with turn_id = run_id and a new agent_id (630-631); baseline (632); `CronRun.trace_context` (643); `turn.started` (647); `job.submitted` `cron:{run}:submitted` (648-652) | db | CronRun insert | Run not created → emit_after_commit |
| cron/executor.py:656-668 | `agent.spawned` (664-666); `job.progress` agent_started (667-668) | db | temp_session_id update | emit_after_commit |
| cron/executor.py:711-729 | `job.finished` `cron:{run}:finished:{status}`; `agent.finished`; `turn.finished` | db | CronRun terminal update | Row stuck in running → emit_after_commit |
| cron/injector.py:251-268 | `job.progress` `cron:{run}:injected` | db | `_mark_injected` | emit_after_commit |
| cron/injector.py:271-304 | `bind(None)`; prepare; owner check; trace derived from `run.trace_context` with run_id=None, or context plus baseline plus a write of `run.trace_context` (295-298); `bind(trace)` | db (identity) | callback injection | identity |
| cron/injector.py:307-360 | `input.injected` `cron:{run}:input` (341-343); `message.committed` ×2 and `part.committed` ×2 through `record_projection_in_tx` (344-350); `job.progress` `cron:{run}:injected` **including message_id** (353-354) | db (`runtime.transaction`) | atomically consumes the result (2 messages, 2 parts, `injected`) | emit_after_commit. The same id carries different content at 267 (keep-first dedupe is safe). |
| cron/recovery.py:74-82 | `job.finished` unknown `cron:{run}:interrupted`; `recording.gap` process_restarted | db | bulk mark of interrupted runs | emit_after_commit |

### 1.4 Agent loop, processor, hooks, tools

| Site | Call → event(s) | Tx | Bound action | Failure today → target |
|---|---|---|---|---|
| agent/loop.py:330-333 | `get_run_trace`; `bind_trace(run_trace)` | read | run_loop | identity |
| agent/loop.py:491-498 | derives step_id; `step.started` (**R1**) | own tx | step start | Run aborts → emit plus an independent check |
| agent/loop.py:638-639, 664, 946-947 | step trace on `ToolContext`; `_trajectory_attempt`; `_trajectory_media_sources` (a sha256 → asset_id map, filled at 2172-2178) | none | none | identity and media references |
| agent/loop.py:1186-1199 | `step.finished` | own tx | after `process_step` | emit |
| agent/loop.py:1216-1219 | `request.retry_scheduled` | own tx | before the retry wait | emit |
| agent/loop.py:1473-1480 | `step.finished` for unfinished steps **before `finish_run`**, in `finally` | own tx | run teardown | A raise skips `finish_run` (only LookupError is caught, 1481), so the lease is held until it expires → emit |
| agent/processor.py:448-456, 1103-1104; 517-518; 1051 | `bind_trace` around save_part / update_message_info; `ctx.trace_context` taken from stream events | none | enables R2 | identity |
| agent/processor.py:812-826 | `tool.requested` plus `tool.finished` denied (conflicting call id) | own tx | blocked batch | emit |
| agent/processor.py:930-948, 1040 | `tool.requested` {tool, wire_tool, provider_call_id, arguments_raw, requested_arguments, schema, schema_source, request_schema_ref}; `record_rejection` → `tool.finished` denied | own tx | before the running part is persisted | emit |
| agent/hooks.py:64-86 | `context_for_tool` (may read the DB; raises ValueError on mismatch); derives call/part/message ids; `StreamTextRedactor`; `bind`; `tool.requested` (unless already recorded) | own tx | tool dispatch | emit |
| agent/hooks.py:87-104 | re-raises TrajectoryError (90-92); otherwise `tool.finished` waiting/cancelled/failed | own tx | exception path | emit |
| agent/hooks.py:105-120 | `tool.finished` with redacted model_output and metadata | own tx | result | emit |
| agent/hooks.py:166-173 | `_on_output` → `tool.output` executor_stream, **one commit per `update_output`** (**R1**) | own tx | incremental output | stream emit |
| agent/hooks.py:195-201 | `still_current(progress=True)`, then `tool.started` (**R1**) | own tx | immediately before `execute_fn` | emit plus check |
| agent/hooks.py:277-285 | `tool.output` executor_result (full output) | own tx | after execution | emit |
| tool/tool.py:164-176 | `tool.started` with validated arguments (**R1**); redactor | own tx | `define_tool` wrapper | emit |
| tool/tool.py:181-189 | `tool.output` executor_result (metadata stored raw, not through `public_value`) | own tx | after execute | emit |
| tool/bash.py:180-196 | `tool.output` per chunk beyond 100 000 chars (**R1**) | own tx | bash streaming | stream emit |
| tool/bash.py:198; web_fetch.py:96; mcp_tool.py:993, 1398, 1607-1612 | `_trajectory_full_tool_output` (mcp resource output built only when enabled) | none | none | carry in the emitted event |
| tool/batch.py:24-46, 58-60 | child context; `bind`; `tool.requested`; `tool.finished` denied | own tx | per invocation | emit |
| tool/task.py:56-67 | child_trace with run_id/generation/step/request/call/message/part = None (62-65); `agent.spawned` | own tx | before the child's message | emit |
| tool/task.py:71, 93-99, 128-138 | `bind(child_trace)`; `agent.finished` cancelled/failed; `agent.message`; `agent.finished` | own tx | child lifecycle | emit |
| tool/skill_tool.py:221-229 | `skill.loaded` with full content and effective_content | own tx | skill load | emit |
| agent/compaction.py:310-318 | `compaction.started` with `input: public_value(messages)` (**the full history**) | own tx | compaction start | emit |
| agent/compaction.py:441-497 | `compaction.finished` cancelled (447), failed (470), completed (492); `bind_trace` at 456, 467, 489 | own tx | compaction end | emit |
| agent/suggestions.py:112-114, 139-141 | `part.committed` through `record_projection_in_tx` | db | suggestions part | Already swallowed (173-176) → emit_after_commit |

### 1.5 LLM and service capture (details in §5)

| Site | Call | Bound action |
|---|---|---|
| agent/llm.py:1057-1059 | `RequestCapture.start` (Responses payload, provider_wire) | before `POST` (1060) |
| agent/llm.py:1065, 1090, 1142-1153, 1173, 1361, 1430, 1435 | `finish` (http failure, error, completed, cancelled); `route_changed`; `stream_chunks`; `capture_usage` | Responses stream |
| agent/llm.py:1463-1466, 1489-1491, 1499, 1510-1518 | billing event id; `trajectory_context` attached to events; `capture_billing` (also in the shielded settle) | `stream_llm` |
| agent/llm.py:1521-1557 | `metered_completion`: start (payload=kwargs, adapter_input), `chunk`, `capture_usage`, `finish`, `capture_billing` | title (loop.py:2520), bash judge (bash.py:90) |
| agent/llm.py:1755-1757, 1762-1770, 1833-1836, 1869-1875 | LiteLLM: start (payload=call_kwargs), `stream_chunks`, usage, `finish` | chat, compaction, cron summary, suggestions |
| tool/video_analyze.py:214-253 | UsageMeter; start (frame image_url and transcript); `chunk`; usage; `finish`; `capture_billing` | vision call |
| tool/video_analyze.py:367-391 | `register_owned_media_inputs`, `retain_derived_media_inputs`, `service_scope` (only when enabled, 373) | analysis job |
| tool/image_gen.py:357-368 | `capture_asset_ids_in_tx(prepared=input bytes)`, then commit | before the provider call |
| tool/image_gen.py:375-386, 400-423 | start (public params plus input refs); ctx stash; `chunk` (image blocks with sha256 and size); usage; `finish` | images generate/edit |
| tool/image_gen.py:952-969 | reads `UsageEvent`, then `capture_billing` | after `settle_image` |
| tool/video_production.py:955-976; 1031-1058; 1089; 1113-1123 | `capture_service_dispatch` and `capture_http_response`; `observe_service_response` for transcription status/result | video submit, STT |
| tool/video_production.py:999-1000, 1020-1022 | `observe_service_response` video_status / video_cancel | polls |
| tool/video_production.py:2331-2337, 2526-2528, 2606-2615, 2835-2837, 2917-2919 | `service_scope(ctx, job, asset_urls?)` | submit, cancel, status, STT |
| tool/video_providers.py:951-964, 997-998 | gateway submit capture; status observe | gateway |
| video/ims_client.py:94-106 | IMS submit capture plus `capture.chunk`; other actions observed | IMS |
| tool/video_compose.py:266-275, 343-346; video/job_recovery.py:206-209, 268-278 | register media plus `service_scope`; recovery `ToolContext` rebuilt from `request_data._trajectory_context` | compose and recovery |

### 1.6 Files, artifacts, jobs

| Site | Call → event(s) | Tx | Bound action | Failure today → target |
|---|---|---|---|---|
| tool/write.py:34-52, 68-72 | `captures_files` triggers an **extra sandbox read** of the before-content; `record_file_change` → `artifact.recorded` file_diff (full before/after text plus diff; files.py:15-42); re-read after formatting | own tx | after `write_file` | Tool error → emit |
| tool/edit.py:401-402, 418-421; tool/multiedit.py:81-82, 98-101 | same (edit / multiedit and format) | own tx | after write | emit |
| tool/apply_patch.py:54-99 | before-read for deletes (72-76); record per op (97) | own tx | per file | emit |
| sandbox/assets.py:192-218 | `read_asset_bytes` **OSS GET** when enabled; `capture_result_asset_in_tx` in the FileAsset insert transaction, then commit | db | view_image, share_file, screenshots | Asset row lost after upload → emit_after_commit reference |
| tool/douyin_publish.py:130-133 | capture with `content=png` | db | QR asset | emit_after_commit reference |
| tool/image_gen.py:509-521 | capture with `content=data` and request_id; **any exception deletes the OSS object** (516-521) | db | generated image | emit_after_commit reference |
| tool/image_gen.py:683-708 | `read_asset_bytes` of the copied object when enabled; capture | db | cache reuse | emit_after_commit reference |
| api/assets.py:277-281 | `read_asset_bytes(row)` **OSS GET of the upload itself (can be up to `_MAX_SIZE`)**, only when enabled and session-bound | none | upload completion | remove |
| api/assets.py:289-294, 431-435 | `revoke_asset_in_tx` → payload tombstones plus `artifact.removed` | db | oversize tombstone / asset delete | emit_after_commit (retention-critical) |
| api/assets.py:302-307 | `activity_context` plus `capture_asset_in_tx(role=input, content)` | db | status ready | emit_after_commit reference |
| tool/video_production.py:640-645, 706-711 | `record_job_in_tx(submitted=True)`, then commit | db | VideoJob insert | emit_after_commit |
| tool/video_production.py:750-765 | `_update_job`: `FOR UPDATE`, keeps `_trajectory_context`, `record_job_in_tx` | db | every job state change | emit_after_commit |
| tool/video_production.py:768-801 | `enabled()` with no user; **OSS GET of the finished video** (786-787); record_job + `capture_asset_in_tx` | db | FileAsset ready | emit_after_commit reference |
| video/job_recovery.py:253-256 | `record_job_in_tx` | db | stale finalization reclaim | emit_after_commit |
| publish/desktop_service.py:166-167, 187-188 | `record_job_in_tx` (submitted / finish) | db | PublishJob | emit_after_commit |
| platforms/service.py:589-594, 611-612, 676-677 | `record_job_in_tx` (submitted with session from `current()`, expired, published webhook) | db | PublishJob | emit_after_commit |

`trajectory/jobs.py` behaviour:
- Skips when recording is disabled or the source/root session is deleted (16-29).
- On first contact it sets `CONTEXT_KEY` on the job (33-34) and, for a job that was not just submitted, emits `baseline.captured` with id `job_adopt:{id}` (35-40).
- The event id is `job:{id}:{sha256(data)[:24]}` (57).
- For a terminal job whose run has changed, it emits `operation.late_result` (58-65), reading `SessionExecution` to decide.

### 1.7 Imports that are not producers

| Site | Use |
|---|---|
| main.py:79-90, 249 | `_shutdown_trajectory`: `stop_exports`, `flush()`, `stop_archive_worker` |
| main.py:107-117 | archive worker (payload drain to blob storage, checkpoints, purge; payload.py:238-260); log line; `resume_exports` |
| main.py:332-335 | admin trajectory HTTP and WS routers |
| api/admin_trajectories.py:9-17, 22-39 | read API, error mapping, audit |
| api/admin_trajectory_ws.py:16-17, 125-131, 159 | subscribes to bus `trajectory.available`; audits via `PgAuditRepo` |
| api/ws.py:247-248 | the user WS drops `trajectory.*` events |
| scripts/trajectory_dev_server.py:33-36, 61-121 | writes fixture events with `append_events_in_tx` into SQLite; `set_storage` |
| db/base.py:119, 126-135, 334-340 | adds desktop `trace_context` columns; readiness probe requires the trajectory tables |

### 1.8 Where identity is persisted (business-side writes)

| Carrier | Written | Read |
|---|---|---|
| `SessionExecution.trace_context` | session.py:724; runtime.py:80; question.py:133-135 | session.py:520-527; runtime.py:48-55, 93-97, 251-254, 308-311; question.py:117 |
| `CronRun.trace_context` | executor.py:643; injector.py:298 | executor.py:714-720; injector.py:265-268, 288-293; recovery.py:76-77 |
| `QuestionCheckpoint.continuation.trace_context` | question.py:132 | question.py:115 |
| `VideoJob.request_data` / `PublishJob.details` key `_trajectory_context` | jobs.py:33-34 | jobs.py:20; video_production.py:759-762; job_recovery.py:273-278 |
| `VideoJob.request_data._trajectory_request_ids` | agent/trajectory.py:294-297 | jobs.py:48; agent/trajectory.py:256-258 |
| Redis `perm_req:{id}`; `PendingPermission.trace_context` | permission.py:322, 333 | permission.py:229-241, 397 |
| `ToolContext.trace_context` and `_trajectory_*` attributes | hooks.py:73-77; loop.py:638-664, 946; processor.py:518, 1051; batch.py:39; agent/trajectory.py:501-503; llm.py:1465-1466, 1528-1529; image_gen.py:384-386, 800-801 | throughout |
| transient FileAsset rows (**created only when recording is enabled**) | agent/trajectory.py:168-176 | business asset queries |

---

## 2. `record` name collisions

| Module | Import | `record` resolves to | Risk |
|---|---|---|---|
| api/workspaces.py:10, admin_skills.py:25, admin_skill_catalog.py:9, admin_billing.py:14, admin_skill_desktops.py:11, admin.py:5, admin_messages.py:12, admin_push.py:8, platform_accounts.py:12 | module-level `from audit import record` | `audit.record` (audit/__init__.py:12-41, swallows its own errors) | These modules have no trajectory references |
| api/admin_fleet.py:8 | module-level | `audit.record` (calls 145-413) | `get_diag` assigns a local `record = await diag.get(...)` (323-326), which shadows the import inside that function |
| api/metadata.py:420, 444; auth/routes.py:161, 199, 357 | function-local | `audit.record` | none |
| sandbox/pool.py:118 | local import inside `_audit` | `audit.record` | Other functions use a local dict named `record` (399-689) and call `_audit`, not `record` |
| sandbox/wuying_desktop_service.py:147, 249, 270 | `as audit_record` | `audit.record` | The same functions hold a dict named `record` (139-160, 240-282); the alias is what prevents the clash |
| api/admin_trajectories.py:9 | `as audit_record` | `audit.record` | The module also imports `get_record` (15) and defines `records` / `record_detail` (79, 90) |
| api/admin_trajectory_ws.py:125-131 | `PgAuditRepo().create` | none | none |
| trajectory `record` aliases | `record as record_trace`: loop.py:330, processor.py:448, compaction.py:310. Plain function-local `record` everywhere else. | `trajectory.recorder.record` | **agent/hooks.py has no `record_trace` symbol**; it imports `record` (65, 168, 197) |
| publish/desktop_service.py:191 | parameter `record: dict` | local dict | The module only imports `record_job_in_tx` |

No scope binds both `audit.record` and the trajectory `record`. A module-level `from trajectory import record` (or `emit` under that name) would collide in every audit-importing module listed above.

---

## 3. Handlers that reference the trajectory error classes

**Fences that the try bodies can reach (both apply only when recording is enabled for the user):**
- **R1** (recorder.py:182-187): `OwnershipError` ("Execution lease is stale") on `request.started`, `request.delta`, `tool.started`, `tool.output`, `step.started` when the event carries run_id and generation and `SessionExecution.run_id` or `run_generation` differ. Purposes title and suggestions are exempt when run_id is None and the generation matches (185).
- **R2** (session.py:510-516): `TrajectoryError` ("Superseded execution cannot update the chat projection") when the bound context has run_id and generation and `execution.generation` differs, or `execution.run_id` is set to another run.

Classes: **F** = fencing, **R** = recording failure propagation (fail-closed), **O** = other.

| Site | Try body (what it can reach) | Class | Evidence |
|---|---|---|---|
| agent/hooks.py:90-92 | tool execution: R1 `tool.started` / `tool.output`, R2 via `save_part` | F+R | skips `tool.finished` and re-raises |
| agent/hooks.py:216-218 | `execute_fn` | F+R | prevents the error `ToolResult` (252-262) |
| agent/processor.py:1147-1149 | `stream_llm` (R1), `persist_part` (R2), `wrap_execute` | F+R | otherwise becomes RETRY or ERROR (1170-1185) |
| agent/processor.py:1166-1168 | partial text `save_part` (R2) | F+R | otherwise logged |
| agent/loop.py:403-405 | `update_session` model fallback (R2) | F+R | otherwise debug log |
| agent/loop.py:1310-1312, 1416-1418, 1441-1443 | `save_part` PatchPart / `update_part_data` / `prune_tool_outputs` (R2) | F+R | otherwise warning |
| agent/loop.py:1371-1373 | `flush_pending_cron_results` (injector context has run_id None) | R | otherwise debug |
| agent/loop.py:1432-1434 | `settle_running_todos` → `todo.changed` | R | otherwise warning |
| agent/loop.py:1457-1459 | whole run | F+R | skips SESSION_ERROR and ERROR status (1460-1467) |
| agent/loop.py:2482-2484, 2546-2548 | title `metered_completion` (R1 exempts "title") | R | otherwise truncation fallback |
| agent/loop.py:2496-2498 | `set_session_title` → settings_changed (R2; task at 508 inherits ContextVars) | F+R | otherwise warning |
| agent/compaction.py:193-195, 649-651 | `save_part` / `update_part_data` (R2) | F+R | otherwise error log |
| agent/compaction.py:271-273, 442-444 | `stream_llm` (R1) | F+R | otherwise fallback summary / `llm_error` |
| agent/llm.py:1412-1414, 1866-1868 | provider streams (R1) | F+R | otherwise yields an `error` event |
| agent/trajectory.py:108-114 | `_recording_operation` wrapper | R | → `RecordingError("Provider input could not be retained")` |
| agent/trajectory.py:484-495 | media retention plus prepared/started | R (also F by event type) | → `RecordingError("Request input could not be recorded")` |
| agent/trajectory.py:524-530, 623-629 | stream redactor redact / finalize | R | → `RecordingError` |
| session/revert.py:126-128, 156-158 | `_begin_restore` / `_finish_restore` | R | otherwise returns False |
| cron/executor.py:195-198 | whole job including `run_loop` | R (F nested) | comment at 196-197: never turn an outage into a summary or webhook |
| cron/executor.py:311-313, 367-369 | summary generation `stream_llm` | R | otherwise "" |
| cron/injector.py:117-119, 159-161, 195-197 | injection, mark, pre-compaction | R (F nested) | otherwise error log / False |
| tool/batch.py:73-75; 81-84 (isinstance on gather results) | `wrap_execute` | F+R | otherwise "[tool] Error:" strings |
| tool/task.py:170-172 | `update_part_data` (R2) | F+R | "never fail the task over a progress pointer" |
| tool/bash.py:106-107 | judge `metered_completion` (R1) | F+R | otherwise "kill" |
| tool/bash.py:210-211 | streaming: `update_output` → `tool.output` (R1) | F+R | otherwise **re-executes the command without streaming** (212-217) |
| tool/mcp_tool.py:1034-1035, 1445-1446, 1619-1620 | MCP call / write / read; no direct recording | R (defensive) | otherwise `_mcp_failure_result` |
| tool/computer.py:526-527, 655-656, 680-682, 709-710 | `_attach_screenshot` → `attach_sandbox_image` (capture plus `save_part` R2); locked body; lease | F+R | otherwise ToolResult |
| tool/computer.py:476-477, 569-570 | `ensure_browser`, `_prepare` (no direct recording) | R (defensive) | none |
| tool/desktop_takeover.py:82-83, 99-100 | `urlsplit`, `browser_status` (unreachable) | R (defensive) | none |
| tool/share_file.py:74-75; tool/view_image.py:71-72 | `attach_sandbox_image` (capture plus R2) | F+R | otherwise "Upload failed" |
| tool/douyin_publish.py:241-243, 309-311 | `_pin_png` (capture plus R2) | F+R | otherwise warning |
| tool/apply_patch.py:98-99 | `record_file_change` | R | otherwise a per-file error |
| tool/image_gen.py:421-424 | `if not isinstance(exc, TrajectoryError)` | R | skips `capture.finish("failed")`, leaving the request unfinished |
| tool/image_gen.py:572-574, 751-753 | `save_part` FilePart (R2) | F+R | otherwise attached=False |
| tool/image_gen.py:924-927 | whole generate (R1, asset capture, `_store_output`) | F+R | otherwise ToolResult failed |
| tool/video_analyze.py:399-400 | `service_scope` plus `_transcribe` (R1) | F+R | otherwise transcript error |
| tool/video_analyze.py:420-421 | full analysis (`OwnershipError` from media registration, R1, job events) | F+R+O | otherwise the job is marked failed |
| tool/video_compose.py:276-277 | register media, `service_scope`, IMS submit (R1, `OwnershipError`) | F+R+O | otherwise job failed |
| tool/video_compose.py:319-320 | `poll_compose_job` (no run) | R | none |
| tool/video_compose.py:180-181, 212-213, 237-238, 251-252, 354-355, 367-368 | resolve / budget / compile / guard / head / settle (no direct recording) | R (defensive) | none |
| tool/video_production.py:894-895 | attach `save_part` (R2) | F+R | otherwise un-claim |
| tool/video_production.py:2407-2408 | submit flow: `update_output` (R1), dispatch (R1), job and asset events | F+R | otherwise job failed |
| tool/video_production.py:2529-2530, 2582-2583, 2655-2656 | cancel / finalize wait / status (`service_scope`, job events) | R | none |
| tool/video_production.py:2870-2871, 2927-2928 | STT submit / retry (`service_scope` `OwnershipError`, R1) | F+R+O | none |
| tool/video_production.py:1259-1260, 1288-1289, 1508-1509, 1565-1566, 1733-1734, 1757-1758, 1911-1912, 1987-1988, 2013-2014, 2090-2091, 2500-2501, 2790-2791, 2848-2849 | OSS, config, validation, settlement (no direct recording) | R (defensive) | none |
| video/job_recovery.py:144-146; video/compose_recovery.py:50-52 | background sweeps | R | **aborts the whole sweep** |
| video/ims_client.py:108-110 | dispatch (R1 when a run is bound) and observe | F+R | otherwise `ImsError` |
| api/admin_trajectories.py:31-34; 131, 193, 200 | read API | O | `CorruptContent` → 409, `TrajectoryError` → 400; download integrity raises |

**Raise sites outside `trajectory/`:**
- F: session.py:516.
- R: agent/trajectory.py:114, 495, 530, 629.
- O: agent/trajectory.py:160, 165, 204, 211, 243, 293. These are `OwnershipError` media and job-ownership checks that **block dispatch, and only when recording is enabled**.

**Plain `ValueError`s raised on identity mismatch** (not TrajectoryError, so the handlers above treat them as ordinary errors): session.py:508, 526; agent/trajectory.py:409; trajectory/producers.py:18, 22. `StreamTextRedactor` raises `ValueError` (stream_redaction.py:285, 289). In hooks/tool/bash that surfaces as a tool error (hooks.py:252-262), or escapes `wrap_execute` when it happens at 110-111.

**Handlers that catch trajectory errors implicitly:**

| Site | Handler | Effect today |
|---|---|---|
| question/continuation.py:237-247 | `except ValueError` around `apply_answers` | A recording or fence error becomes permanent `QUESTION_RESUME_FAILED` instead of the retry path (248-252) |
| api/questions.py:31-32 | `except ValueError` | reply / reject / draft recording failure → HTTP 422 |
| agent/processor.py:1122-1131 | `except ValueError` around `get_agent` + `update_session` | swallows R2 or recording errors, logged as "Unknown agent" |
| cron/timer.py:368-369; cron/service.py:345-346 | `except Exception` | the executor's re-raise becomes an error job result (retry / backoff) |
| api/sessions.py:1089-1091; question/continuation.py:256-259 | `except Exception` around `run_loop` | logged only |
| api/sessions.py:376 (sync prompt); api/sessions.py:125 | no handler | HTTP 500 |
| agent/suggestions.py:173-176, 181-184 | `except Exception` | swallowed |

---

## 4. Event types produced outside `trajectory/`

| Event | Direct producers | Produced via trajectory helpers called by business code |
|---|---|---|
| trajectory.started | none | recorder.py:87-94 (any first append) |
| baseline.captured | question.py:128 | `ensure_trajectory_in_tx(baseline)` ← session.py:637 (callers session.py:691, revert.py:34, fork.py:176/191, executor.py:632, injector.py:296); jobs.py:36 |
| input.accepted | session.py:727 | none |
| input.injected | session.py:727 (synthetic); injector.py:341; continuation.py:130 | none |
| session.settings_changed | session.py:400; continuation.py:141 | none |
| history.reverted | revert.py:39, 48; session.py:1159 | none |
| history.regenerated | session.py:1079 | none |
| history.forked | fork.py:187, 193 | none |
| turn.started | session.py:726; executor.py:647; runtime.py:76 | none |
| turn.finished | executor.py:729; runtime.py:255 | none |
| run.started | runtime.py:81 | none |
| run.finished | runtime.py:99 via 249 | none |
| run.interrupted | runtime.py:99 via 289, 335 | none |
| run.cancel_requested | runtime.py:310 | none |
| step.started | loop.py:495 | none |
| step.finished | loop.py:1192, 1474 | none |
| request.prepared, request.started | agent/trajectory.py:467, 477 (`RequestCapture.start` ← llm.py:1057, 1533, 1755; video_analyze.py:224; image_gen.py:380; `capture_service_dispatch` ← video_production.py:962, 1036, 1116; video_providers.py:955; ims_client.py:97) | none |
| request.delta | agent/trajectory.py:536 (`chunk` ← llm.py:1536; video_analyze.py:227; image_gen.py:412; ims_client.py:102; agent/trajectory.py:353, 356), 570-573 (stream ← llm.py:1142, 1762), 633 (redaction finalize) | none |
| request.usage | agent/trajectory.py:612 (llm.py:1152, 1539, 1769, 1834; video_analyze.py:230; image_gen.py:415), 424 (billing ← llm.py:1499, 1512, 1550; video_analyze.py:244; image_gen.py:967) | none |
| request.finished | agent/trajectory.py:637 (llm.py:1065, 1173, 1361, 1430, 1435, 1540, 1544, 1836, 1870, 1875; video_analyze.py:232, 236; image_gen.py:416, 419, 423; agent/trajectory.py:341, 344) | none |
| request.retry_scheduled | loop.py:1216 | none |
| request.route_changed | agent/trajectory.py:652 ← llm.py:1090 | none |
| tool.requested | hooks.py:82; processor.py:820, 936; batch.py:42 | none |
| tool.started | hooks.py:200; tool.py:167 | none |
| tool.output | hooks.py:170, 282; tool.py:185; bash.py:193 | none |
| tool.finished | hooks.py:96, 112; processor.py:824, 946; batch.py:59; continuation.py:123 | none |
| permission.requested / resolved / expired | permission.py:70 (via 87, 88, 321, 352, 398, 437, 450) | none |
| question.asked / draft_saved / resolved / cancelled | question.py:144 (via 240, 346, 311); runtime.py:278; continuation.py:182 | none |
| takeover.requested / finished | question.py:155 | none |
| agent.spawned | executor.py:664; task.py:66 | none |
| agent.message | task.py:128 | none |
| agent.finished | executor.py:727; task.py:95, 135 | none |
| compaction.started / finished | compaction.py:316 / 447, 470, 492 | none |
| context.replaced | session.py:851 | none |
| message.committed | session.py:731, 800, 848; injector.py:346; continuation.py:138 | none |
| part.committed | session.py:734, 921, 1214; injector.py:349; continuation.py:122; question.py:248; suggestions.py:113, 140 | none |
| plan.changed | session.py:589 | none |
| todo.changed | storage.py:95 | none |
| skill.loaded | skill_tool.py:225 | none |
| job.submitted | executor.py:648 | jobs.py:41/57 ← video_production.py:643, 710; desktop_service.py:167; platforms/service.py:592 |
| job.progress | executor.py:667; injector.py:267, 353; agent/trajectory.py:298, 369 (← video_production.py:1000, 1021, 1058, 1089; video_providers.py:998; ims_client.py:106) | jobs.py ← video_production.py:765, 800; job_recovery.py:256; desktop_service.py:188; platforms/service.py:612, 677 |
| job.finished | executor.py:722; recovery.py:78 | jobs.py (terminal statuses, same callers) |
| operation.late_result | none | jobs.py:62-65 |
| artifact.recorded | none | files.py:35 ← write.py:52, 72; edit.py:402, 421; multiedit.py:82, 101; apply_patch.py:97. artifacts.py:103 ← `capture_asset_in_tx` (api/assets.py:306; video_production.py:801; agent/trajectory.py:177), `capture_asset_ids_in_tx` (session.py:584, 635; image_gen.py:364; agent/trajectory.py:240), `capture_result_asset_in_tx` (sandbox/assets.py:217; image_gen.py:511, 707; douyin_publish.py:132) |
| artifact.removed | none | artifacts.py:234 ← api/assets.py:293, 434 |
| recording.gap | recovery.py:81 | recorder (paused 234, resumed 83, stream recovered 192, disabled stream 395) ← `mark_capture_paused_in_tx` callers revert.py:26, session.py:592, 738, fork.py:197, plus any `record()` while disabled |

Declared but never produced: `context.injected`, `tool_catalog.changed`, `takeover.started` (types.py:23, 25, 27).

---

## 5. Request capture (`backend/agent/trajectory.py`)

### 5.1 What is captured per call

**`RequestCapture.__init__`** (433-451)
- Derives the request context: `request_id=ascending("request")`, `call_id=None`, `parent_call_id` from the current call, `message_id` from ctx, `part_id=None`.
- Starts monotonic timers and a `CaptureStreamRedactor`.

**`start`** (453-505)
1. `context_for_tool` (404-416) uses the bound context; otherwise it opens a DB transaction for `context_for_session`. A mismatch raises `ValueError`.
2. `request_snapshot(payload)` (63-73) keeps only `REQUEST_FIELDS` (20-27), passing each through `public_value`. `extra_body` is filtered the same way. Every other kwarg name (api_key, api_base, headers, timeout …) is listed in `omitted_fields`.
3. `public_value` (37-60):
   - calls `model_dump(mode="json")`
   - applies schema-aware `sanitize` under schema keys
   - drops keys that start with `_` and keys in `PRIVATE_FIELDS` (28-34)
   - replaces unknown objects with `{"availability": "not_recorded"}`
   - does **not** strip base64
4. When `ctx._trajectory_media_urls` is set (service scope), `_service_body` rewrites owned media URLs to `trajectory-media:<payload_id|asset_id>` (266-281, including JSON inside string values for IMS) and adds a `media_inputs` manifest (461-465).
5. When enabled, **one DB transaction** runs `ensure_trajectory_in_tx`, then `retain_request_media_in_tx(snapshot, source_asset_ids=ctx._trajectory_media_sources)`, then `persist(db)` (485-491).
6. `persist` writes two events:
   - `request.prepared` with id `request:{id}:prepared` (467-476): purpose, model, provider, capture_level, **input snapshot**, attempt (`ctx._trajectory_attempt`), `previous_request_id` (retry chain, when call_id is None), `parent_request_id` (tool-side call), `sdk_internal_attempts`, `billing_usage_event_id`
   - `request.started` with id `request:{id}:started` (477-480): purpose, model, capture_level, timing_source. This is where R1 applies.
7. Side effects on ctx:
   - When the call is not inside a tool call, `ctx.trace_context = capture.context` (500-501). Later tool calls derive from it (processor.py:930-933).
   - `ctx._trajectory_request_schemas` is built from the snapshot's tools (502).
   - `ctx._trajectory_active_request` is set (503); `stream_llm` uses it to tag events and for billing (llm.py:1489-1491, 1499).

**Payload per entry point**

| Entry point | `payload` passed | capture_level |
|---|---|---|
| `_stream_litellm_direct` (llm.py:1729-1756) | `call_kwargs`: model; messages (system joined, full history, tool results, images as `image_url` **data URIs** via `_finalize_message` 1629-1647); tools; stream options; max_tokens; provider kwargs (listed as omitted); thinking/reasoning; extra_body; tool_choice | adapter_input |
| `_stream_responses_api` (llm.py:~957, 1057) | Responses payload: `input` items (images as `input_image` data URIs, 144-150), instructions, tools, reasoning, tool_choice | provider_wire |
| `metered_completion` (llm.py:1533) | raw kwargs (title prompt, bash judge prompt …) | adapter_input |
| video_analyze `_complete` (222-225) | messages with frame `image_url` (rewritten through retained media) and the transcript | adapter_input |
| image_gen (375-383) | public params plus input references {asset_id, name, media_type, sha256, size_bytes, payload ref}; no base64 | adapter_input |
| `capture_service_dispatch` (303-333) | `{"model", "input": {operation, business_body (SERVICE_FIELDS allowlist per profile, 119-126), omitted_fields, media_inputs, media_url_representation, job_id}}` | provider_wire (IMS uses adapter_input) |

**`capture_service_dispatch`, continued**
- When there is no scope it falls back to `current_tool_context()` or `current()` (310-319).
- It sets `dispatch_ctx._trajectory_billing_event_id = None` (324-327).
- `_link_service_job` (284-300) opens its own transaction, takes `VideoJob FOR UPDATE`, appends the request_id to `request_data._trajectory_request_ids` (a business write), and emits `job.progress` provider_dispatch with `db=`.
- On exit: `finish("cancelled"|"failed")` on error, `finish("completed", reason="accepted"|"stop")` on success (340-344).

**Per chunk**
- `chunk_data` (507-530): chunk_index, mode delta, blocks (`litellm_chunk_blocks` 658-674 for text / reasoning / tool_arguments; `responses_chunk_blocks` 677-704), purpose, `raw = provider_output_snapshot(chunk)` (76-90 field allowlist), elapsed_ms. Then redacted; a redaction failure becomes `RecordingError`.
- `chunk` (532-537): awaited `request.delta` with id `request:{id}:chunk:{n}`. Used for non-streaming responses and HTTP bodies. `capture_http_response` (347-357) records the parsed body, or the non-JSON body text.
- `stream_chunks` (539-603):
  - A reader task reads ahead from the provider (queue maxsize 32, byte bound `TRAJECTORY_PENDING_BYTES`, 553-569) and calls `record_stream` per chunk (570-573).
  - The consumer **awaits each chunk's receipt before yielding that chunk** (591-595), so commit latency and back-pressure gate token delivery.
  - A failed receipt ends the stream (R1 is enforced per committed batch).
  - Reader errors are forwarded (579-589). `finally` cancels the reader and closes the stream (596-603).

**Usage, billing, finish**
- `capture_usage` (605-615): `request.usage` (replace, provider_normalized) with id `request:{id}:usage:{n}`.
- `capture_billing` (419-429): `request.usage` with ledger credits and `billing_usage_event_id`, id `request:{id}:billing`.
- `finish` (617-647): finalizes the redactor; optionally writes `request.delta` with `source: recorder_redaction` (633-636); writes `request.finished` with status, finish_reason, error type and message, usage, chunk_count, duration, ttft, first_text, generation_ms. A `finished` flag makes it idempotent.
- `finish` is awaited inside provider `finally` blocks (llm.py:1434-1435, 1874-1875); a recording error there masks the original exception or cancellation.
- `route_changed` (649-655).

**Media helpers**
- `register_owned_media_inputs` (129-179), enabled only:
  - Checks host == `oss.host` and a key prefix of `assets/{user}/` or `analysis/{user}/`; otherwise `OwnershipError`.
  - Looks up the FileAsset in its own DB transaction (161-163); deleted or not ready → `OwnershipError`.
  - If there is no asset, it **downloads the object** (167), **inserts a transient FileAsset** (168-176) and runs `capture_asset_in_tx` role=input (177).
- `retain_derived_media_inputs` (182-221): checks source ownership (201-204); **downloads each staged frame/audio** (212), **base64-encodes it** (215), then `retain_request_media_in_tx` stores the bytes under the source asset id (216-219).
- `_service_inputs` / `service_scope` (224-263):
  - `prepare_asset_ids` downloads bytes unless that version is already retained (237-238; artifacts.py:54-76).
  - `capture_asset_ids_in_tx` in its own transaction (239-240).
  - Any unavailable asset → `OwnershipError` (241-243).
  - Sets the ContextVars `_service_scope` and `_service_request`; the request id is taken from the job's `_trajectory_request_ids` (255-258).
- `observe_service_response` (360-370): `job.progress` with `provider_response = public_value(body)` for polls, cancels and downloads.

### 5.2 Where base64 media or OSS media bytes appear

| Site | What |
|---|---|
| agent/loop.py:2140-2156 | OSS presigned GET → `data:<mime>;base64,…` into `_images` / `_IMAGE_CACHE` (a business need) |
| agent/loop.py:2172-2178 | base64-decodes again to compute sha256 → `ctx._trajectory_media_sources` (recording only) |
| agent/llm.py:144-150, 1639-1646 | data URIs embedded in the provider payload, therefore in the `request_snapshot` input |
| trajectory/artifacts.py:150-217 (called at agent/trajectory.py:488) | `retain_request_media_in_tx`: decodes `data:` URIs, `{"type":"base64"}` blocks and `input_audio.data`; hashes; checks payload/asset rows; `store_bytes` → **bytes in `trajectory_payloads.content`** (payload.py:74-95). Dedupe is per (trajectory, sha, media_type, source_asset_id) (payload.py:79-85). Replaced by `{"$media": ref, source_kind asset\|inline_non_asset\|ambiguous_asset, original_encoding, declared_media_type, source_asset_id}` |
| agent/trajectory.py:167, 212-215, 237-238 | OSS downloads for registration, derived frames, service inputs |
| tool/image_gen.py:222-238, 360-368 | input images downloaded (business need), then stored as bytes by the capture (prepared) |
| tool/image_gen.py:510-514, 683-687 | generated image bytes stored; reuse path re-downloads |
| sandbox/assets.py:192-196, 217 | re-downloads the just-uploaded sandbox file, then stores it |
| tool/douyin_publish.py:132 | QR PNG bytes stored |
| api/assets.py:277-281, 306 | downloads the user upload (can be up to `_MAX_SIZE`), then stores it |
| tool/video_production.py:786-787, 801 | downloads the finished video, then stores it |
| session/session.py:561 (via artifacts.py:76) | downloads attachment and baseline file bytes, then stores them |
| recorder.py:150-151 | any event data over 64 KiB (for example `request.prepared` with the full prompt on **every request**) re-stored as a JSON payload; dedupe only for whole-blob identity |

### 5.3 DB, OSS and blob I/O in the business process, and awaited receipts

- **Tables written:**
  - `session_trajectories`: upsert, lock, counters (recorder.py:70-78, 117-143)
  - `trajectory_events` (153-159)
  - `trajectory_payloads`: staged content (payload.py:88-94)
  - `trajectory_records` including `search_text` (repository.py:113-126)
  - `trajectory_session_summaries` (repository.py:100-142)
  - payload tombstones (payload.py:190-196; lifecycle.py:21-25)
  - plus the identity carriers in §1.8
- **Locks:** `SessionTrajectory` on every append; `Session FOR UPDATE` in todo writes (storage.py:75-76); `VideoJob FOR UPDATE` in `_link_service_job` (291); the session lock taken only when enabled in cron (executor.py:626-629).
- **Reads:**
  - `SessionExecution` per fenced event (recorder.py:184) and Session ancestry per event (43-63)
  - idempotency lookup (131); payload dedupe (payload.py:79; artifacts.py:30-51, 161-165); FileAsset (artifacts.py:114, 178)
  - full history for baselines (session.py:609-616); trajectory state in fork (fork.py:179-181) and baseline checks (session.py:551-555, 603-608)
  - `TrajectoryEvent` in permission (permission.py:235); `UsageEvent` (image_gen.py:959-965)
- **OSS:** `read_asset_bytes` presigned GET (artifacts.py:18-25) at every site in §5.2.
- **Blob storage** (local/gcs/azure; payload.py:25-47, not Aliyun OSS): archive upload, download, verify and delete run in the API process (payload.py:199-260; main.py:107-108).
- **Awaited receipts:**
  - every own-tx `record` (and it first drains that session's stream queue, recorder.py:221)
  - every `db=` append (SQL round trips coupled to the business commit)
  - per-chunk receipts in `stream_chunks` (591)
  - `flush()` at shutdown (main.py:88)
  - the provider call only starts after the `start` transaction commits (485-491)

---

## Migration notes

### A. Replacement per producer

| Producer group | Sites | Replacement | Ordering / atomicity |
|---|---|---|---|
| Chat projection | session.py:400, 570-593 (callers 726-735, 800, 848-853, 921, 1079, 1159, 1214); suggestions.py:113, 140; injector.py:341-354; continuation.py:122-143; question.py:128-159, 248 | emit_after_commit on `db.sync_session` | **Atomicity matters**: a fact must exist only if the commit happened. Per-session order is serialized by `session_exposure_lock` plus `lock_owned_session` (runtime.py:106-113), so enqueue order equals commit order only within one process. |
| Run / turn lifecycle | runtime.py:76-86, 99-103, 255, 310 | emit_after_commit | Causal order input → turn.started → run.started → step / request → run.finished must survive merges across processes |
| Question | question.py; continuation.py:182; runtime.py:278 | emit_after_commit | id `{type}:{id}:{rev}` |
| Permission | permission.py:59-91, 321-453 | emit (no business transaction) | `_read_recorded_reply` needs a business-side durable reply (for example Redis SETEX `perm_reply:{id}` before publish, or a DB row) |
| Cron | executor.py:647-652, 664-668, 722-729; injector.py:267, 341-354; recovery.py:78-82 | emit_after_commit | idempotent `cron:{run}:*` ids |
| Jobs | jobs.py callers (§1.6) | emit_after_commit. Keep `_trajectory_context` in business rows but drop the `enabled` gating. | content-digest ids with keep-first dedupe; `operation.late_result` needs a `SessionExecution` read (jobs.py:58-65) |
| File assets | sandbox/assets.py:217; image_gen.py:511, 707; douyin_publish.py:132; api/assets.py:306; video_production.py:801; agent/trajectory.py:177; session.py:584, 635 | emit_after_commit with a **reference** {asset_id, oss_key, mime, size}; no downloads | Revokes (api/assets.py:293, 434) are retention-critical and need guaranteed delivery or reconciliation |
| File diffs | files.py ← write / edit / multiedit / apply_patch | emit (the worker externalizes large text) | Extra sandbox reads (write.py:36-42; apply_patch.py:72-76; format re-reads) should stay behind a cheap `enabled()` gate |
| Todo | storage.py:95 | emit_after_commit | drop the `FOR UPDATE` (75-76) |
| Steps, tools, agents, skills, compaction | loop.py:495, 1192, 1216, 1474; processor.py:820-826, 936-948; hooks.py:82-119, 200, 282; tool.py:167, 185; batch.py:42, 59; task.py:66-138; skill_tool.py:225; compaction.py:316-497 | emit | producer FIFO order within a coroutine; the fences (B) move out |
| Tool output streams | hooks.py:166-173; bash.py:180-196 | stream emit | `chunk_index` is assigned by the producer (hooks.py:171) |
| Request capture | agent/trajectory.py:432-655 | emit for prepared / started / usage / finished / route_changed; stream emit for `request.delta` | Remove the commit-before-dispatch wait (485-491) and the per-chunk receipt wait (591). Replace data URIs with `{asset_id, sha256}` from `_trajectory_media_sources` before enqueue. |
| Revert / fork | revert.py:39, 48; fork.py:187-194 | revert: emit; fork: emit_after_commit | the fork event can't carry `source_trajectory_id` / `through_seq` (183-184); the worker resolves them |
| Baseline | session.py:596-637 and callers | emit a first-contact fact; the worker builds the baseline | today the decision reads trace tables (session.py:551-555, 603-608) |
| Deletion | session.py:340-341 | emit_after_commit plus worker reconciliation against `sessions.is_deleted` | today atomic with the soft delete |
| Pause gaps | `mark_capture_paused_in_tx` callers (revert.py:26; session.py:592, 738; fork.py:197) | emit a `recording.gap` on flag change, or derive it in the worker | removes trace-DB hits while disabled |

### B. Fencing sites that need an independent runtime check

These fences currently apply **only when recording is enabled**.

| Recorder fence | Paths | Existing independent check | Needed |
|---|---|---|---|
| R1 `request.started` | llm.py:1057, 1533, 1755; image_gen.py:380; video_analyze.py:224; dispatch ← video_production.py:962, 1036, 1116; video_providers.py:955; ims_client.py:97 | loop.py:1156; hooks.py:195; heartbeat abort (runtime.py:201-213) | a check at dispatch using `question.runtime.current_run`, exempting title / suggestions as recorder.py:185 does. Covers retries, compaction, tool-side LLM calls and **paid media submits** |
| R1 `request.delta` | agent/trajectory.py:570-591 | abort via heartbeat every 20 s; processor.py:516 | in-process invalidation (bus event on generation or run change) plus a periodic check; stale chunks are then fenced with up to heartbeat latency instead of per 50 ms batch |
| R1 `tool.started` | hooks.py:200; tool.py:167 | hooks.py:132, 195, immediately before | covered for hooks, including nested batch (batch.py:67); audit any direct `execute` calls |
| R1 `tool.output` | hooks.py:170, 282; tool.py:185; bash.py:193 | none per chunk | same approach as delta |
| R1 `step.started` | loop.py:495 | loop.py:414 | covered |
| R2 projection | save_part, update_part_data, update_message_info, create_assistant_message, update_session (setting keys), regenerate, dismiss, suggestions, prepare_trajectory_assets; bound at processor.py:450-456, 1103; compaction.py:456, 467, 489; loop.py:332, 497; hooks.py:79 | `runtime.transaction` locks but doesn't check ownership (runtime.py:106-113). Checks do exist at session.py:410-418, question.py:202-203, suggestions.py:105, 123, runtime.py:230 | inside `runtime.transaction` / the projection helper: when the `current_run` ticket targets this session and `execution.generation != ticket.generation or (execution.run_id and execution.run_id != ticket.run_id)`, raise |

- **New exception type.** Introduce `question.runtime.Superseded(Exception)` that is **not** a `ValueError`, so the implicit catches (continuation.py:237; api/questions.py:31; processor.py:1130) can't swallow it. Replace every F / F+R `except TrajectoryError` / `isinstance` in §3 with it.
- **Delete** the R-only handlers and the defensive ones.
- **Default-on fencing changes production behaviour.** Stale runs will now be stopped for users without recording. That also adds one lock transaction per provider dispatch.

### C. What moves where

- **Out of the API process:**
  - archive worker, exports, shutdown flush (main.py:79-90, 107-117, 249)
  - admin HTTP and WS routers (main.py:332-335; api/admin_trajectories.py; api/admin_trajectory_ws.py)
  - the `trajectory.available` publish (recorder.py:243-253) moves to the worker
  - projection (repository.py:70-147), payload staging, `sanitize` / redaction, ancestry validation (recorder.py:43-63), idempotency, seq
- **Stays in business code:**
  - `TraceContext` / `bind` / `current` and the carriers in §1.8
  - `public_value` / `request_snapshot` allowlists (credential exclusion)
  - `chunk_index` and producer timers
- **Remove from the business DB:** `db/models/trajectory.py`; the trajectory tables in migration f6a8c0e2b4d6 (keep the `trace_context` columns at 136-137); readiness entries (db/base.py:335-340).
- **Rewrite:** scripts/trajectory_dev_server.py (it seeds the business SQLite).

### D. What breaks

- **Event ids that embed `trajectory_id`:** `asset:{sha256(tid:asset:sha)}` and `asset:{sha256(tid:asset:deleted)}` (artifacts.py:102, 233), `evt_start_{tid}`, `evt_baseline_{tid}[_{seq}]` (recorder.py:86-101). Producers can't compute these; they need session-scoped ids.
- **Ordering:** `flush()` currently guarantees that stream chunks commit before structural events (recorder.py:221). Without receipts the worker needs producer sequence numbers.
- **Validation:** `prepare` validation raises at the producer today (types.py:81-120). `IdempotencyConflict` (recorder.py:126, 133) raises today. Both must become drop / gap / keep-first.
- **Business behaviour that exists only while recording is enabled:**
  - transient FileAsset creation (agent/trajectory.py:168-176)
  - ownership refusals (160-165, 204-211, 243, 293)
  - cron session lock (executor.py:626-629) and the todo row lock
  - OSS deletion of generated images after a recording failure (image_gen.py:516-521)
  - the non-streaming bash re-execution fallback, which is reachable only when a non-trajectory error happens (bash.py:212-217)
- **Error semantics:**
  - Recording failures no longer abort cron (executor.py:195-198), revert, permission or prompt acceptance.
  - Recording failures no longer fail question APIs with 422, resume with a permanent error, or skip `finish_run` (loop.py:1473-1480).
  - Tests that assert fail-closed behaviour must change.

### E. Risks

- **Lost facts.** A crash between commit and spool append, or queue overflow, loses after-commit facts. Producer boot_id plus a per-process emit counter let the worker detect the gap.
- **Multi-process producers.** API replicas, cron timer, question worker and video recovery can all write the same session. The worker assigns seq by arrival, which can invert causal order; `caused_by_event_id` exists (context.py:25) but is unused.
- **Shared volume.** A shared docker volume assumes co-location; `k8s/` manifests exist.
- **Secrets in the spool.** Redaction currently happens before persistence (recorder.py:122; stream redactors). If it moves to the worker, the spool holds secrets.
- **Very large events enqueued in memory:** compaction input (compaction.py:317), full prompts with base64, file diffs, skill content, provider bodies.
- **Commit hook edge cases.** The new after-commit hook must handle savepoints (recorder.py:245), rollbacks (256-262) and mid-scope commits (image_gen.py:368, 515; video_production.py:644, 711).

### F. Open questions

1. How does the worker build baselines and validate ownership: a business-DB read replica, or producer-shipped snapshots?
2. How do inline non-asset media (`inline_non_asset`, `input_audio`, `_transient_images`) reach content-addressed blobs: through the JSONL spool, or as side-files?
3. Derived `analysis/{user}/` frames and audio are not FileAssets: record OSS-key references tied to the source asset, or always register hidden assets?
4. Should the ownership checks in agent/trajectory.py become always-on business validation?
5. What replaces the durable permission reply?
6. Desktop single-user SQLite mode (db/base.py:112-121): is there a worker and spool at all?
7. Where is the recording flag evaluated (emit gating vs worker), and how are pause/resume gaps represented?
8. How is guaranteed delivery achieved for deletion and revoke facts (reconciliation job vs outbox)?