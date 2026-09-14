for an invalid or undelegated owner, a missing or deleted session, or a workspace mismatch (`recorder.py:32-62`), and when restarting a deleted trajectory (80).
  - For a stale run lease on `request.started/delta`, `tool.started/output` and `step.started`, except title/suggestions requests after the run ended (182-187; the exception is at 185).
- **Recording failures propagate (fail-closed):**
  - Every persistence error becomes `RecordingError` (`types.py:123-139`; `recorder.py:446-450`).
  - Producers re-raise `TrajectoryError`: `tool/bash.py:105-107, 209-211`; `agent/hooks.py:90-92, 216-218`; `agent/llm.py:1411-1414, 1865-1868`; `agent/trajectory.py:108-114, 484-495, 523-530, 623-629`.
- **Notifications:** `trajectory.available` is published only after the outer commit and dropped on rollback (`recorder.py:243-262`). The admin WS consumes it in the same process (`api/admin_trajectory_ws.py:152,159`).
- **Recording off still writes to the trajectory tables:**
  - An existing trajectory gets a `recording.gap` paused marker (`recorder.py:206-212, 227-240`; `session/session.py:591-592, 737-738`).
  - `record_stream` returns a task that records a gap (`recorder.py:394-395`).
- **Streams:**
  - Receipts and backpressure use a 4 MiB per-root queue, a 64 MiB global limit and a batch window (`recorder.py:265-360`).
  - Structural events flush pending chunks first (`recorder.py:219-220, 413-443`).
- **Deletion:** runs inside the session-delete transaction and leaves a tombstone (`session/session.py:340-341`; `trajectory/lifecycle.py:10-28`); `purge_deleted_content` does GC (31-59).
- **Exports:**
  - The build runs as FastAPI `BackgroundTasks` (`api/admin_trajectories.py:149`). `httpx.ASGITransport` finishes those before returning, so tests see `completed` on the next GET (`test_trajectory_storage.py:171-174`; `test_trajectory_read_races.py:44-47`).
  - Export validation outer-joins `file_assets` (`api/admin_trajectories.py:167-171`).
- **Business code reads trajectory tables:**
  - Permission lost-wakeup recovery (`permission/permission.py:215, 226-246`).
  - Fork provenance (`session/fork.py:178-184`).
  - Checking whether a baseline exists (`session/session.py:538-554`).
- **Record reads load the whole session state:** `get_record` (`repository.py:428-444`); `search` does a case-insensitive substring match over each record's JSON (447-467).

## 3. Backend test inventory

**Dependency tags:**
- **SYNC**: reads trace tables right after the producing call returns.
- **RB**: rollback or journal-failure atomicity.
- **OE**: expects `OwnershipError`.
- **IC**: expects `IdempotencyConflict`.
- **SEQ**: seq allocated in the transaction.
- **PJ**: projection updated in the same commit.
- **NTF**: in-process after-commit notification.
- **FC**: fail-closed.
- **BP**: receipts, backpressure or flush barrier.
- **CP**: `file_asset` bytes copied into trajectory storage.
- **DEL**: deletion inside the business transaction, or a tombstone that rejects writes.
- **ST**: whole-state read or `search_text`.

### 3.1 Unit tests

**`unit/test_trajectory_projection.py`**: 6 functions, 13 cases; no DB; fast; all KEEP.

| Test (lines) | Asserts |
|---|---|
| `test_all_shared_projection_fixtures` (11-22), x2 fixtures | `replay` equals `expected_state`; statistics and agents match; the historical prefix matches; checkpoint plus tail equals the full replay |
| `test_cross_language_golden_full_chunked_and_checkpoint_replay` (25-40) | `events[:17]` equals the historical state; results don't depend on batch splits; `tool:call_a` output goes "one" then "one two"; block text "我来读取"; 3 requests; 45 input tokens |
| `test_unknown_versions_visible_context_immutable_and_seq_safe` (43-58) | version 99 goes to `unsupported_events`; bind/derive nesting; TraceContext round trip; `prepare` raises without `request_id`; `sequence("1.5")` raises; 2^53+1 parses |
| `test_credential_redaction_covers_nested_raw_and_signed_urls` (61-68) | `sanitize` removes secrets and does not mutate its input |
| `test_raw_json_and_incomplete_credential_fragments` (70-82), x7 | Raw, escaped and partial JSON credentials are redacted |
| `test_schema_credential_property_definitions_remain_inspectable` (85-91) | Schema constraints are kept; values are redacted |

**`unit/test_trajectory_stream_redaction.py`**: 14 functions, 41 cases; no DB; fast; all KEEP, because redaction must still happen before `emit`. It covers:
- **Split keys:** a key split across chunks (49-55, which also pins the `{"$stream_blocks":…,"availability":"sanitized_reference"}` raw shape).
- **Every split position** for 17 secret shapes: password, spaced/punctuated/escaped/unicode keys, nested credentials, Bearer, Basic, Authorization, Cookie, signed URLs, URL userinfo, `sk-`, JWT (58-91).
- **Benign text:** survives 1-character chunking (x12, 94-107); plain fragments pass through unchanged (110-113).
- **State isolation:** per block and per redactor (116-127).
- **Mode handling:** replace-to-delta transition (130-138); cumulative replace holds back short prefixes (141-148); escaped quote (151-155).
- **URLs:** pending or oversized URLs (158-164); final replace yields `sanitized_reference` (167-176).
- **Business fields and schemas:** http.response business fields kept (179-193); tool schema definitions kept (196-209).
- **Raw deltas:** an unmapped raw delta gets `sanitized_inline_delta` and a tail (212-221); an unknown nested provider delta is handled (224-231).

**`unit/test_trajectory_migration.py`**: 1 case; sync SQLite file; fast.
- `test_additive_migration_up_down_and_nullable_trace_context` (8-22): revision `f6a8c0e2b4d6` alone creates `trajectory_events`, `trajectory_records`, `trajectory_payloads` and `trajectory_exports`; adds a nullable `session_executions.trace_context`; adds payload `content` and `storage_status`; downgrade restores the stubs.
- KEEP: the revision is part of the shipped chain.

**`unit/test_trajectory_runtime.py`**: 11 cases; no DB writes (the `recorded` fixture patches recording); fast.

