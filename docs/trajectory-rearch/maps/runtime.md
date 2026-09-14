# Execution runtime and fencing: current state and re-architecture map

Paths are relative to `backend/` unless they start with `k8s/`, `docker-compose` or `frontend-v2/`.

**Bottom line**
- **Terminal-event bug.** When a user stops or replaces a run, `invalidate_locked` never clears `run_id` or `lease_until`. The same `run.interrupted:{run_id}` fact is then recorded again with a different payload, which raises `IdempotencyConflict`. With recording enabled, one stopped session can:
  - block question expiry and resume for the whole process;
  - make that session reject new prompts, regenerate, delete and a second Stop.
- **Fencing that depends on recording.** Only the recording code stops a stale run from writing chat state or starting LLM requests. With recording off, a superseded run can still commit assistant messages and parts.
- **The fix is cheap.** The fence can move into `question.runtime.transaction`, where the execution row is already locked and loaded, at zero extra SQL per write.

## 0. Constants

| Constant | Value | Where |
|---|---|---|
| Lease length | 60 s | question/runtime.py:19, :177, :209 |
| Heartbeat period | 20 s (60/3) | runtime.py:203 |
| Continuation sweep | 2 s | question/continuation.py:261-267 |
| Queued-resume retry (capacity) | +5 s | runtime.py:168-171 |
| Transient resume retry | +10 s | continuation.py:248-252 |
| Wait before the stop marker | 0.3 s | session/abort.py:41, :153 |
| Unclaimed stop TTL | 30 s | session/status.py:22, :47 |
| Subagent stop grace | 10 s, then cancel | tool/task.py:197-201 |
| Cron job timeout (`wait_for`) | default 1800 s | cron/timer.py:360-362; cron/service.py:339-341 |
| Suggestions timeout / placeholder expiry | 45 s / +15 s | agent/suggestions.py:26, :133 |
| Shutdown abort wait | 30 s | main.py:216-229 |
| Stream checkpoint write | 0.5 s | agent/processor.py:74 |
| Max steps / LLM retries | 200 / 5 | agent/loop.py:500-503, :382 |
| Process model | one uvicorn process, `replicas: 1` | Dockerfile:26; k8s/base.yaml:81 |

## 1. Execution model

### 1.1 `SessionExecution` (db/models/question.py:10-34)
- Schema comes from migrations/d9e1f3a5b7c2_durable_questions.py:19-34; `trace_context` was added by f6a8c0e2b4d6_session_trajectories.py:136.
- It is also listed in the readiness schema (db/base.py:329-333).
- There is one row per session. `execution_locked` creates it lazily, with generation 0, under the session row lock (runtime.py:116-124).

| Field | Meaning | Writers | Readers |
|---|---|---|---|
| `generation` | Turn fence. Only `invalidate_locked` increments it. | runtime.py:290 | `owns` :132; start_run :146, :152; ask question.py:214, :232; `_resolve` :302; list_pending :360; apply_answers continuation.py:101, :149; session.py:514; recorder.py:185; suggestions.py:77; notifications/events.py:167, :171; legacy.py:35 |
| `run_id` | uuid4 of the lease holder; NULL means no holder | set start_run :173. Cleared by finish_run :239, recover :336, delete_session session.py:337. **Not cleared by invalidate_locked.** | is_live :128; owns :132; invalidate :285; cancel :308; recover query :322; recorder.py:186; session.py:515; suggestions.py:77; events.py:73, :134-137, :174, :177; trajectory/jobs.py:61; legacy.py:35 |
| `run_generation` | Generation at the time the current or last run started. Never cleared. | start_run :174 | start_run :154 (a live lease blocks only when it belongs to the current generation); recover :329, :333; invalidate :287; recorder.py:186 |
| `lease_until` | `run_id` is authoritative only while this is in the future | start_run :177; heartbeat :209. Cleared at :240, :337, session.py:338 | is_live :128; recover :322; tick filter continuation.py:225; events.py:135, :174 |
| `run_origin` | `prompt` or `question`. Cron and subagent runs are `prompt`. | start_run :175 | finish_run :232; recover :330; run.started payload :82 |
| `run_progress` | Run has passed its first progress point; decides redelivery | False at start :176; True via `still_current(progress=True)` :196-197 | finish_run :232-238; recover :330 |
| `resume_pending` | Outbox flag: saved answers still need a run | True: `_resolve` question.py:315, finish_run :235, recover :331. False: start_run :178, invalidate :291, continuation.py:153, :242 | start_run :149, :152; waiting_status :224; apply_answers :97; tick query :224; suggestions.py:77; events.py:177 |
| `resume_error` | User-facing reason | runtime.py:238, :340; continuation.py:243; legacy.py:48. Cleared at :179, :292, question.py:316 | recover publish :346-347 |
| `next_attempt_at` | Resume backoff | runtime.py:170; continuation.py:252. Cleared at :180, :293, question.py:317 | tick query continuation.py:226 |
| `trace_context` | TraceContext dict (turn, agent and run identity for the trajectory) | runtime.py:80; question.py:133-135; session.py:722-724 | get_run_trace :47-55; `_record_run_terminal` :93-97; finish_run :251-256; cancel :308-311; session.py:518-527 |
| `updated_at` | Ordering | most transitions | tick order continuation.py:227 |

Indexes: `(resume_pending, lease_until)` and `(lease_until)` (model :31-34).

