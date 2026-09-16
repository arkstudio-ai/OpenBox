# w2-ingest independent verification (second run)

**branch:** wp/w2-ingest-verified-r2 (not pushed)

**head sha:** bf7b74f2ded7f9ff44396b0a55c1589d6a0e8979 (base 486e724)

**verdict:** fixed

The implementer's head passed all its own tests, but review found five real defects. I fixed each on this branch with a test that fails on the old code (proof listed per fix), and added the missing process-kill crash test. Everything stays inside w2-ingest ownership, and no tests were quarantined or weakened.

**fixes**

1. Duplicate `recording.state` controls (213b8b3, seam c)
   - Symptom: two processes resuming the same period each added a resume gap and bumped `recording_epoch`. A resume also applied to trajectories that were never paused.
   - Cause: the control carries no epoch (checked `producers.py` at dec1ccb), and `_recording_state` guarded pauses only.
   - Fix: the trajectory's own status decides. A pause applies only when not paused; a resume only ends a pause.
   - Test: `test_duplicate_recording_state_controls_from_several_processes_apply_once` (two producers, two periods). Failed before with an extra resumed gap.

2. Earliest first_seq (6adbca0, seam b)
   - Symptom: an event could reference an existing payload row whose first_seq was later than the event, so the content was invisible for H in between.
   - Cause: existing rows were reused without reading first_seq.
   - Fix: lookups load first_seq, and the transaction lowers it with a guarded `UPDATE ... WHERE first_seq > seq`, the same rule as the projection's `ensure_payload_rows`. Availability is never touched, so deleted rows stay deleted.
   - Test: `test_references_lower_first_seq_to_the_earliest_position_without_resurrecting`. Failed before (stayed 50 instead of 3).

3. One failing file stopped ingest for every producer (037194b, plus f9acd6c for the PostgreSQL test)
   - Symptom: any unexpected error in a batch (for example a tombstone purge timing out) escaped `run_once`. The same oldest file failed first on every pass, so no producer progressed.
   - Also: an `occurred_at` like `9999-12-31T23:00:00-05:00` made `parse_time` raise `OverflowError`, with the same effect.
   - Fix: failures are isolated per file, counted as `failed_batches`, logged by error type only, and retried from the committed offset with backoff (1 s doubling to 60 s) while other producers continue. `parse_time` returns None when out of range.
   - Tests: `test_a_failing_batch_backs_off_while_other_producers_continue`, `test_out_of_range_times_are_invalid_values_not_fatal_errors`, `test_times_outside_the_datetime_range_parse_as_missing`. All failed before.
   - Two restart tests (`test_offsets_commit_with_rows_and_a_restart_never_duplicates` on SQLite, `test_services_run_one_task_per_loop_and_resume_without_duplicates` on PostgreSQL) now simulate the kill with a `BaseException`. Their assertions are unchanged.

4. Blob GC race (6efcb5c, seam a)
   - Symptom: the archive package's `process_gc_queue` checks references, then deletes with no lock in between. A batch that stored the object (a no-op `if_absent` put) or reused it, and committed in that gap, left a reference to a deleted object. A reused object whose GC entry was still queued was also never uploaded again.
   - Fix, and the ordering that makes it safe:
     - `lock.ObjectGuard`: a process-local shared/exclusive guard. Deletes cannot starve, and leaving never awaits.
     - `services.GuardedGcBlobStore`: `WorkerServices` wraps `RetentionService.blob_store` with it. A delete of a blob key takes the guard exclusively, re-checks for an available row, and only then deletes. The guard covers one check plus one delete, not a whole pass. Segment, export and prefix deletes pass through.
     - An ingest batch that stores or reuses blobs holds the guard shared from its GC-queue check through commit. Keys with queued entries are uploaded again with `if_absent=False`. Reused bytes are kept until that check and re-encoded with the row's encoding.
     - The transaction re-reads the queue under the trajectory row locks, retries if a new entry appeared, and cancels entries for keys it references. Batches without blobs skip the guard.
     - Result: a delete either sees the committed reference, or runs while no batch is in flight, and later batches re-upload.
   - Tests: `tests/unit/test_worker_ingest_gc.py` uses a stub of the archive package's GC step. It covers a delete after its check while a batch commits, a batch arriving during a delete, and a stale entry for a reused key. All three failed against the pre-fix head. Guard, planner and wiring tests were added too.
   - Cross-check: the real `RetentionService` and lifecycle from 8a8ae7e, overlaid on this head, keep referenced keys, delete orphans, wait on the guard, and re-upload stale queued keys.