| Test (lines) | Asserts | Tags | Class |
|---|---|---|---|
| `test_request_snapshot_uses_allowlist_and_omits_private_provider_state` (33-44) | Allowlist; keys, headers and encrypted content listed in `omitted_fields` | — | KEEP |
| `test_each_adapter_dispatch_has_identity_and_persists_chunks_before_delivery` (47-83) | `request.started` is recorded before `acompletion` (67); each `text_delta` is recorded before delivery (75-77); 2 attempts get 2 request ids and share a step; chunk_index 1,2,1,2; no key | ordering | ADAPT (check emit order, not durability) |
| `test_failed_recording_never_dispatches_provider` (86-106) | Recording raises `TrajectoryError`, the stream raises, and the provider is not called | FC | REWRITE |
| `test_tool_denial_is_recorded_without_dispatch_or_execution_duration` (109-124) | requested then finished, status denied, `duration_ms` None | — | ADAPT (swap the capture fixture) |
| `test_full_tool_result_is_retained_before_model_truncation` (127-155) | Full `tool.output`; `model_output` "short"; one `tool.started` | — | ADAPT (swap the capture fixture) |
| `test_invalid_tool_arguments_never_have_execution_start` (158-176) | requested then finished failed; no duration | — | ADAPT (swap the capture fixture) |
| `test_custom_executor_full_output_slot_is_cleared_between_calls` (179-198) | The full-output slot resets per call | — | ADAPT (swap the capture fixture) |
| `test_responses_final_only_output_is_a_replace_checkpoint` (201-207) | Pure block builder | — | KEEP |
| `test_parallel_batch_keeps_parent_and_sibling_contexts_separate` (210-238) | Distinct part ids; `parent_call_id`; parent context restored | — | ADAPT (swap the capture fixture) |
| `test_stream_reader_can_fill_one_batch_before_first_receipt` (241-270) | Chunk delivery waits for receipts (264) | BP | DELETE (replace with a non-blocking emit test) |
| `test_closing_consumer_closes_provider_reader_and_records_cancel` (273-296) | Provider reader closed; `request.finished` cancelled | — | ADAPT (swap the capture fixture) |

**`unit/test_trajectory_session_runtime.py`**: 7 functions, 10 cases; conftest in-memory SQLite with the real recorder; medium (*est.*).

| Test (lines) | Asserts | Tags | Class |
|---|---|---|---|
| `test_existing_session_records_only_new_input_and_captures_actual_baseline` (33-65), x4 | Off: no events. On: one `input.accepted`; legacy baseline history; seq 1..n (61); `SessionExecution.trace_context` saved (62-65) | SYNC SEQ | ADAPT |
| `test_input_and_compatibility_projection_rollback_with_journal` (68-88) | A journal failure raises and leaves 0 Message, Part and SessionTrajectory rows and 0 events | RB FC | REWRITE (invert it) |
| `test_child_prompt_and_messages_join_parent_trajectory_before_child_runs` (91-112) | No child root; child `trace_context` points at the parent; parent gets `input.injected` | SYNC | ADAPT |
| `test_superseded_generation_cannot_commit_a_late_chat_part` (115-135) | A late `save_part` raises `TrajectoryError`; the part is absent; no new events. The check lives in `session/session.py:510-516` and only runs when recording is enabled (503-504) | fence inside recording | REWRITE |
| `test_delayed_cron_injection_restores_original_turn_and_is_idempotent` (138-162) | Injecting twice yields 1 `input.injected` and 1 `job.progress`; original turn kept; ambient context restored | SYNC, event-id dedupe | ADAPT |
| `test_auxiliary_request_chunks_can_follow_finished_run_with_original_identity` (165-191) | Suggestions after `finish_run` keep turn and run; chunk_index 1,2 | exception at `recorder.py:185` | ADAPT |
| `test_cron_pipeline_uses_saved_target_for_summary_child_run_and_completion` (194-247) | Saved root and child contexts used; `CronRun.trace_context`; `job.finished` completed | SYNC | ADAPT |

### 3.2 Integration tests

All of these use `tracedb` (SQLite file or PostgreSQL) except where noted.

**`integration/test_trajectory_storage.py`**: 15 cases; medium (*est.*).

| # | Test (lines) | Asserts | Tags | Class |
|---|---|---|---|---|
| 1 | `test_transactions_rollback_retry_notifications` (61-80) | No notification before commit; rollback leaves 0 events and doesn't use up a seq; after commit, seq "2" and a notification; retrying the same id gives the same seq; different content raises IC; `committed_seq` 2, `next_seq` 3 | RB SEQ IC NTF | REWRITE |
| 2 | `test_concurrent_append_contiguous_sqlite` (82-90) | 4x8 concurrent writes give seq 1..33 and `committed_seq == projected_seq` | SEQ PJ | REWRITE |
| 3 | `test_historical_checkpoint_and_stream` (92-106) | `flush` returns "7"; `state_at("4")` shows partial text; final state equals `replay`; a lagging `projected_seq` is still correct | BP PJ | ADAPT |
| 4 | `test_child_ownership_deleted_session_unknown_result` (108-120) | Child events project `unknown`; foreign or undelegated source, and a deleted root, raise OE | OE | REWRITE |
| 5 | `test_admin_read_paths_live_role_and_target` (137-158) | Listing ignores workspace; cursor paging; 403 on 9 paths plus list and export for non-admins; search hit at H=2; unrecorded session shows `not_recorded` and gets no row; role downgrade gives 403 | SYNC ST | ADAPT |
| 6 | `test_payload_history_export_deletion` (160-184) | `$payload` externalized; secret absent; 404 before `first_seq` and 200 at 2; cross-session 404; export ZIP manifest and sha256; deleted row gives 410 for payload and download | SYNC PJ | ADAPT (the row mutation at 180 needs a rewrite) |
| 7 | `test_durable_payload_staging_archives_without_transaction_lock` (186-209) | Row staged `pending`; blob outage keeps it readable; later `stored` with content None | CP | REWRITE |
| 8 | `test_stream_barrier_deletion_tombstone_and_gc` (211-227) | Chunk seq before `finished` seq; deletion empties events and sets a tombstone; a late append raises OE; `purge_deleted_content` | BP DEL OE | REWRITE |
| 9 | `test_read_committed_cache_race_does_not_leak_future` (229-242) | A stale header at seq 3 never shows future text | PJ | ADAPT |
| 10 | `test_pause_resume_baseline_and_export_gaps` (244-270) | Off: one gap and no content. On: 2 baselines. Manifest `complete False` with 2 gaps | in-transaction pause marker | ADAPT (open question) |
| 11 | `test_failed_stream_retires_for_fresh_run_and_records_gap` (272-291) | A DB outage raises `RecordingError`; rows are started, started, gap, started; capacity back to 0 | FC BP | REWRITE |
| 12 | `test_checkpoint_pages_reuse_unchanged_history` (293-309) | The first `record_pages` page is reused | PJ | ADAPT |
| 13 | `test_global_stream_capacity_backpressures_independent_sessions` (312-342) | One session's blocked write holds up the other session's receipt | BP | REWRITE (invert it) |
| 14 | `test_malformed_cursors_are_validation_errors` (345-353) | 400 for sessions, records and search | — | ADAPT (app only) |
| 15 | `test_export_shutdown_resumes_and_deletion_wins` (356-388) | `stop_exports` cancels; `resume_exports` restarts; deletion wins | DEL | ADAPT |