### 1.2 `RunTicket` and in-process state
- **`RunTicket`.** Frozen `(session_id, user_id, generation, run_id)` (runtime.py:30-35).
  - The `current_run` ContextVar (:38) is set at loop.py:340 and reset at loop.py:1484.
  - Every task created inside the run inherits it: tool exec tasks (processor.py:1052), the title task (loop.py:508), video finalization tasks (tool/video_production.py:1469), and child `run_loop` tasks (task.py:184, which then set their own).
- **`ToolContext.run_id`.** Same id (loop.py:408, :637; tool/tool.py:39), but ToolContext carries no generation.
- **Abort signal.**
  - Signals are in-process `asyncio.Event`s keyed by session (session/status.py:9, :32-50).
  - `trigger_abort` (:53-61) remembers a stop that arrives with no run for 30 s (:16-22).
  - `discard_pending_abort` runs when a non-synthetic message commits (session.py:740-742).
  - `abort_all` runs at shutdown (main.py:216-229).
- **`_trace_run_started`.** In-memory run start times (runtime.py:39, :87, :98).

### 1.3 `generation` vs `run_generation`
- **`generation`** answers "is this turn still current?"
  - A replacement message, regenerate, cancel or delete increments it in `invalidate_locked` (runtime.py:290).
  - The same transaction cancels open questions (:263-284).
- **`run_generation`** answers "which turn was the lease taken for?"
  - It lets `start_run` ignore a live lease from an older turn (:154).
  - It lets recovery redeliver only same-turn question claims (:329-331).
- **"Current" run.** `owns` = `run_id == ticket.run_id and generation == ticket.generation and lease_until > now` (:131-133). After invalidation the generation differs, but `run_id` and `lease_until` stay as they were (:285-295).

### 1.4 Lifecycle transitions

| Transition | Entry points | Gates | Effects in the same transaction | After commit | Trajectory writes (today, inside the business transaction) |
|---|---|---|---|---|---|
| **Start: prompt** | prompt_async api/sessions.py:383-461 (preempts if active :393-398, else `check_concurrent_agents` :399-402); sync send :339-376; regenerate :609-660; plan accept/reject :687-709 / :713-734 (synthetic message, no invalidation); command :826-842 (409 if busy); summarize :756-774. All reach `run_loop` → `start_run` (loop.py:327). | Refuses when: a pending question exists at the current generation; `resume_pending` is set; or a live lease has `run_generation == generation` (runtime.py:144-155) | run fields set; `session.status = busy` (:172-182) | publish `busy` :185 | `turn.started` for pre-rollout contexts :72-77; `run.started` id `run_start:{run_id}` :81-86; `trace_context` saved :80 |
| **Start: question resume** | `_resolve` sets `resume_pending` (question.py:315) → `tick` (continuation.py:218-234) → `apply_answers` (:93-163: tool results, `applied=True`, status `queued`) → `_resume` (:254-259) → `run_loop(expected_generation)` | `expected_generation == generation` and `resume_pending` (runtime.py:151-153). Capacity: lock the user row; if busy/compacting sessions ≥ max, set `queued` and retry in 5 s (:156-171). | same, `run_origin = question` | same | `run.started` with `resume_of_run_id` |
| **Start: cron child** | `wait_for(execute_cron_job)` (cron/executor.py:17-22) → temp session `kind=cron`, `parent_id=job.session_id` (possibly NULL) (:389-421) → synthetic prompt (:476-484) → `run_loop(temp)` (:501) | prompt gates | as prompt | — | CronRun insert + `turn.started` + `job.submitted` in one transaction (:614-653); `agent.spawned` / `job.progress` (:656-668) |
| **Start: subagent** | task tool: child session `parent_id=parent`, `kind=normal` (task.py:37-45) → synthetic prompt under `child_trace` (:70-79) → `_run_child` task (:176-186) | prompt gates on the child's own row | child row starts at generation 0 | — | `agent.spawned` (:66-67); child `run.started` joins the parent trajectory (runtime.py:61-65) |
| **Heartbeat** | loop.py:342 → runtime.py:201-213 | every 20 s: not owner → `abort.set`; any exception → `abort.set` | `lease_until += 60 s` | — | — |
| **Finish** | run_loop `finally` (loop.py:1480) → finish_run (runtime.py:227-257) | not owner → silent return (:230-231) | Interrupted question run with no progress → `resume_pending`; interrupted with progress → failed + `resume_error` (:232-238). `run_id`/`lease` cleared; status via `waiting_status` (:239-243). `task_finished` outbox (:244-246; skipped for child/cron, events.py:38-40). | publish status :257 | `run.finished:{run_id}` (:247-250); `turn_finish:{turn_id}` for root, non-cron, completed runs only (:251-256) |
| **Cancel (user stop)** | POST abort api/sessions.py:738-751; WS `session.abort` api/ws.py:285-294 → `abort_session_turn(user_stop)` (abort.py:125) → `trigger_abort` (:142) → `cancel_session` (runtime.py:306-315) → IDLE (abort.py:148) → 0.3 s wait → marker message + settle todos (:155-166) | `cancel_session` runs even when `was_active` is False (:145-150) | `invalidate_locked("cancelled")`; status `idle` | `question.cancelled`, `idle` | `run.cancel_requested` (random event id) :308-311; `run.interrupted` via invalidate |
| **Preempt** | api/sessions.py:348-351, :393-398, :630-636 → `abort_session_turn(preempted)` | no `cancel_session`; questions stay answerable until the replacement commits (abort.py:143-147) | the following `create_user_message` (session.py:692-693) or `delete_messages_from` (:1071-1074) invalidates | — | — |
| **Supersede** | `invalidate_locked` (runtime.py:260-295). Callers: session.py:693 (non-synthetic message), :1074 (regenerate), runtime.py:312 (cancel), session.py:336 (delete) | caller already holds the session lock | Questions → superseded/cancelled, their parts → error (:263-284); `generation += 1`; resume fields cleared. **`run_id`/`lease_until` untouched.** | `publish_invalidated` (:298-303) | `question.cancelled`; `run.interrupted:{run_id}` (status cancelled, reason = status) (:285-289) |
| **Delete** | `delete_session` (session.py:298-370) | owner lock, including deleted rows | invalidate, then clear `run_id`/`lease` (:335-338); internal parts cleared; `delete_trajectory_in_tx`; `is_deleted` | invalidated questions; `trigger_abort` (:345-347); cron cascade; sandbox release | trajectory rows deleted, tombstone kept (trajectory/lifecycle.py:10-28) |
| **Recover** | `tick` → `recover_expired_runs` (runtime.py:318-349) | `run_id` set and lease expired, rechecked under the lock (:321-328) | Same-turn question claim with no progress → `resume_pending` (:329-331); status `waiting_status(error)`; clear run; if error: `resume_error` + `task_finished` when `run_generation == generation` (:332-343) | status + `session.error EXECUTION_INTERRUPTED` (:344-347). Only `LookupError` is isolated (:348-349). | `run.interrupted:{run_id}` (status unknown, reason lease_expired) (:333-335) |
| **Expire questions** | continuation.py:166-195 | pending and `expires_at <= now` | status `expired`, `applied`; status recomputed only if the lease is not live (:189-190) | publish | `question.cancelled` |
| **Shutdown** | continuation.py:208-216 cancels `_resume` tasks (CancelledError → interrupted); main.py:216-229 `abort_all` (abort path, `interrupted=False`) | — | — | — | abort exits are recorded as `completed` (§3.5 item 5) |

