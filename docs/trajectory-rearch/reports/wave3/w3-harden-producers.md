# w3-harden-producers report

Everything was finished and committed before the stop instruction arrived, so I ran nothing further. The last full unit run had already finished and is green: 3158 passed, 0 failed, 41 skipped.

**Branch:** `wp/w3-harden-producers`
**Head:** `b746e25c63691bc7342a738bbab55dbc195c833b`
**Commits:** `4713bcb` (meta sync), `fcfca89` (epochs and unlocked settings writes), `b746e25` (artifacts). Nothing pushed; the worktree is clean.

## Summary
All four items of the w3-harden-producers section are done. Recording stays fail-open.

1. **`artifact.recorded` conflicts:** I used a per-process memo rather than use-site ids.
   - The first use of an asset in a trajectory sends the event. Later uses return the same reference (same `event_id`) and send nothing.
   - A use counts once its transaction commits, or once the emitter accepts it when there is no transaction. A use that is rolled back or refused does not count, so the next use sends it.
   - The admin projection is unchanged: artifact records are keyed by artifact id, and the worker already kept only the first event.
2. **Meta sync query bound:**
   - The 1-second catch-up is removed; the task always waits `TRAJECTORY_META_SYNC_SECONDS` between cycles.
   - Each interval sends at most 4 queries, the file_assets rescan included. Every table with work gets one first, so a long rescan never delays the other tables' changed rows.
   - Leftover queries continue tables that returned a full page. A row the emitter refused waits for the next interval.
3. **`update_session` while paused:** it records no `session.settings_changed` while the root session is marked paused.
   - Only a locked write resumes, and its baseline starts from the session as it is then.
   - If this process already reopened the period (a question adoption leaves the paused flag set), recording continues.
   - If the marker can't be read, the write counts as paused and nothing is recorded; the business write still goes through.
   - `update_session` is the only unlocked writer of settings facts. The one in `continuation.py` runs inside the session lock.
4. **Contract 6:** every `recording.state` control has an integer `epoch` next to `state`.
   - A resume carries the epoch of the period it opens, which is also its baseline id suffix.
   - A pause carries that epoch minus one, fixed when the pause is saved.
   - Every report of the same pause or resume carries the same epoch, and each later one is strictly larger. A plain "newer than last applied" rule therefore handles both.
   - A pause is the stored epoch plus one. If a run start has dropped the stored epoch, it uses the current time in milliseconds.
   - Fork and revert can't save a pause, but still report one when recording is off. They send the stored epoch plus one, or 1 if it was dropped, so they can never outrank a later resume.

## Changed files
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-ad61260679fb7e1a4/backend/trajectory/producers.py`
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-ad61260679fb7e1a4/backend/trajectory/artifacts.py`
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-ad61260679fb7e1a4/backend/trajectory/meta_sync.py`
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-ad61260679fb7e1a4/backend/session/session.py`
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-ad61260679fb7e1a4/backend/tests/unit/test_trajectory_producers_session.py`
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-ad61260679fb7e1a4/backend/tests/unit/test_trajectory_producers_capture.py`
- `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-ad61260679fb7e1a4/backend/tests/unit/test_trajectory_meta_sync.py`

No files outside the package.

## Unit tests
- `cd backend && uv run pytest tests/unit -q`: **3158 passed, 0 failed, 41 skipped.** That is the wave-2 3146 plus 12 new tests.
- The three changed test files alone: 44 passed.
- The four new settings and artifact tests fail against the base versions of `session.py` and `artifacts.py`.

## New settings
None. `QUERIES_PER_INTERVAL = 4` is a constant taken from SPEC §5.8. The old `CATCH_UP_SECONDS` constant is removed.

## Deviations
1. **Epoch rule changed from wave 2.** A pause is now the stored epoch plus one, falling back to the clock; wave 2 used `max(previous+1, now_ms)`. The pause and its resume carry different epochs (R−1 and R) so a strict comparison applies both.
2. **Some fork/revert pause reports will be ignored.** After a run start drops the stored epoch, they carry 1, so a worker that has already applied an epoch ignores them. This is safe but no longer records that pause; wave 2's status rule would have applied them.
3. **Shared, larger memo.** The artifact memo shares the per-process memo with recording periods; its size went from 4096 to 16384 entries. Eviction only means a later use may send a harmless duplicate.

## Open issues
1. **For w3-harden-worker (important):** epochs can be millisecond timestamps (about 1.8e12).
   - `session_trajectories.recording_epoch` is a 32-bit `Integer` (`trajectory/store/models.py`, migration `t0001`), which would overflow on PostgreSQL. The last applied epoch needs a BigInteger.
   - The worker must compare against the last applied control's epoch, not its own +1 counter.
2. **Run starts drop the stored epoch.** `question/runtime.py` `_record_run_started` (not owned) rewrites `trace_context` without the recording markers. Keeping `recording_epoch` in that rewrite would make epochs small counters and make fork/revert pause reports effective.
3. **Existing gap:** a pause reported only by fork or revert is never followed by a resume. A worker that applies it stays paused until the next pause and resume.
4. **Artifact deduplication is per process.** Separate processes, or a restart, can still each send one copy, which the worker treats as a keep-first conflict.
5. **Meta sync catch-up is slower by design:** about 2000 rows per 30-second interval.
   - A large first snapshot will take a while; for example, 100k file_assets rows take about 25 minutes.
   - A file_assets table above about 10k rows will rescan continuously, one page per interval.
6. **Some changes made while paused are not recorded.** Title, directory and revert changes aren't in the resume baseline. Titles still reach the worker through meta sync.
7. **Not run:** PostgreSQL variants and integration suites (out of scope, and testing was stopped).