**`integration/test_trajectory_boundaries.py`**: 9 cases; uses `app_client`; medium.

| # | Test (lines) | Asserts | Tags | Class |
|---|---|---|---|---|
| 1 | `test_scoped_tickets_are_atomic_and_cannot_authenticate_execute_socket` (78-93) | A ticket needs its audience; 20 concurrent consumes, 1 wins; identity fields present; a forged role claim gets 403 | — | ADAPT (shared ticket store) |
| 2 | `test_readonly_socket_has_no_execution_side_effect_and_rechecks_live_role` (96-126) | `subscribed`; 4 actions return `READ_ONLY`; the watermark has exactly 5 keys; no execution; role loss closes with 4403 | SYNC NTF | ADAPT |
| 3 | `test_idle_subscription_stops_after_token_revocation` (129-136) | Blacklisted token closes with 4401; REST 401 | — | ADAPT |
| 4 | `test_ordinary_websocket_never_receives_owned_trace_content` (139-149) | `api.ws._on_bus_event` drops trajectory events | — | KEEP |
| 5 | `test_late_job_callbacks_use_submission_identity_and_cannot_revive_deleted_trace` (152-192) | Context saved on the job; duplicate late callbacks produce 2 events on the original run; after deletion, 0 events and the tombstone stays | SYNC DEL | ADAPT |
| 6 | `test_retained_attachment_reuse_and_explicit_delete` (195-222) | Same `payload_id` reused; bytes `b"one"`; revoked gives `FileNotFoundError` | CP | REWRITE |
| 7 | `test_permission_commit_survives_lost_wakeup_and_keeps_original_call` (225-262) | The recorded resolution is read back from the trace DB (`permission/permission.py:226-246`) and raises `PermissionCorrectedError` | business code reads the trace DB | REWRITE |
| 8 | `test_old_pending_question_adopts_baseline_and_resumes_without_tool_execution` (265-298) | Baseline with the pending question; `question.resolved`; no `tool.started` | SYNC | ADAPT |
| 9 | `test_file_tools_retain_actual_edits_and_patch_does_not_repeat_last_operation` (301-336) | 4 writes and 4 `artifact.recorded` with before/after and diff | SYNC | ADAPT |

**`integration/test_trajectory_agent_loop.py`**: 4 cases; full app and the real loop; slow (*est.*, background waits have a 10 s timeout at 119, 289, 439).

| # | Test (lines) | Asserts | Tags | Class |
|---|---|---|---|---|
| 1 | `test_user_input_through_application_agent_loop_and_auxiliary_request` (42-175) | 3 provider calls; contiguous seq; one turn; purposes chat, chat, suggestions; tool event sequence; provider schema captured; 3 bills linked; state records | SYNC SEQ PJ | ADAPT |
| 2 | `test_pause_resume_captures_new_baseline_and_reads_attachments_before_write_lock` (178-227) | Asset reads happen without a lock; gap paused then resumed; `fetched` equals both assets; baseline bytes; `recording_status` "gap" | CP | REWRITE |
| 3 | `test_multilevel_task_uses_one_root_and_independent_child_runs` (230-343) | One root; 2 spawned agents; 3 runs; parent call links; 6 requests and 12 deltas | SYNC | ADAPT |
| 4 | `test_real_compaction_prunes_effective_context_but_preserves_replay` (346-524) | Full raw outputs next to truncated parts; compaction events; replay before and after is preserved; the new context excludes the originals | SYNC PJ | ADAPT |

**`integration/test_trajectory_auxiliary.py`**: 7 functions, 8 cases. The bash, shutdown and desktop tests don't use `tracedb`.

| Test (lines) | Asserts | Tags | Class |
|---|---|---|---|
| `test_image_edit_captures_actual_parameters_owned_versions_and_existing_bill` (22-101) | Exact wire kwargs; new request id; input sha256; output payload bytes (91-93); bill linked | SYNC CP | ADAPT (rewrite 91-93) |
| `test_bash_recording_failure_does_not_kill_or_reexecute[idle_judge, output]` (104-126) | `RecordingError` propagates; no fallback run; no kill | FC | REWRITE x2 |
| `test_retained_file_hash_matches_redacted_content` (129-143) | Redacted before/after text with sha256 | SYNC | ADAPT |
| `test_shutdown_stops_archive_after_receipt_failure` (146-156) | `_shutdown_trajectory` still stops workers after raising (`main.py:79-90`) | FC | REWRITE |
| `test_desktop_initializes_dedicated_tickets_without_enabling_auth` (159-174) | Desktop mode issues tickets with auth off | — | KEEP (open question) |
| `test_fork_retains_baseline_only_and_new_root_records_future_input` (177-208) | 2 roots; `history.forked` in and out; baseline only | SYNC | ADAPT |
| `test_snapshot_restore_and_undo_have_observed_terminal_events` (211-231) | `history.reverted` requested/completed x2 | SYNC | ADAPT |

**`integration/test_trajectory_media_dispatch.py`**: 6 functions, 10 cases.