### 1.5 Session status values written by the runtime
- `busy`: runtime.py:182; loop.py:357.
- `compacting`: loop.py:434.
- `queued`: runtime.py:169; continuation.py:157; runtime.py:224.
- `waiting_input`: question.py:249-250; continuation.py:154; runtime.py:223.
- `idle`: finish_run; runtime.py:313; abort.py:148.
- `error`: finish_run; runtime.py:338; continuation.py:244; legacy.py:47; loop.py:1466.
- `retry` is published on the bus only (loop.py:1212-1215), never written to the DB.
- Active statuses: the API treats `busy|compacting` as active (api/sessions.py:20); the WS abort also counts `retry` (api/ws.py:294).

### 1.6 Child sessions: source session vs root session
- **Business link.** `Session.parent_id`.
  - Subagent children are `kind=normal` and hidden in the sidebar.
  - Cron temp sessions are `kind=cron` and listed (session.py:266-275).
  - Each child has its own `SessionExecution`; invalidating the parent never touches children.
- **Trace identity** (trajectory/context.py:8-31):
  - `session_id` is the root trajectory owner.
  - `source_session_id` is the session whose execution row, messages and parts are written.
  - Children are derived with `source_session_id=child` and run/generation cleared (task.py:61-65; executor.py:95).
  - `_record_run_started` adopts an inherited context whose source is the ticket's session (runtime.py:61-65).
- **Checks resolve by source** (recorder.py:184; session.py:509, :525). Ancestry is validated by walking `parent_id` (recorder.py:49-62).
- **`turn.finished` only for root, non-cron runs.** Guard: `context.session_id == ticket.session_id and kind != cron` (runtime.py:254). Test: tests/integration/test_trajectory_agent_loop.py:333-338.
- **Questions.** Subagents cannot ask; cron children can (question.py:200-201).
- **Notifications** skip child and cron sessions (events.py:38-40, :47-49, :72).

## 2. Current fencing inventory

| # | Check | Location | When it runs | Effect on a stale run | Needs recording |
|---|---|---|---|---|---|
| F1 | Start gates | runtime.py:144-171 | `start_run` | no ticket | No |
| F2 | `still_current` at step top | loop.py:413-421 | each iteration, before loading messages | `abort.set`; marks last step `finish=aborted` (update_message_info :418-420); break | No |
| F3 | `still_current(progress)` before request | loop.py:1156-1158 | **after** `create_assistant_message` (:1113) and the step_start part (:1132) have committed | `abort.set`; break | No |
| F4 | Tool pre-auth check | hooks.py:131-133 | before doom-loop/permission checks (direct calls and batch children via batch.py:67) | "Superseded" blocked result | No |
| F5 | Tool start check (progress) | hooks.py:194-196 | after permission, immediately before `execute_fn` | "Superseded" blocked result | No |
| F6 | Heartbeat | runtime.py:201-213 | every 20 s | `abort.set` (also on DB error) | No |
| F7 | Abort races | processor.py:516 with :367-408 (LLM stream); :892-895, :1064-1076 (tool task cancel); :1107-1112; loop.py:1220-1223; task.py:189-201 | continuous, in-process only | stream closed / task cancelled | No |
| F8 | `finish_run` ownership | runtime.py:230-231 | run end | no status, terminal event or notification | No |
| F9 | `set_session_status` ownership | session.py:410-418 | in-run status writes | silently ignored | No |
| F10 | `ask` ownership | question.py:196-203 | durable question creation | `QuestionGone("superseded")` | No |
| F11 | Cleanup gate | loop.py:1446-1449 | before closing this run's open tool parts | cleanup skipped | No |
| F12 | Suggestions `_current` | suggestions.py:76-86 (claim :122-123; settle :98-105) | before claim and after the LLM call | no part / `unavailable` | No |
| F13 | Notification guards | events.py:65-78; :164-178 | on emit and on push delivery | not sent | No |
| F14 | Answer/resume gates | continuation.py:97-98; question.py:275-281, :302-303 | on answer and resume | `QuestionGone` / no resume | No |
| **F15** | **Chat write fence** "Superseded execution" | session.py:509-516, reached via `record_projection_in_tx` :574. Callers: save_part :921; create_assistant_message :800; update_message_info :848/:851; update_part_data :1214; update_session settings :399-401; suggestions; injector. Also create_user_message :684. | Inside the write transaction, **only if** `enabled(user)` **and** a bound TraceContext for that source session has run_id + generation (persist_part binds `ctx.trace_context` processor.py:450-456; final tool save :1103-1104; step scope loop.py:497). Denies when generation differs or another `run_id` holds the row. Ignores lease expiry. | `TrajectoryError`; the write rolls back and the run crashes through the re-raise sites listed below | **Yes** |
| **F16** | **Recorder side-effect fence** `OwnershipError` | recorder.py:182-187 | `record()` of `request.started`, `request.delta`, `tool.started`, `tool.output`, `step.started` when the context has run_id + generation. Compares the source execution's `run_id`/`run_generation`; no lease check. `auxiliary_after_run` (:185) allows purposes title/suggestions when `run_id` is NULL and generation is equal. | `request.started` is written in `RequestCapture.start` before provider I/O (agent/trajectory.py:466-497), so the provider call never happens. `tool.started` (hooks.py:199-201; tool/tool.py:164-169) prevents execution. Chunk/output failures abort the stream or tool. | **Yes** |