5. Daily byte counts lost on PostgreSQL (bf7b74f)
   - Symptom: ingest's `add_user_bytes` and the budget pass's `_user_levels` both read, modify and write the same worker-state JSON row. Overlapping commits lost a batch's bytes or the exceeded marker.
   - Fix: both read the row with `FOR UPDATE`. Lock order stays acyclic.
   - Test: `tests/integration/test_worker_budgets_pg.py`. Failed before (150 instead of 180).

**added coverage:** 19ed384, `test_worker_ingest_crash.py`. A child process is SIGKILLed at four points: after upload and before the transaction, inside the transaction before COMMIT, after a commit, and after a file's last commit but before its deletion. After a fresh restart every event appears exactly once, seq is contiguous, and the blob is readable. It passed on existing behaviour.

**legacy converter:** ran against a legacy schema filled from `backend/trajectory/fixtures`, with media bound to live and deleted assets, a deleted payload, a whole-data `$payload` from the blob directory, a 70 KiB value and a checkpoint page.
- Runs on SQLite and PostgreSQL, same results: dry run, convert, rerun skips, finalize-drop refused before verify, `--verify` exit 0, `--finalize-drop` exit 0 (7 tables dropped).
- Checked afterwards: seq contiguous, every reference has first_seq at or below its event, blob digests match.

**seam d (information only):** ingest uses no savepoints in notification paths. With lifecycle's tombstone the deleted notification is published twice, which is harmless.

**tests** (run from `backend/`; baseline at 486e724 was package 87, PostgreSQL 5, unit 2855 passed / 1 skipped, integration 166 passed / 42 skipped)

| Command (on bf7b74f) | Passed | Failed | Skipped |
|---|---|---|---|
| `uv run pytest` over the 16 package unit files (the implementer's list plus `test_worker_ingest_crash.py` and `test_worker_ingest_gc.py`) `-q` | 102 | 0 | 0 |
| `uv run pytest tests/unit -q` | 2870 | 0 | 1 (existing systemd-analyze skip) |
| `uv run pytest tests/integration -q` (tree identical to bf7b74f, committed right after) | 166 | 0 | 43 (+1 is the new PostgreSQL budget test) |
| `TRAJECTORY_TRACE_TEST_DATABASE_URL=postgresql+asyncpg://postgres:trace@localhost:55481/openbox_trace_test_w2_ingest_v2 uv run pytest tests/integration -q` | 189 | 0 | 20 |
| `uv run pytest tests/unit/test_worker_ingest_benchmark.py -q -s` | 3 | 0 | 0 |

- Benchmark: 19,856 events/s on SQLite; a 42 MiB file peaked at 15.4 MiB traced memory.
- Container `obx-verify2-ingest-pg` is stopped. Ruff is not installed, so no lint run.

**remaining_issues**
1. The projection still uploads and commits blob rows without the guard. It can race GC's check-then-delete, and it can make deleted content available again under the JSON dedupe key. Ingest could then reuse a dangling row if the GC entry is already gone. Recommendation: wrap `project()` and `maybe_checkpoint()` in `object_guard.shared()`.
2. The guard is process-local. That is correct while GC and blob writers run in the writer-lock process; the converter runs outside it.
3. Because `recording.state` carries no epoch, a stale control from a crashed producer's abandoned `.part`, ingested after a newer transition, can still flip the state. A producer-side epoch would make this exact.
4. The converter keeps legacy first_seq on dedupe hits (verify requires equality). The fixture run found no invisible references.
5. A file that permanently fails keeps retrying with backoff; it is visible through spool-age alarms and is not auto-quarantined. A meta un-delete inside a batch would loop, but the business code has no session restore today.
6. Carried over from the implementer: missing wave-2 modules for default wiring, Redis fan-out depends on `init_redis_bus`, an idle killed producer goes undetected, legacy table-name candidates, meta records without `updated_at`, and blocking of same bytes bound to an asset whose meta hasn't synced.
7. For the integrator: after switching to `lifecycle.revoke_asset`, entries it queues inside the ingest transaction come after the check and are not cancelled, so the protocol still holds.

Key files (under /Users/wang/workspace/OpenBox/.claude/worktrees/agent-aa9d774e1740ab0e0/backend):
- trajectory/worker/ingest.py
- trajectory/worker/content.py
- trajectory/worker/lock.py
- trajectory/worker/services.py
- trajectory/worker/meta.py
- trajectory/worker/budgets.py
- tests/unit/test_worker_ingest_gc.py
- tests/unit/test_worker_ingest_crash.py
- tests/integration/test_worker_budgets_pg.py