| Test (lines) | Asserts | Tags | Class |
|---|---|---|---|
| `test_video_dispatch_captures_final_wire_and_poll_does_not_create_request[ark, bossip, sd2, task]` (50-113) | POST then GET x2; business body unchanged; one `provider_wire` prepare with placeholders; no secrets; retained media bytes (101-104); 2 polls on one request; job still `dispatching`; `_trajectory_request_ids` saved | SYNC CP | ADAPT x4 (rewrite 101-104) |
| `test_asr_dispatch_and_observed_results_keep_one_request[dashscope, openai_url]` (116-149) | One request; 2 or 0 observations; one POST | SYNC | ADAPT x2 |
| `test_ims_captures_final_sdk_query_and_polls_saved_job_context` (152-186) | SDK query recorded; poll uses the saved context | SYNC | ADAPT |
| `test_derived_media_remains_revocable_with_original_video` (189-214) | Deleting the source revokes the derived copy | CP | REWRITE |
| `test_media_recording_failure_prevents_actual_submit` (217-229) | A recording error blocks the HTTP submit | FC | REWRITE (split out the namespace guard at `agent/trajectory.py:156-165, 241-243`) |
| `test_explicit_media_retry_has_a_new_dispatch_id` (232-260) | 2 request ids: failed, then completed | SYNC | ADAPT |

**`integration/test_trajectory_media_deletion.py`**: 1 function x2 (`large_request`); uses `app_client`.
- `test_inline_and_derived_media_have_no_independent_copy_after_source_deletion` (23-103) checks:
  - The SDK body is not mutated.
  - Media refs point to the original asset by sha256; the derived frame keeps `source_asset_id`.
  - After deletion: reads return 200 with no base64; payloads return 410; the old export returns 410; a new export is incomplete; a new capture shows `source_attachment_deleted`.
- CP; REWRITE the internals and keep the HTTP assertions.

**`integration/test_trajectory_read_races.py`**: 3 functions, 14 cases; uses `app_client`; all ADAPT (the `attachment` helper needs a rewrite).
- `test_download_rechecks_during_blob_read` (34-71), x10: for payload and export, authority or content changes during `blob.download`:
  - role change gives 403; revoked token 401; asset deleted 410; payload deleted 410; root deleted 404.
  - The body never leaks and `Cache-Control` is `no-store`.
  - Revalidation happens at `api/admin_trajectories.py:126,195` and `trajectory/auth.py:31-51`.
- `test_admin_json_and_ticket_are_not_cacheable` (74-90): `no-store` on 200/400/422/403 and on the ticket.
- `test_record_detail_accepts_encoded_path_identity` (93-111), x3: ids containing `/`, `%2F`, unicode or `?#` round-trip (ST).

**`integration/test_trajectory_responses_title.py`**: 2 functions, 3 cases; all ADAPT.
- `test_responses_native_and_fallback_persist_actual_dispatches[native, fallback]` (39-167) checks:
  - `input` equals the wire JSON.
  - Seq order prepared < started < finished (114).
  - Identity fields match.
  - `$stream_blocks` resolves back to the raw chunks.
  - Fallback records `http_400`, then `route_changed`, then `previous_request_id`.
- `test_title_entry_after_run_records_original_identity_and_existing_bill_once` (170-229): after the run ends, the title request keeps the original identity and gets one linked bill. It relies on `recorder.py:185`.

**`integration/test_trajectory_stream_redaction.py`**: 4 functions, 13 cases; all ADAPT.
- `test_fragmented_request_credentials_never_enter_retained_views` (80-137), 5 surfaces x inline/payload: no fragment appears anywhere, including `search_text` at 31 (that part is REWRITE); chunk count and index are right; `$payload` follows `INLINE_BYTES`.
- `test_tool_cumulative_output_redaction_resets_for_each_call` (140-176).
- `test_redaction_state_is_not_shared_between_requests` (179-204).
- `test_sensitive_schema_properties_remain_the_same_request_and_tool_contract` (207-266).

**`integration/test_trajectory_tool_output.py`**: 4 functions, 5 cases; all ADAPT.
- Bash stream over the preview limit replays at each seq (53-87).
- MCP tool and resource keep the full public body (90-117, x2).
- `web_fetch` keeps the processed body (120-138).
- Bash redaction across chunks after 100k characters (141-161).

**`integration/test_trajectory_video_billing.py`**: 1 case (18-65); ADAPT. Video analysis prepare and settlement are linked to exactly one bill, and the parent context is unchanged.

**`integration/test_trajectory_process_crash.py`**: 1 case (9-23); slow (2 spawned interpreters plus a real 2 s lease; 45 s timeout at 16); REWRITE.
- Runs the crash benchmark on SQLite.
- Asserts exit code -9, distinct PIDs, preserved prefix, 0 re-executions, old records unknown, and the new input completed.

### 3.3 Other backend files that touch trajectory

| File | What it touches | Class |
|---|---|---|
| `unit/test_database_readiness.py` (8 functions, 28 cases) | `_create_current_schema` creates the 7 trajectory tables (55-61). `test_desktop_trace_context_upgrade_preserves_old_runs_and_is_repeatable` (173-184). `test_readiness_requires_trajectory_migration_columns` x4 (187-200) covers two `trace_context` columns and payload `content`/`storage_status`. Readiness list is at `db/base.py:335-341` | ADAPT: keep `trace_context` in business readiness; move table checks to trace-DB readiness |
| `unit/test_durable_questions.py` (33) | The `state` fixture leaves trajectory tables out of `create_all`, drops `cron_runs.trace_context`, then runs `d9e1f3a5b7c2` and `f6a8c0e2b4d6` (53-67) | KEEP; adjust the fixture if the metadata splits |
| `unit/test_tool_exposure_migration.py` (3) | At head: 7 trajectory tables and both `trace_context` columns (167-171). Downgrade removes them (213-214) | ADAPT once a business migration drops these tables |
| `unit/test_migration_heads.py` (1) | Single head, including merge `c7e9b1d3f5a7` (17-29) | KEEP; copy for the trace DB |
| `conftest.py` | Env isolation; in-memory schema | ADAPT |

### 3.4 Business tests that run through recording code with recording off

- **Files and call counts:** these files call `create_user_message`, `save_part`, `start_run`/`finish_run`, `RequestCapture`, `ToolHooks.wrap_execute`, cron, permission or fork entry points.
  - `unit/test_durable_questions.py` (57), `unit/test_durable_question_failures.py` (13), `integration/test_mobile_notification_events.py` (10), `unit/test_cron_executor.py` (9), `unit/test_processor_outcomes.py` (8), `unit/test_mcp_security.py` (6).
  - `unit/test_tool_part_identity.py`, `unit/test_turn_abort.py`, `integration/test_message_center.py` (4 each); `unit/test_tool_runtime_loop.py` (3).
  - 2 each: `unit/test_permission_patterns.py`, `unit/test_tool_exposure_state.py`, `unit/test_suggestions.py`, `unit/test_plan_enter_question.py`, `unit/test_cron_injector.py`, `unit/test_todo_edits.py`, `unit/test_desktop_activation.py`, `unit/test_loop_termination.py`.
  - 1 each: `unit/test_tool_part_runtime_binding.py`, `unit/test_sandbox_assets.py`, `unit/test_user_id_discipline.py`, `unit/test_image_gen.py`.