**With recording off**
- `record()` returns early (recorder.py:206-212).
- `get_run_trace` returns None (runtime.py:44-46), so no request or step context carries a run id.
- `trajectory_context_in_tx` returns None (session.py:503-504).
- Result: **F15 and F16 do nothing.** A superseded run can commit assistant messages, parts, message info and part updates. Only F2-F11 stop it.

**F15 blind spots**
- It passes when `execution.run_id` is NULL (session.py:514-515).
- So a zombie run whose lease was recovered (run_id cleared, same generation) can still write chat state. F16 would still reject its request and tool starts.

**Not fenced in either mode**
- **`update_session` and its in-run callers.** It is a plain `get_db_session` write (session.py:389-401). Callers: model fallback loop.py:401, agent switch loop.py:524 and processor.py:1124, context loop.py:1088, tokens loop.py:1340, compaction agent/compaction.py:502-508, and `set_session_title`.
- **Todo storage** (session/todo.py:45-58).
- **Internal parts and tool reveals** (session/internal_parts.py:429, :641): they lock the session but never read the execution row.
- **Snapshots** (loop.py:1124, :1263-1287).
- **Cron flush inside the run** (loop.py:1364-1374). Its trace has run_id=None (cron/injector.py:292-293); the recording-off path is four separate transactions (injector.py:220-248).
- **Bus events from stale runs**: deltas (processor.py:575-581) and `SESSION_ERROR` (loop.py:1461-1465).
- **A child run after its parent is superseded.** The child fences only on its own row; the stop reaches it via in-process abort forwarding (task.py:189-201) or the parent heartbeat (≤20 s) plus the 10 s grace.
- **Open tool parts of a superseded run stay open.** Cleanup is skipped (loop.py:1446-1449), and invalidation closes only question parts (runtime.py:280-284).

**Recording fences fail closed by crashing**
- `TrajectoryError` subclasses `ValueError` (trajectory/types.py:34) and is explicitly re-raised at:
  - agent/loop.py:403-405, :1310-1312, :1371-1373, :1416-1418, :1432-1434, :1441-1443, :1457-1459;
  - processor.py:1147-1149, :1166-1168; hooks.py:90-92, :216-218; tool/batch.py:73-75; tool/task.py:170-172;
  - injector.py:117-119, :159-161, :195-197; executor.py:194-198, :311-313, :367-369.

**Cost of each `still_current` today**
- It is one full locking transaction (runtime.py:107-113; internal_parts.py:141-180):
  - in-process guard;
  - SQLite `BEGIN IMMEDIATE`;
  - `SELECT sessions FOR UPDATE`;
  - execution row read;
  - COMMIT.

## 3. Known bug: terminal event `IdempotencyConflict`

### 3.1 Mechanism
- **Event ids.** `_record_run_terminal` uses `f"{event_type}:{run_id}"` (runtime.py:99-103). Callers:
  - finish_run → `run.finished:{id}` (:249-250);
  - invalidate_locked → `run.interrupted:{id}` with status cancelled, reason superseded or cancelled (:285-289);
  - recover → `run.interrupted:{id}` with status unknown, reason lease_expired (:333-335).
- **The payload differs between calls** in four ways:
  - status and reason differ by path;
  - `duration_ms` depends on time;
  - the first terminal pops `_trace_run_started`, so later ones get duration None / `not_recorded` (:98-102);
  - `trace_context` may have been re-saved in between (for example generation at session.py:721-724).
- **The recorder rejects it.** It hashes every field except `event_id`/`occurred_at` (recorder.py:123) and raises `IdempotencyConflict` on an id match with a different hash (:129-134).
  - That error is a `TrajectoryError`, so it passes through `recording_boundary` (types.py:133-134).
  - Recovery catches only `LookupError` (runtime.py:348), so the error is not handled there either.
