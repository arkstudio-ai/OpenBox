# Durable ask implementation and acceptance checklist

Scope: implement the agreed human-input lifecycle in the backend, web v2, and
Flutter mobile client without operating another user's production session or
deploying from this task.

- [x] One ask contains 1–4 questions; a single answer accepts the complete set.
- [x] Persist the question, immutable question/content identity, answers, and drafts.
- [x] Waiting for human input releases the run, Redis subscription, and active quota.
- [x] Persist answer and continuation intent in one transaction; replay the known
  continuation, not arbitrary completed tool calls or paid side effects.
- [x] Generic questions, plan-enter approval, and creator-memory approval must work.
- [x] A successfully stored ordinary user message supersedes the old turn's asks;
  disconnects do not. Cancellation and expiry are not implicit approval.
- [x] Fence old runs/answers by session generation; duplicate submissions are
  idempotent and scoped to the authenticated owner.
- [x] Coordinate independent questions: apply individual answers, but resume the model
  only after every pending question in that turn is resolved.
- [x] Reconnect loads authoritative pending state and saved drafts. Web and mobile show
  submitting, failure/retry, expired/superseded states and readonly history.
- [x] Verify backend transactions, restart recovery, cancellation/races, tenant
  isolation, multi-question validation, frontend drafts, event compatibility,
  build/type checks and migrations. No production deployment is implied.

## State and execution rules

- Asking saves a `QuestionCheckpoint` bound to the session generation and its
  original tool part, then raises `QuestionSuspended`. The processor completes
  its bookkeeping and exits the run; the session becomes `waiting_input`.
- Additional independent questions in the same provider batch may be collected.
  Ordinary tools after a suspended ask are recorded as **not executed**, not
  run automatically across the human-input boundary.
- Answering atomically records `answered` (or `rejected` for skip) and sets
  `SessionExecution.resume_pending`. A shared worker applies only the explicit
  generic-question, plan-enter, or memory-proposal continuation, once.
- When every pending question in the generation is resolved, the session is
  `queued`; claiming a quota slot starts a fresh `busy` run using persisted
  history. The worker never re-invokes the earlier tool to recover its result.
- A committed ordinary user message advances the generation and changes old
  unapplied asks to `superseded`, whether the next model output is prose or a
  new ask. Reconnect alone does neither. Cancel, expiry, and skip grant no
  approval. Historical cards are read-only.
- Drafts use compare-and-set revisions. Both clients retain local edits,
  autosave after a debounce, reload server drafts, show failures, and provide
  explicit retry on conflicts. Account-scoped caches prevent cross-user reuse.
- Waiting does not retain an executor coroutine or question-specific Redis
  connection. Only active runs have a heartbeat. Resume, lease, and expiry
  indexes support the shared worker's scans; indefinite waits retain database
  records rather than executing compute. `expires_at` is optional and does not
  imply a default approval.

## Initial verification (2026-09-09, local only)

- `backend/tests/unit/test_durable_questions.py`: 34 tests, passed on both
  SQLite and an isolated PostgreSQL 16 database. The PostgreSQL fixture disables
  the process-local lock so concurrent claims/answers rely on database locks.
  Every case creates the two new tables through the actual Alembic migration;
  column parity and migration downgrade/upgrade are also checked.
- Full loop test runs the real `run_loop`, processor, hooks, question tool,
  persistence, and fresh continuation worker. The external model and sandbox
  are faked, with isolated config, agent/tool catalogue, and system prompt:
  first run exits waiting, a saved answer reaches the second model call,
  then the session finishes idle.
- Tests cover concurrent answers/replacement, duplicate replies, lost leases,
  pre-start redelivery, no automatic replay after progress, quota queueing,
  transient worker failures, owner isolation, immutable questions, memory
  version checks, plan approval, skip, expiry, deletion, and legacy cleanup.
- Related backend gate (durable questions, abort races, turn abort, plan,
  memory, processor outcomes, database readiness): 111 passed.
- Web: `npm run check` passed i18n parity, lint (warnings only), TypeScript,
  and 369 tests; `npm run build` passed. Ask tests include submitted payloads,
  one shared submit, disabled controls, draft recovery/conflict handling,
  stale-event tombstones, and replacement-message success/failure semantics.
- Mobile: `flutter analyze --no-pub` passed; `flutter test --no-pub` passed
  97 tests, including 17 ask-specific widget/API/lifecycle tests. Tests cover
  a 390px-wide three-question card, native cache restoration, server drafts, revisioned HTTP
  payloads, error/retry, 410 removal, duplicate-event protection, account
  switching, and controls staying disabled across virtual-list rebuilds.
  Lifecycle tests also verify that a delayed stop acknowledgement cannot
  remove a newer ask and an older reconnect snapshot cannot overwrite a
  newer refresh; pending reads share an ordering guard across controllers.
- The full backend suite has 1,749 passes and seven failures. Failures concern
  an old computer-tool test context lacking `sandbox_error`, a test importing the
  absent `_desktop_route_preflight`, and five video model/config assertions.
  Their unrelated implementation/configuration was not changed for this task.

## Extended positive/negative regression (2026-09-09)

The follow-up found and fixed stale HTTP snapshots deleting newer WebSocket
asks in both clients, misleading missing-final warnings while waiting/queued,
and clipped mobile-web navigation/composer controls. The expanded results,
state matrix, real-environment evidence and explicit limitations are recorded in
[durable-ask-state-matrix.md](durable-ask-state-matrix.md).

Latest gates: 124 related backend tests; 78 durable-question cases repeated on
PostgreSQL; 384 web unit tests plus check/build; 11 isolated Chromium tests;
104 Flutter tests plus analyzer. All these gates passed. The full backend unit
suite is 1,793 passed / the same seven pre-existing unrelated failures, not all
green. No production rollout was performed.

## Release handoff

Alibaba Cloud backend and web rollout was completed on 2026-09-09 with
`20260909-ask-2183504`. See [DEPLOY.md](../DEPLOY.md) for production config
isolation, the database-copy rehearsal, backups and rollout verification.
The earlier local-only test records remain historical. No mobile app-store
binary was published as part of the Docker rollout.

1. Back up the database and drain/stop **all old-version workers**. Old
   in-memory question waiters cannot coexist safely with the new protocol;
   this is not a mixed-version rolling deployment.
2. Apply Alembic revision `d9e1f3a5b7c2` after `c8e0a2b4d6f1`, then start the
   new backend. Startup closes pre-checkpoint orphan ask parts as expired,
   preserves their history, and requires fresh user confirmation. It does not
   fabricate answers or resurrect old Python coroutines.
3. Release web v2 and the updated mobile build together. This task did not
   publish an app-store build, deploy a server, or access the reported user's
   session in a browser.
4. Smoke-test with a test-owned session: ask → close client → reopen → answer;
   ask → send a new message → old card read-only; three questions → one
   submission; restart a worker while waiting → answer still resumes once.
5. Do not downgrade/drop the checkpoint tables while durable questions or
   accepted answers remain. Coordinate rollback with a database backup and
   explicit cancellation/migration of pending input, not implicit approval.

Safety boundary: a process dying after an execution has made progress must not
blindly replay side-effecting tools. Preserve accepted answers and expose an
interrupted execution for explicit continuation; queued/pre-start work can be
redelivered automatically.