- **Why they need trajectory tables today:** with recording off they still hit `mark_capture_paused_in_tx`, a SELECT … FOR UPDATE on `session_trajectories` (`recorder.py:206-212, 227-231`; `session/session.py:591-592, 737-738`). So they need trajectory tables in the business test DB.
- **Runtime fencing already tested without recording:**
  - `unit/test_durable_questions.py:160-162, 211, 291-298, 412-428, 453`.
  - `unit/test_durable_question_failures.py:126-145, 218-244`.

### 3.5 Classification totals

| File | Cases | KEEP | ADAPT | REWRITE | DELETE |
|---|---:|---:|---:|---:|---:|
| unit/test_trajectory_projection.py | 13 | 13 | | | |
| unit/test_trajectory_stream_redaction.py | 41 | 41 | | | |
| unit/test_trajectory_migration.py | 1 | 1 | | | |
| unit/test_trajectory_runtime.py | 11 | 2 | 7 | 1 | 1 |
| unit/test_trajectory_session_runtime.py | 10 | | 8 | 2 | |
| integration/test_trajectory_storage.py | 15 | | 8 | 7 | |
| integration/test_trajectory_boundaries.py | 9 | 1 | 6 | 2 | |
| integration/test_trajectory_agent_loop.py | 4 | | 3 | 1 | |
| integration/test_trajectory_auxiliary.py | 8 | 1 | 4 | 3 | |
| integration/test_trajectory_media_dispatch.py | 10 | | 8 | 2 | |
| integration/test_trajectory_media_deletion.py | 2 | | | 2 | |
| integration/test_trajectory_read_races.py | 14 | | 14 | | |
| integration/test_trajectory_responses_title.py | 3 | | 3 | | |
| integration/test_trajectory_stream_redaction.py | 13 | | 13 | | |
| integration/test_trajectory_tool_output.py | 5 | | 5 | | |
| integration/test_trajectory_video_billing.py | 1 | | 1 | | |
| integration/test_trajectory_process_crash.py | 1 | | | 1 | |
| **Total** | **161** | **59** | **80** | **21** | **1** |

- **Benchmarks:** `benchmark.py` ADAPT (drive worker ingest instead). `benchmark_stream.py`, `benchmark_concurrency.py` and `benchmark_process_crash.py` REWRITE.
- **Fixtures:** KEEP.

## 4. Frontend admin-trajectories tests

### 4.1 Vitest files

All 25 are KEEP as long as the HTTP/WS contract and projection shape stay the same. None of them talks to a backend.

| File | Tests | What it covers |
|---|---:|---|
| api/endpoints.test.ts | 3 | Every read is a GET under `/api/admin/trajectories/sessions`; ids are encoded; events use `after_seq`, `limit`, `include_data=true`; the only write is `POST …/export {through_seq}`; every call is abortable (24-78) |
| api/access.test.ts | 12 | Purge on sign-out or role loss; latch on 403; epoch unlock; stop streams; abort tracked requests (53-153) |
| api/queries.test.tsx | 9 | No search hits from another watermark; deleted body replaced; "not produced yet"; late export or download ignored after access loss (58-187) |
| api/sync.test.ts | 26 | Checkpoint vs full replay by `projector_version`; paging; duplicate hints; polling recovery; holes; deletion (404 after load); corruption; network retry; seek (97-497) |
| components/TrajectorySessionPage.test.tsx | 5 | No future leaks; read-only; deletion; not recorded (175-270). Mocks endpoints (23) and socket (41) |
| components/TrajectorySessionPage.fixedH.test.tsx | 2 | Fixed H never shows head rows; live first paint (185-212) |
| components/inspector/MarkdownRenderer.test.tsx | 3 | No remote images; safe links; no raw HTML |
| components/inspector/MediaRefView.test.tsx | 3 | `$media` read through the protected payload endpoint; unavailable states; release on deletion (55-121) |
| panels ArtifactPanels / RequestInputPanel / ToolArgumentsPanel | 4 / 2 / 3 | Versions and diff; request input shape; raw or streaming arguments |
| session PlaybackBar / RecordTable / seekScale | 3 / 4 / 5 | Big-seq stepping; relation nesting; seek scaling |
| components/session/usePlaybackControls.test.ts | 8 | Step boundaries; "both recorder raw-content modes" (207) |
| hooks/useDownloadPayload.test.tsx | 7 + `it.each` x3 | Fresh GET per click; access loss; maps 410 to deleted, 404 to pending, 409 to corrupt (128-133) |
| hooks/usePlayback / useStableWatermark | 2 / 2 | Pinned position; watermark hold |
| utils/adapter.test.ts | 8 | Checkpoint compatibility and bounds |
| utils/artifact.test.ts | 8 | Availability wrappers; media becomes a protected payload ref (141) |
| utils/layout.test.ts, utils/view.test.ts | 8, 14 | Golden fixtures; relation tree; tabs; states |
| utils/params.test.ts | 7 | URL round trip; server-side sort; cursor treated as opaque |
| utils/projector.test.ts | 18 (8 run per fixture) | TS projector matches the backend `expected_state`, statistics and agents; checkpoint plus tail; raw events behind a record "like the server detail" (116) |
| utils/seq.test.ts | 9 | Seq values as decimal strings |

### 4.2 Contract surfaces these tests pin

- **REST paths and params:** `api/endpoints.ts:18-163`.
- **Types** (`types/protocol.ts`):
  - `TrajectoryRecord`, including `preview`, `result_preview`, `data`, `blocks`, `usage` (177-208).
  - `RecordSummary` (210-211).
  - `ProjectionState`: "checkpoint.state and the client projection share this exact shape" (219-226).
  - `SessionRow.projected_through_seq` (276).
  - `RecordDetail` with events (305-309), `EventPage` (311-318), `CheckpointResponse` (320-328).
  - `SearchHit {record_id, seq, kind, preview}` (330-335) and `SearchPage` (337-342).