- **The skip guard rarely applies.** `context.run_id != ticket.run_id → return` (:96-97) only helps when `trace_context` was overwritten.
  - Non-synthetic `create_user_message` does overwrite it with run_id None (session.py:687-688, :721-724), but only **after** calling `invalidate_locked` (:693).
  - `cancel_session` never overwrites it.
  - The stop marker message is synthetic (abort.py:159-165) and re-saves `trace_context` with the **old** run_id (session.py:722-724).

### 3.2 Why the old run stays visible
- `invalidate_locked` leaves `run_id` and `lease_until` set (runtime.py:285-295).
- The superseded run's `finish_run` fails ownership on generation (:132, :230), so it never clears them.
- Its heartbeat is cancelled (loop.py:1471-1472) or exits (runtime.py:206-208).
- The row is cleared only by the next `start_run` (:173-177), by recovery (:336-337) or by delete (session.py:337-338).

### 3.3 Failure sequences (recording enabled for the user, trajectory/config.py:19-20)

| # | Sequence | Result |
|---|---|---|
| A | 1. Stop → cancel records `run.interrupted:R` {cancelled, cancelled, duration X}.<br>2. Marker message keeps run_id R and sets generation G+1.<br>3. Lease expires (≤60 s).<br>4. Recover records `run.interrupted:R` {unknown, lease_expired, None}. | Conflict inside recovery; the transaction rolls back and the row stays. `tick` aborts before `expire_questions` and resume (continuation.py:219-252). **Question expiry and resume stop for every session**, every 2 s, indefinitely. |
| B | After A, before any new run: new message → `invalidate_locked` → `run.interrupted:R` {cancelled, superseded, None}. Same for regenerate (session.py:1074), delete (:336) and a second Stop (runtime.py:312; `cancel_session` runs even when idle, abort.py:145-147). | The prompt is rejected, the session cannot be regenerated or deleted, and Stop returns 500. **The session is unusable.** |
| C | Preempt or regenerate while the old run is still unwinding: invalidate records `run.interrupted:R`, then no new run claims the lease (failure before `start_run`, process exit). `delete_messages_from` does not rewrite `trace_context`. Also: a double regenerate before the new run claims. | Same system-wide stall as A after lease expiry; the second regenerate conflicts. |
| D (same class) | `turn_finish:{turn_id}` (runtime.py:251-256). A turn can hold several runs:<br>- regenerate of a completed reply (api/sessions.py:609-660; `delete_messages_from` keeps the saved turn, session.py:1071-1080);<br>- plan accept/reject (api/sessions.py:699-709, :724-734; synthetic messages keep the turn, session.py:722-724).<br>Triggers when the earlier run in the turn completed with text. | The second completed run's `finish_run` conflicts (run_id differs in context) and rolls back. `run_loop` re-raises from `finally` (only `LookupError` is caught, loop.py:1481). After lease expiry, recovery marks the session **error**, sends `EXECUTION_INTERRUPTED` and a `task_failed` notification for a run that succeeded. |

### 3.4 Same root cause with recording off
After a Stop, the stale lease is later "recovered":
- `session.status` flips to `error`;
- `resume_error` "Execution interrupted…" is set;
- `EXECUTION_INTERRUPTED` is published (runtime.py:332-347).

The user sees this unless a new run claimed the lease first.

### 3.5 Minimal correct fix
1. **Clear the old run when invalidating.** After runtime.py:289, set `execution.run_id = None; execution.lease_until = None`. Keep `run_generation`, `run_origin` and `run_progress` for diagnostics. This makes session.py:337-338 redundant.
2. **Record the terminal event only once per run.**
   - In `_record_run_terminal`, return if `run.finished:{id}` or `run.interrupted:{id}` already exists, or switch to a single `run_terminal:{id}` id with keep-first semantics.
   - Apply the same rule to `turn_finish:{turn_id}` (runtime.py:251-256).
   - This is required because production already has poisoned rows (run_id set plus an existing event).
3. **Isolate candidates in `recover_expired_runs`.**
   - Wrap each candidate (:325-349) in `except Exception: log; continue`.
   - Treat `run_generation != generation` rows (old superseded leases) as silent cleanup: clear run_id and lease, no error status, no publish.
4. **Isolate the phases of `tick`.**
   - Give recovery (continuation.py:219), expiry (:220) and resume candidates (:222-252) their own try/except.
   - Isolate each session inside `expire_questions` (:171-195).
   - Exclude `TrajectoryError` from the permanent-failure `except ValueError` (:237). Today a recording failure turns an accepted answer into `QUESTION_RESUME_FAILED`.
5. **Optional, same change: record aborted runs honestly.** Pass an explicit `aborted` flag to `finish_run`. Abort-signal exits (loop.py:416-421; shutdown main.py:216-229) are currently recorded as `run.finished status=completed` (runtime.py:247-248).

### 3.6 Code that observes `run_id` after invalidation (impact of clearing it)

| Code | Relies on `run_id` staying set? | After the fix |
|---|---|---|
| recover_expired_runs (runtime.py:318-347) | yes: finds superseded leases and marks sessions error | no longer sees them (intended) |
| delete_session (session.py:337-338) | clears it explicitly | redundant |
| `auth_blocked` (events.py:128-138) | emits `platform_auth_expired` for a live run_id even when superseded | stops for superseded runs (intended) |
| `record_job_in_tx` (trajectory/jobs.py:58-65) | flags late results only when run_id differs, so it misses them during a stale lease | flags them immediately (intended) |
| `is_live` gates: question.py:249-250, :320-321; continuation.py:97-98, :189-190, :225, :240 | a stale lease delays status recompute and resume by up to 60 s | immediate |
| `start_run` :154; owns/still_current/heartbeat/finish_run/set_session_status/ask; session.py:514-515; recorder.py:185-186; suggestions.py:77; events.py:164-178; legacy.py:35 | the generation mismatch already decides | unchanged |
| Tests: test_durable_questions.py:216-230 (the new run survives the old finish); test_mobile_notification_events.py:63-89 (cancel + duplicate finish), :107-115 | none of the grepped runtime/question/notification tests asserts run_id persists after invalidation | still pass |