- **WS messages:** `api/admin_trajectory_ws.py:25,65-159`: 4401/4403 closes, `READ_ONLY`, `subscribed`, `trajectory.available`.
- **Payload status mapping, `$media` and `$stream_blocks` shapes:** see the hooks and utils rows in §4.1.

### 4.3 Playwright fixture e2e

- **Specs:** `frontend-v2/e2e/trajectories.spec.ts` has 15 tests (194-763): golden parity, list URL state, inspector, replay prefix, live head, stepping, `$media`, 403, 4403, role loss, deletion, read-only, visuals. `e2e/trajectories-scale.spec.ts` has 1 test (123): 100k events and 10,004 records.
- **Mock server:** `e2e/helpers/trajectory-server.ts` intercepts `/api/admin/trajectories` and `/ws/admin/trajectories` (27-28), using the frontend `statistics` module (17) and the backend fixtures.
- **Config:** `testMatch` at `playwright.trajectories.config.ts:15`; 1 worker (19); 90 s timeout (21); base URL 3101 (25). The header comment (1-8) says these runs are fixture evidence, not proof of the backend. KEEP.

## 5. Commands

These come from the repo's Makefile, package files and docs; I did not run them.

```sh
# Backend
cd backend && uv sync --extra test                           # Makefile:83-84
cd backend && uv run pytest tests/ -v                        # Makefile:113-114
cd backend && python -m pytest tests/unit tests/integration/test_trajectory_*.py -q --tb=short   # backend-regression.json:4
cd backend && PYTHONPATH=. python -m pytest tests/unit/test_trajectory_projection.py tests/unit/test_trajectory_migration.py tests/integration/test_trajectory_storage.py -q   # STORAGE_VERIFICATION.md:22
cd backend && PYTHONPATH=. python -m pytest tests/unit/test_trajectory_stream_redaction.py tests/integration/test_trajectory_stream_redaction.py -q   # STORAGE_VERIFICATION.md:31
make deps                                                    # Makefile:87 -> PostgreSQL 16 on :5432
cd backend && TRAJECTORY_TEST_DATABASE_URL=postgresql+asyncpg://<user>:<pw>@localhost:5432/openbox_trajectory_storage_<x> uv run pytest tests/integration/test_trajectory_*.py -q   # the database must already exist
cd backend && OBX_QUESTION_TEST_DATABASE_URL=postgresql+asyncpg://<user>:<pw>@localhost:5432/openbox_questions uv run pytest tests/unit/test_durable_questions.py -q
cd backend && uv run python -m trajectory.benchmark --output <f> [--requests N]     # optional TRAJECTORY_BENCHMARK_DATABASE_URL
cd backend && uv run python -m trajectory.benchmark_stream --output <f>             # optional TRAJECTORY_STREAM_DATABASE_URL
cd backend && uv run python -m trajectory.benchmark_concurrency --output <f>        # optional TRAJECTORY_CONCURRENCY_DATABASE_URL
cd backend && uv run python -m trajectory.benchmark_process_crash --output <f>      # optional TRAJECTORY_CRASH_DATABASE_URL

# Frontend
cd frontend-v2 && npm run test                               # package.json:12 (vitest run; vite.config.ts:47-50 jsdom)
cd frontend-v2 && npx vitest run src/features/admin-trajectories
cd frontend-v2 && npm run check                              # package.json:18
cd frontend-v2 && npm run dev -- --host 127.0.0.1 --port 3101          # must already be running; the config has no webServer
cd frontend-v2 && npx playwright test -c playwright.trajectories.config.ts            # config:8
cd frontend-v2 && npx playwright test -c playwright.trajectories.config.ts e2e/trajectories-scale.spec.ts   # frontend-checks.json:39
cd backend && python scripts/trajectory_dev_server.py --data-dir <tmp> --port 8091   # native acceptance, IMPLEMENTATION_STATUS.md:128,135
```

- **CI:** there is no workflow directory at the repo root, so these suites are run by hand.
- **Known failure:** `tests/unit/test_internal_tunnel_keys.py::test_desktop_preflight_has_stable_503_code` fails on unchanged main (`backend-regression.json:11-15`).
- **Frontend baseline:** whole-app vitest is 105 files, 736 tests, 10.8 s (`frontend-checks.json:13-15`).

## Migration notes

### Where the tests go

| Target suite | Contents | Comes from |
|---|---|---|
| Business unit (no DB) | Event shapes via an `emit` capture seam; request snapshot; lexical redaction; runtime fencing | unit runtime, unit redaction, `prepare` validation |
| Business plus emitter (temp spool) | after-commit and rollback; overflow producing a gap; `emit` never raises; business writes and dispatch succeed when recording fails | Rewrites of session_runtime:68-88, runtime:86-106, auxiliary:104-126, media_dispatch:217-229, storage:61-80, 272-291, 312-342 |
| Worker (trace DB plus fake OSS) | Ingest, seq, dedupe, ownership, externalization, projection, summaries, checkpoints, archive, retention, deletion, exports, HTTP API and WS | storage, read_races, boundaries 1-3, media_deletion HTTP parts, golden projection, `benchmark.py` |
| End to end (app, spool, `drain()`, trace DB) | Loop, media, responses/title, tool output, DB-level redaction, billing links, fork/revert, jobs, questions | agent_loop, auxiliary, media_dispatch, responses_title, stream_redaction, tool_output, video_billing, boundaries 5/8/9 |
| Subprocess | Business-process SIGKILL; worker SIGKILL; multi-process emitters | process_crash and the concurrency/crash benchmarks |
| Frontend | No change | 25 vitest files, 2 specs |

### What breaks

1. **Read-after-write:** 11 integration files and `unit/test_trajectory_session_runtime.py` query `TrajectoryEvent` and `SessionTrajectory` right after the call. Each needs a `drain()`.
2. **Tests call internal transaction APIs directly:** `append_events_in_tx`, `ensure_trajectory_in_tx`, `mark_capture_paused_in_tx`, `create_checkpoint_in_tx`, `record_stream`, `flush`, `_capacity` (storage); `delete_trajectory_in_tx` (storage, boundaries, read_races); `drain_payloads`, `store_bytes`, `delete_for_asset` (storage/read_races); `capture_asset_in_tx`, `revoke_asset_in_tx`, `_retained_asset`, `record_job_in_tx`; `create_export`, `build_export`, `stop_exports`, `resume_exports`, `purge_deleted_content`; `state_at` (7 files); `expand`/`read_payload` (7 files).
3. **Expected exceptions go away:**
   - `IdempotencyConflict`: storage:77.
   - `OwnershipError`: storage:117, 120, 226.
   - Recording errors: runtime:103, session_runtime:82, 131, auxiliary:123, 153, media_dispatch:226, storage:281.
4. **Fencing lives in recording code today:**
   - `session/session.py:510-516` runs only when recording is enabled.
   - `recorder.py:182-187` is the recorder's stale-lease check.
   - Fences that already work without recording: `agent/hooks.py:131-133, 195-196`, `agent/loop.py:414, 1156, 1448`, `session/session.py:411-414`, `question/runtime.py:127-198`.
   - The media namespace and deleted-asset guards exist only when recording is enabled (`agent/trajectory.py:138-165, 186-211, 230-243`).
5. **Trajectory models sit in the business `Base`** (`db/models/__init__.py:52-53`). That affects: conftest `create_all`; `tracedb` `drop_all`/`create_all` (storage:35-37); benchmark setup (`benchmark_concurrency.py:47-58`, `benchmark.py:47-49`); readiness (`db/base.py:335-341`); tool-exposure head assertions; the durable-questions table filter.
6. **Recording-off code still locks `session_trajectories`** (`session/session.py:591-592, 737-738`; `recorder.py:227-231`). The roughly 22 business test files in §3.4 fail with "no such table" if the tables leave the business DB while these calls remain.
7. **Business code reads trace data:** `permission/permission.py:226-246`, `session/fork.py:178-184`, `session/session.py:538-554`.
8. **The API/WS moves to the worker:**
   - The routers are included at `main.py:332-335`, and `app_client` builds the business app.
   - The Vite proxy sends `/api` and `/ws` to 8080 (`frontend-v2/vite.config.ts:42-45`).
   - `scripts/trajectory_dev_server.py:34-50` assumes a single process.
9. **Cross-database reads:**
   - Export validation joins `file_assets` (`api/admin_trajectories.py:167-171`).
   - The admin check reads `users` (`trajectory/auth.py:42-51`).
   - Listing and headers read business sessions, users and workspaces (`repository.py:260-380`).
10. **`search_text` is read by a test** (`integration/test_trajectory_stream_redaction.py:31`) and written at `repository.py:126`.
11. **Receipt and backpressure behavior disappears:** `benchmark_stream.py`, runtime:241-270, storage:312-342.
12. **Shutdown changes:** `main.py:79-90` and auxiliary:146-156.
13. **Byte-copy assertions:** boundaries:195-222, agent_loop:178-227, media_dispatch:101-104 and 189-214, media_deletion, auxiliary:91-93.

### Harness to build