## 4. Recording-independent fencing API (proposal)

### 4.1 API
```python
# question/runtime.py
class RunRevoked(Exception):  # deliberately NOT ValueError/TrajectoryError
    ticket: RunTicket; reason: Literal["superseded", "lease_lost", "deleted"]

@dataclass(frozen=True)
class AuxiliaryTicket:          # title/suggestions: may outlive the run, never the turn
    session_id: str; user_id: str; generation: int; run_id: str; purpose: str
auxiliary_run: ContextVar[AuxiliaryTicket | None]

def write_verdict(execution, ticket) -> str | None     # deny if generation differs or another run_id holds the row
def start_verdict(execution, ticket, now) -> str | None  # strict owns() (lease included)
def assert_current_locked(execution, session_id) -> None  # 0 SQL; checks current_run/auxiliary_run for THIS session only
async def assert_current(boundary: str, *, progress=False) -> None  # <=1 statement, no session row lock
def revoke(run_id, reason) -> None  # in-process revoked set + abort.set(); free check for streaming paths
```
- **Two rules.**
  - Writes use `write_verdict` (today's F15 semantics, now independent of recording).
  - Starts of LLM requests, tools, steps and spawns use strict `owns`.
  - Reason for the looser write rule: detached tasks that inherit `current_run` legitimately write after the run finished. Example: video segment finalization (tool/video_production.py:1464-1477) attaching parts via `_attach_completed` → `save_part` (:804-842).
- **Where the write fence lives.** `transaction(session_id, user_id, *, fence=True)` (runtime.py:106-113) calls `assert_current_locked` right after `execution_locked`. Runtime internals pass `fence=False`:
  - runtime.py `start_run` :141, `still_current` :193, `heartbeat` :205, `finish_run` :229, `cancel_session` :307, recovery :326;
  - continuation.py and question.py worker paths.
- **Progress and liveness in one statement:**
  ```sql
  UPDATE session_executions SET run_progress = true
   WHERE session_id=:s AND user_id=:u AND run_id=:r AND generation=:g AND lease_until > :now
  ```
  Zero rows updated means revoke and raise. The heartbeat uses the same form with `SET lease_until`.
- **Swallowed errors still stop the loop.** `RunRevoked` always calls `revoke()`, so broad handlers that swallow it (task.py:169; injector.py:116-120) still stop the loop at its next boundary.

### 4.2 Insertion points

| Boundary | Insert at | Transaction available | Check | DB cost |
|---|---|---|---|---|
| Chat writes under the lock | runtime.py:112-113. Covers session.py:683, :787, :844, :903, :1206, :412 (keep its silent return) and cron/injector.py:328 | yes: session row `FOR UPDATE`, execution row already loaded | `assert_current_locked` (write rule) | **+0 statements** |
| Session field writes | session.py:393-398 (`update_session`; also tokens/context :437-497, title :427-429) | own transaction, no lock | add `EXISTS(session_executions …)` to the UPDATE predicate when a ticket for this session is bound; 0 rows on an existing session → `RunRevoked` (or ignore for token counters) | +0 round trips |
| Internal parts / reveals | internal_parts.py:429, :641, after `lock_owned_session` | yes | load execution + `assert_current_locked` | +1 PK SELECT (native-provider events only) |
| Step start | loop.py:413-414 (replaces `still_current`) | none | `assert_current("step")` | 1 PK SELECT, no lock (today: 1 locking transaction) |
| Before assistant message | loop.py:1112 | none | revoked-set check (the DB check ran at :414) | 0 |
| Chat request start | loop.py:1156 | none | `assert_current("request", progress=True)` | 1 conditional UPDATE |
| Any LLM request | agent/llm.py:1463 (`stream_llm`, before `UsageMeter.start`); :1526 (`metered_completion`) | none | `assert_current("request")`; aux rule for `billing_kind` title/suggestions; skip DB when already checked this step | 0-1 PK SELECT |
| Paid service submit | agent/trajectory.py:328 (`capture_service_dispatch`, before `RequestCapture.start`) | none | `assert_current("service")` | 1 PK SELECT |
| Tool start | hooks.py:194-196 (keep, reimplemented); hooks.py:131-133 becomes a revoked-set check, with a DB check only after a permission wait | none | `assert_current("tool", progress=True)` | 1 conditional UPDATE per tool (today: 2 locking transactions) |
| Tool streaming output | hooks.py:166-189 | none | revoked-set check before emit and `PART_UPDATED` publish | 0 |
| Tool dispatch loop | processor.py:892-895 | none | revoked set alongside `abort` | 0 |
| Subagent spawn | task.py:86-91, before `_run_child` | none | `assert_current("spawn")` | 1 PK SELECT |
| Cron flush in run | loop.py:1364 | none | `assert_current("flush")`; make the recording-off injection atomic (injector.py:220-248 → one transaction like :328-354) | 1 PK SELECT |
| Heartbeat | runtime.py:205-209 | own | conditional UPDATE of the lease | 1 statement per 20 s |
| Title | loop.py:507-508: bind `AuxiliaryTicket(title)` and clear `current_run` in the task | — | aux rule on the fenced `update_session` | +0 |
| Suggestions | loop.py:1487-1489: bind `AuxiliaryTicket(suggestions)`; `_current` (suggestions.py:76-86) stays the authority | yes | unchanged | +0 |

### 4.3 Per-step cost (recording off today)

| Item | Today | Proposed |
|---|---|---|
| Step-top check (loop.py:414) | 1 locking transaction (~3 statements + commit; SQLite `BEGIN IMMEDIATE`) | 1 PK SELECT |
| Request check (:1156) | 1 locking transaction | 1 conditional UPDATE |
| Per tool (hooks.py:132, :195) | 2 locking transactions | 1 conditional UPDATE + free check |
| End-of-run check (:1448) | 1 locking transaction | 1 PK SELECT |
| Chat writes: :1113, :1132, :1276, :1336; parts processor.py:536, :572, :736, :950, :1104, plus 0.5 s checkpoints | 1 locking transaction each, **no fence** | same transaction, fence +0 |
| `update_session` writes (:1088, :1340; 2 statements each) | unfenced | +0 round trips |
| Heartbeat | 1 locking transaction per 20 s | 1 statement per 20 s |

Recording on also adds, today, per fenced write: identity and ancestry reads, trajectory upsert and lock, seq updates, event insert, synchronous projection (recorder.py:43-172), and the side-effect execution lookup (:183-184). All of that leaves the business path in the target design.

### 4.4 Handling requirements
- **`run_loop`** must treat `RunRevoked` as an abort: no `SESSION_ERROR` (loop.py:1455-1466) and no "failed" step.
- **Hooks and processor.** `wrap_execute` records the tool as cancelled (hooks.py:89-104); `execute_one` returns None as on abort (processor.py:1070-1076).
- **Cleanup ownership.** A revoked run can no longer write `finish=aborted` (loop.py:418-420) or close its open tool parts (loop.py:1389-1419). Recommend `invalidate_locked` closes that run's non-terminal tool parts in its own transaction.

## Migration notes

**What moves where**
- **Fencing.** session.py:509-516 and recorder.py:182-187 (including the aux rule :185) move to `runtime.assert_current(_locked)`. The fence must hold whether recording is on or off.
- **Run facts become emits after commit of the owning transaction:**
  - run.started (runtime.py:81-86); the single run terminal from the ownership change (:99-103);
  - turn.started / turn.finished (:72-77, :251-256; session.py:726); run.cancel_requested (:308-311);
  - question.* and takeover.* (question.py:139-160, :248; continuation.py:120-143);
  - input / message / part / settings facts (session.py:719-735, :800-801, :848-853, :921-923, :1079-1080, :1159-1160, :1214-1216; suggestions.py:112-114, :139-141);
  - cron (executor.py:646-652, :664-668, :718-729; injector.py:266-268, :341-354; cron/recovery.py:55-80);
  - subagent (task.py:66-67, :95-98, :128-138).
- **Durations.** `_trace_run_started` (runtime.py:39) is replaced by the worker deriving durations from `run.started.occurred_at`.
- **Deletion.**
  - `delete_trajectory_in_tx` inside `delete_session` (session.py:340-341) becomes a `session.deleted` emit.
  - The worker must drop late emits for deleted sessions. Today a running loop's `step.finished` hits `OwnershipError` (recorder.py:45-46) and propagates out of loop.py:1473-1479.
- **Baseline and assets.** Reads inside business transactions become emits or worker-side work:
  - session.py:531-561 and :596-637;
  - called from session.py:682/:691, executor.py:623/:632, injector.py:282/:296, revert.py:30-35, fork.py:176/:191.
- **Readiness.** The trajectory tables in the business readiness schema (db/base.py:335-340) move to the worker.

**What breaks or must be ported**
- **Tests that use `TrajectoryError` as the fence:**
  - tests/unit/test_trajectory_session_runtime.py:116-135, plus the aux identity test :166-191;
  - tests/integration/test_trajectory_responses_title.py:183-217;
  - tests/integration/test_trajectory_storage.py:77 (becomes keep-first, no raise);
  - tests/integration/test_trajectory_agent_loop.py:333-338 (counts move to the trace DB).
- **Test fixture migrations.** tests/unit/test_durable_questions.py:58-67 runs the trajectory migration on the business schema. `session_executions.trace_context` and `cron_runs.trace_context` come from that migration (f6a8c0e2b4d6:136-137); keep those two identity columns in business alembic or split the migration.
- **Dead code.** The `TrajectoryError` re-raise sites (§2) become dead. Audit that `RunRevoked` is not swallowed on run paths.
- **Behaviour change with recording off.** Stale-run chat writes that commit today will now fail.

**Risks**
- **Poisoned production rows.** Sessions stopped since recording was enabled already hold run_id plus `run.interrupted:{id}`. The fix must tolerate existing terminal events and clear old superseded leases without flipping those sessions to error (§3.5 items 2-3).
- **Cron child sessions.**
  - Independent execution rows, linked to the notify session (executor.py:417-419).
  - Timeouts cancel the loop, which becomes error when it had progressed (runtime.py:236-238).
  - They can ask durable questions (question.py:200), but executor.py:506-507 then raises and the answer never reaches `cron_runs`.
  - `turn.finished` and notifications are skipped (runtime.py:254; events.py:39).
  - Direct injection checks only status `busy` (injector.py:37).
  - In-run flush is unfenced (loop.py:1364-1374).
- **Subagents.**
  - Parent invalidation does not cascade to children.
  - Stop propagation is in-process only (task.py:189-201), or bounded by the parent heartbeat plus 10 s.
  - Recorder ancestry validation (recorder.py:49-62) leaves the business path, so the worker must validate lineage.
- **Auxiliary work after the run ends.**
  - The title task inherits `current_run` and the step trace (loop.py:497-508) and is not kept in `_background_tasks`. A naive `owns` fence would block every title, because the run finishes first.
  - Today, with recording on: title requests are blocked while a later run in the same generation holds the lease (recorder.py:185 needs run_id NULL); the title write rolls back through session.py:514-516 (unretrieved task error at loop.py:2480-2498).
  - Today, with recording off: the title is always written.
  - Suggestions run with `current_run` already reset (loop.py:1484 before :1489) and rely on `_current`.
  - The policy must be explicit per purpose.
- **Detached late writers** such as video finalization (video_production.py:1464-1477, :804-842) need the looser write rule, and tests.
- **Stale bus events** (deltas, `TOOL_*`, `SESSION_ERROR`) are unfenced; decide whether the revoked set gates publishes.
- **Late observational emits after a terminal are normal:** step.finished (loop.py:1473-1479), tool.finished cancelled (hooks.py:96-103), agent.finished (task.py:95-98). The projector closes open records on `run.interrupted` (projector.py:277-282; repository.py:86-92) and must not reopen them.
- **Fail-open recording vs fail-closed fencing.**
  - Recording must become fail-open. Today it can block `start_run` (runtime.py:183-184), `ask` (question.py:240), `finish_run` (runtime.py:249-256) and `apply_answers` (continuation.py:120-143).
  - Fencing must stay fail-closed: a heartbeat DB error aborts the run (runtime.py:210-213).
- **Scale-out.** One process and one replica today. The worker container does not host runs, but scaling the API needs a DB-driven or bus-relayed abort.
- **SQLite desktop mode.** Every check takes `BEGIN IMMEDIATE` (internal_parts.py:157-161). Spool and worker availability for desktop is unspecified.
- **Pre-existing gaps:**
  - revert (session/revert.py:54) and `delete_failed_turn` (session.py:1097-1163) do not invalidate a live run;
  - permission waits keep the lease alive indefinitely (hooks.py:322-330 with runtime.py:201-209).

**Open questions**
1. Keep `SessionExecution.trace_context`, `QuestionCheckpoint.continuation.trace_context` (question.py:132), `CronRun.trace_context` (executor.py:643) and job `_trajectory_context` (jobs.py:33-34) in the business DB as identity only? `get_run_trace` returns None when recording is disabled (runtime.py:44-46).
2. Lease-expired zombie runs pass the write rule once recovery clears run_id. Is the in-process revoked set enough (single process), or should recovery bump generation or store a revoked run id?
3. Should invalidation cascade to live subagent children, or should each boundary check ancestors (+1 SELECT per level)?
4. Should the invalidating transaction close a revoked run's open tool parts and set `finish=aborted`?
5. Title policy: allow while the generation is unchanged, or allow whenever the title is still empty?
6. Should Stop on an idle session still bump the generation (abort.py:145-147)?

**Tests that must exist**

Keep green with recording on and off:
- test_durable_questions.py:175, :185, :196, :206, :216, :287, :387, :392, :403 (PostgreSQL multi-worker mode via `OBX_QUESTION_TEST_DATABASE_URL`, :28-45), :420, :434, :449, :487, :545;
- test_durable_question_failures.py:306;
- test_suggestions.py:117, :157, :185;
- test_mobile_notification_events.py:63-89, :107-115, :118-141;
- test_trajectory_agent_loop.py:333-338, against the trace DB.

New:
1. With recording off, superseded and lease-lost runs cannot commit `save_part`, `create_assistant_message`, `update_message_info`, `update_part_data`, `update_session` or internal parts (port test_trajectory_session_runtime.py:116-135).
2. Stop → lease expiry → tick raises nothing:
   - the session stays idle;
   - other sessions still expire questions and resume;
   - then prompt, regenerate, delete and a second Stop all succeed, with recording on and off.
3. Exactly one terminal event per run for every ordering of cancel / invalidate / finish / recover / delete (property test), plus recovery of an already poisoned row.
4. Recovery and `expire_questions` isolate a failing candidate; tick phases are isolated; emit failures never cause `QUESTION_RESUME_FAILED`.
5. Two completed runs in one turn (regenerate of a completed reply; plan accept) give one `turn.finished`, and both runs finish cleanly.
6. Request-start fence:
   - no provider call for a revoked run through `stream_llm`, `metered_completion` or `capture_service_dispatch`;
   - title and suggestions allowed after finish in the same generation, denied after a new message.
7. Tool-start fence with recording off, including batch children and `define_tool` tools; no output published after revoke.
8. Late detached writer (video finalization) can still attach its part after a normal finish, and cannot after supersession.
9. Subagent: parent revoked without an in-process abort (simulated second process) → the child stops within the bound, cannot write the parent's tool part, and no child is spawned after revoke.
10. Cron: timeout marks the run interrupted; a revoked run's in-run flush injects nothing; recording-off injection is atomic.
11. Emit fail-open and after-commit semantics: emitter errors or queue overflow never affect start / finish / invalidate / cancel / recover / ask / reply / apply; rollback discards emits (extend test_durable_questions.py:196).
12. `RunRevoked` exits `run_loop` as an abort, without `SESSION_ERROR`; the tool is recorded as cancelled.
13. After clearing run_id in invalidation: `auth_blocked` is not sent for superseded runs, and `operation.late_result` is emitted.