| Piece | Requirements | Replaces |
|---|---|---|
| `spool_dir` fixture | A spool under `tmp_path` with new env vars; a JSONL reader that tolerates a torn last line; asserts the spool stays empty when recording is off | — |
| Emitter control | Reset the process-wide queue and writer thread between tests (today's globals reset only because each test gets a new loop, `recorder.py:283-288`); `flush(timeout)`; fault injection (writer raises, disk full, queue size 1) | `record_stream` patches |
| `emitted` capture seam | Patch `trajectory.emit`, keeping order | `recorded` (runtime:13-24) |
| `drain()` | Synchronous: ingest, assign seq, dedupe, check ownership, externalize, project, summarize, force checkpoint/export jobs, publish the watermark. Returns counters (ingested, duplicates, conflicts, ownership drops, deletion drops, gaps) | `flush()`, `BATCH_MS=1` (7 places) |
| `FakeOSS` | sha256 keys; zstd bodies; put/get/head/delete/list/copy; fault injection; a download hook for race tests; object and byte counters; separate `core.oss.get_oss` host stub (media_dispatch:195) | `MemoryBlob` (storage:19-25) |
| `trace_db` fixture | Its own engine, metadata and alembic; SQLite file by default; opt-in `TRACE_TEST_DATABASE_URL` with the disposable-name guard copied from storage:30-32 | `tracedb` |
| `business_seed` fixture | Seed users, workspaces, projects and sessions (storage:45-54) | `tracedb` seeding |
| `worker_client` / `worker_socket` | Worker ASGI app with both engines; a shared `MemoryCache` for tickets and the JWT blacklist; viewer override | `client` (storage:122-135); `app_client` and `socket` (boundaries:27-75) |
| `pg_trace` fixture | Skip unless the env var is set; `CREATE EXTENSION pg_trgm`; a schema per test via `search_path` (pattern at `unit/test_durable_questions.py:34-45`); covers partitions, tsvector search and retention | — |
| Subprocess harness | Spawn emitter and worker processes and SIGKILL exact PIDs (pattern at `benchmark_process_crash.py:200-241`) | process_crash |
| Shared support module | Move fixtures into `tests/integration/conftest.py` or `tests/support/trajectory.py`; stop importing `tracedb` 10 times, `app_client` 3 times, `Chunk`, `start_recorded_turn`, and the unit Responses helpers across modules | — |
| conftest | Extend the `TRAJECTORY_*` snapshot (conftest.py:7-25) to the new variable names; add a trace-DB setup used only by trace tests | — |

### Risks

- **Atomicity evidence is lost.** Today's tests prove "an event exists if and only if the business commit happened" (storage:61-80; session_runtime:68-88) and "the committed prefix survives SIGKILL". With emit after commit into an in-memory queue, a crash before flush loses events silently unless each process has an epoch and counter the worker can check for gaps.
- **Ordering:**
  - Current assertions: prepared < started < finished (responses_title:114); chunk before the structural event (storage:221); tool event sequence (agent_loop:146-147).
  - The single writer must keep per-producer FIFO; across processes, order is arrival order.
- **Projection lag:** every SYNC test must drain. Only storage:106 exercises a lagging `projected_seq` today.
- **Redaction placement:** the spool is a shared volume, so redaction must stay before `emit`. No test scans the spool.
- **The worker needs business authority data:** users, sessions, `file_assets`, the JWT blacklist and tickets. The 14 read-race cases depend on this.
- **Frontend parity:**
  - Records stored as "summary plus references" must not change `checkpoint.state`, which must equal the client projection (`types/protocol.ts:219-226`).
  - `get_record` must still return the full record with the same event selection as the frontend projector (`utils/projector.test.ts:116`).
- **Search semantics:** today search is a substring match over the full record JSON with a JSON-slice preview (`repository.py:456-463`). Moving to tsvector/pg_trgm may change hits, and backend coverage is only 2 weak assertions (storage:152; read_races:80).
- **Desktop mode:** SQLite in a single process (`db/base.py:112-137`; auxiliary:159-174) has no container slot for a worker.
- **Dependencies:** no zstd library is locked.

### Open questions

1. When recording is off, who emits pause and resume markers (storage:244-270; agent_loop:201-227)?
2. For a duplicate `event_id` with different content, keep the first silently or emit a diagnostic?
3. Is ownership checked in the business process when emitting, or by the worker against the business DB?
4. Payload and media reference format: tests pin `$payload.payload_id`, `$media.payload_id`, the availability wrappers, and the 410/404/409 mapping.
5. Can content be read after `emit` but before the OSS upload completes (storage:196-202 currently asserts it can)?
6. How do tests wait for an export build? Today it completes inside the request via `BackgroundTasks`.
7. How do permission lost-wakeup recovery (boundaries:225-262) and fork `source_through_seq` work without reading the trace DB?
8. Baseline history size relative to the queue bound (`session/session.py:596-637`).
9. Should the media namespace guard become unconditional business validation?
10. Desktop deployment: worker in-process, or none? And where does the ticket endpoint live?

### Acceptance tests to add

| # | Area | Test | Replaces |
|---|---|---|---|
| 1 | Emitter | `emit()` never raises for invalid events, unserializable data, a dead writer, or an unwritable spool; failures are counted | runtime:86-106 |
| 2 | Emitter | Overflow drops events and produces exactly one `recording.gap` with a dropped count; producer latency stays bounded while the writer is stalled | storage:312-342; runtime:241-270; `benchmark_stream` |
| 3 | Emitter | An event reaches the spool only after COMMIT; ROLLBACK or a failed commit discards it; savepoint behavior is defined; outside a transaction it is written at once | storage:61-80 |
| 4 | Emitter | Spool format: JSONL, atomic append, rotation, a torn tail is re-read | — |
| 5 | Emitter | Spool files contain no credential fragments (5 surfaces x inline/payload) | stream_redaction:80-137 |
| 6 | Fail-open | Chat, permission, job and billing writes commit when `emit` fails | session_runtime:68-88 |
| 7 | Fail-open | Dispatch, bash, media submit and tools proceed when recording is broken | runtime:86; auxiliary:104; media_dispatch:217 |
| 8 | Fencing | A superseded generation cannot save a part, with recording on or off; runtime exception; 0 events emitted | session_runtime:115-135 |
| 9 | Fencing | A stale run cannot start a request or deltas, a tool or tool output, or a step; title/suggestions after the run are still allowed | `recorder.py:182-187` |
| 10 | Fencing | The media namespace and deleted-asset guard blocks dispatch with recording off | `agent/trajectory.py:156-165` |
| 11 | Business | Permission lost-wakeup recovery works without the trace DB | boundaries:225-262 |
| 12 | Worker | N spawned emitters produce contiguous seq, unique ids, no holes | storage:82-90; `benchmark_concurrency` |
| 13 | Worker | Duplicate or conflicting `event_id`: first kept, no exception, conflict counted | storage:73-77; boundaries:171-181 |
| 14 | Worker | After SIGKILL mid-batch, restart resumes from the offset with no duplicates or losses | — |
| 15 | Crash | Business-process SIGKILL between commit and flush leaves a detectable gap; no re-execution; `run.interrupted` recorded | process_crash |
| 16 | Worker | Foreign, undelegated or deleted-root events are dropped and counted | storage:108-120, 226 |
| 17 | Blobs | System prompt, tool definitions and each message stored once by sha256 (object count equals unique contents); zstd round trip; payload bytes identical | storage:160-209 |
| 18 | Media | `file_asset` media is recorded as a reference with 0 objects written; deleting the asset gives 410 and an incomplete export with no bytes | boundaries:195-222; media_deletion; media_dispatch:189-214; agent_loop:178-227 |
| 19 | OSS outage | Retries without loss; other trajectories keep progressing | storage:186-209 |
| 20 | Projection | Golden fixtures ingested through the spool: `state_at(H)` equals `replay(≤H)` for every H | projection:11-40 |
| 21 | Projection lag | Reading at `through_seq` never shows future rows; `projected_through_seq` is reported | storage:229-242 |
| 22 | Summaries | List sort, filter and cursor, including unrecorded sessions | storage:137-158 |
| 23 | Archive | Events, `state_at`, record detail and export are correct across an OSS segment boundary | — |
| 24 | Retention | Budget gap reason; blobs deleted only when unreferenced (refcount) | — |
| 25 | Deletion | Tombstone; rows, segments and blobs removed; an in-flight export ends deleted; late events dropped | storage:211-227, 356-388; boundaries:185-192 |
| 26 | API parity | Existing HTTP assertions pass against the worker app (403 matrix, 400 cursors, `no-store`, 404/410, encoded ids, manifest sha256) | storage:137-184, 345-353; media_deletion:69-91 |
| 27 | Revalidation | Role, token, asset, payload and root changes during the OSS read | read_races:34-71 |
| 28 | WS parity | Single-use ticket, `READ_ONLY`, watermark keys, 4403/4401 from the worker; business `/ws` never forwards | boundaries:78-149 |
| 29 | SQL search (PG) | `SearchHit` shape; no future hits; cursor bound to `(head, q)`; CJK and substring cases; no `search_text` column | storage:152; `repository.py:447-467` |
| 30 | `get_record` | Indexed read with a statement count that doesn't grow with record count (10k records); historical `as_of_seq`; same event selection as the frontend projector | read_races:93-111; `projector.test.ts:116` |
| 31 | Partitions (PG) | Inserts route to partitions; queries prune; retention drops partitions | — |
| 32 | Checkpoints | Page reuse under the worker | storage:293-309 |
| 33 | Migrations | Trace-DB alembic single head and up/down; business drop migration with readiness and fixtures updated; keep the `f6a8c0e2b4d6` test | test_trajectory_migration; test_migration_heads:17-29; tool_exposure:167-171 |
| 34 | Isolation | With recording off, the §3.4 business files touch neither the spool nor the trace DB | §3.4 |
| 35 | Frontend | `npm run test` still passes; optionally add a worker-generated API response snapshot | 25 vitest files |