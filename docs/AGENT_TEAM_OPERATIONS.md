# Agent Team operations and recovery

This guide describes the implementation on `docs/agent-team-plan`. Release
readiness remains tracked in [the implementation record](AGENT_TEAM_IMPLEMENTATION.md)
and [acceptance matrix](AGENT_TEAM_ACCEPTANCE.md). Local fixture measurements
are not production capacity promises.

## Deployment prerequisites

1. Back up the database and apply the additive Alembic migration through
   `e2b4d6f8a0c1`. The six team/catalog tables and nullable Inbox provenance
   coexist with ordinary sessions. Do not rewrite historical sessions.
2. Deploy a compatible backend with all four gates initially false:
   `TEAM_ADMISSION_ENABLED`, `TEAM_GENERATED_MEMBERS_ENABLED`,
   `TEAM_TOOLS_ENABLED`, `TEAM_UI_ENABLED`. They default to false.
   Keep the existing Agent recovery service enabled; it runs at startup and
   every 15 seconds independently of team admission and Cron.
3. Configure each allowed model's provider, capabilities, reasoning variants,
   context and output limits. These are model execution limits, not a second
   credit wallet. The existing account ledger owns pricing and consumption.
4. `enforce` checks the account's credit balance and debits actual usage exactly
   once, as for ordinary chat. `shadow` records usage without debiting the
   account; the external provider can still charge the deployment account.
   Paid team tools require ledger accounting and explicit tool authorization.
5. Configure Skill snapshots independently of Trace:
   `TEAM_SKILL_BLOB_PROVIDER=local|oss` and, for local storage,
   `TEAM_SKILL_BLOB_LOCAL_PATH`. All API workers must share the same durable
   snapshot store. Local publication is atomic and content addressed; do not
   delete blobs belonging to retained runs. Artifact snapshots use the
   configured owned object store and retain source and immutable asset IDs.

`TEAM_MAX_RUNNING_MEMBERS` defaults to 3 per backend worker across users.
Every actively executing team Driver counts, including its coordinator.
`TEAM_RESERVED_AGENT_SLOTS=2` leaves two per-user Driver slots outside teams,
within `MAX_CONCURRENT_AGENTS` (default 5). The run limit, per-user allowance
and per-process cap are all enforced; a waiting coordinator releases its slot.
These bounds do not reserve CPU, provider quota or database connections.
The default wake merge window is 3 seconds; the default stall interval is
600 seconds. Frozen Agent limits additionally bound steps, output tokens and
wall time. Member wall time covers the entire task attempt, including waits
and cold wakes; coordinator/trial wall time covers each Driver turn.

## Enabling paid tools

`team_delegable_tools` in `openbox.jsonc` is a deployment ceiling, not a user
grant. The member's definition, frozen run grant, live account permissions and
tool adapter must also allow an operation. Skills and MCP descriptions never
create authority.

Use `BILLING_RATES_FILE` to select a reviewed tariff catalog. A media rate is
eligible for strict reservation only when its own record includes
`"price_bound_verified": true`. Before setting it, record the actual service,
contract/source and verification date, account currency and credit conversion,
requested resolution/duration/output-count ceilings, rounding/minimum charges,
and treatment of failed or repeated requests. The per-call quote must bound
the charge for every allowed parameter combination. `TEAM_MAX_MEDIA_SECONDS`
limits bounded media input/output staging; it does not establish a tariff.

The repository image and transcription prices are placeholders. No verified
marker has been added to them. An example of the extra metadata on a reviewed
rate is:

```json
{
  "price_bound_verified": true,
  "source": "Deployment tariff reference and verified parameter limits"
}
```

This is metadata to add to a fully reviewed rate, not a standalone price or
authorization. A user must still confirm the tool's per-call and total run
limits. The five T2 adapters reserve before sending, use a stable effect/job
identity, and settle from existing usage rows using the admitted tariff.
Unknown outcomes retain the full reservation. An actual charge over its
verified bound pauses with `paid_price_bound_exceeded`; do not hide the
discrepancy by altering usage or releasing the reservation.

Every retry through the provider boundary durably invalidates prior
`_team_not_dispatched` evidence before sending, even when Trace is disabled.
The retained `_team_provider_dispatches` counter prevents a stale failure
handler from restoring an old unsent flag. A crash after this marker is
conservatively unknown. Release as unsent only for a failed/cancelled job with
explicit unsent proof, no dispatch count and no provider task identity.

## Diagnosing a run

Use the owner's Team panel and run history first. History is SQL-paginated and
does not replay journals. Team detail and event catch-up are projections of
the append-only journal; Trace is optional diagnostic evidence. Member sessions
are read-only and high-frequency streams require an explicit subscription.

Useful facts to retain in an incident report are the team/root/member IDs,
task and attempt IDs, event watermark, Driver run ID and generation, effect or
job identity, grant/tariff version, and usage IDs. Do not put provider secrets,
signed asset URLs, or full private prompts in general logs.

| State/reason | Action |
| --- | --- |
| `paused`, insufficient credits | Recharge the account, then resume. All teams and ordinary chat use the same account balance; other owners are independent. Legacy budget pauses can resume without adding a team grant. |
| `model_bound_unavailable` | Correct a real model tariff or provider limit. Verify the exact configured model rather than substituting one silently. |
| `agent_wall_time_exceeded` / `wall_time_exceeded` | Inspect the interrupted attempt and external effects before retrying. A retry creates a new attempt with an explicit history. |
| `coordinator_errors` | Inspect the three recorded generations and provider errors. The first two failures enqueue bounded recovery inputs; the third pauses. Provider 429/503 retries are separately bounded and honor Retry-After; a provider quota error differs from a local Driver-capacity refusal. |
| `INVALID_TASK` / `INVALID_TASK_TRANSITION` | Keep the task and previous attempts. Accept/rework only from review, retry from blocked/failed, reopen from succeeded. Supply the latest expected_revision for every coordinator update; retry/rework/reopen/cancel also require a nonempty reason field. A summary is not a reason. |
| `INVALID_TASK_RESULT` | Correct the submitted object to match output_schema. A JSON string containing an object is decoded only when the object validates; no schema or authorization is relaxed. |
| `WORKSPACE_SNAPSHOT_UNAVAILABLE` | Preserve the notice and boundary status. Missing capture is not an empty diff. Inspect sandbox availability and logs; never recreate an old start/end boundary from later files. |
| `PERMISSION_REQUIRES_USER` | The member reports a blocker immediately. The coordinator requests the exact missing scopes in the root confirmation card. File permissions may require all normalized aliases. |
| `outcome_unknown` / `manual_review` | Query the existing provider identity using its adapter. Preserve the reservation; never submit another request just to find out what happened. |
| `completing` | Wait for member/Driver settlement and all model/tool usage. A final summary alone does not close unknown charges. |
| Failed goal | Use explicit `team_finish(status="failed", reason=...)`; retain useful results and original blocked tasks. A blocker report is not successful delivery. |

Cancellation invalidates only the active team's suspended coordinator question,
including saved but unapplied answers. It clears the waiting status before
releasing the root as ordinary chat. Later root questions must not be cancelled.
Inspect the question generation and team session_active flag when diagnosing a
stuck card; do not delete checkpoint rows.

Each new run records a start and end workspace snapshot. A fenced claim is
committed before sandbox I/O; no journal transaction remains open during that
I/O. Closure retains project ownership until capture is ready or explicitly
unavailable. Captures and restores serialize on a sandbox filesystem lock shared
by API workers, covering the whole Git index transaction. A warm capture takes
one sandbox request; remote lock wait is bounded at 20 seconds. Scheduled team
convergence has its own server context, so a released model lease cannot be
inherited by end snapshots or cold wakes. All journal owner/workspace checks
still apply.

The recovery order is Driver/tail reconciliation, Inbox settlement/wake,
external-effect query, then team convergence. A command retry must retain its
idempotency key and request. Cache version 2 replays old/corrupt caches from
schema-1 events; a sequence gap fails visibly instead of guessing. A successful
write refreshes the cache. Do not edit journal rows to repair a cache.

## Verification commands and fault evidence

Run unit tests from `backend` with `.venv/bin/python -m pytest tests/unit -q`.
Run the frontend checks from `frontend-v2` with `npm run check`.
For a migrated isolated PostgreSQL test database:

```sh
TEAM_TEST_DATABASE_URL='postgresql+asyncpg://USER@127.0.0.1:PORT/openbox_team_checks' .venv/bin/python scripts/verify_team_postgres.py
TEAM_TEST_DATABASE_URL='postgresql+asyncpg://USER@127.0.0.1:PORT/openbox_team_checks' .venv/bin/python scripts/verify_team_recovery.py
```

The scripts create unique fixture rows and retain them. They do not delete
existing data or contact model/media providers. The recovery verifier calls
`os._exit(73)` in actual children at crash boundaries, then reads/reconciles from
another process. It advances only its own expired lease timestamps and uses
a recording wake adapter. These are storage/fencing tests, not provider
latency, billing-contract or end-user throughput tests.

| Plan 10.5 fault | Current evidence |
| --- | --- |
| Committed command, lost response | `command_response_lost`: same admission ID on retry, one event set. |
| Exit during provisioning | `provisioning_uncommitted`: session/admission rolls back together; retry creates exactly one member. Current admission has no separately committed provisioning window. |
| Queued message | `queued_message`: one durable Inbox message after recovery. |
| Inbox write before delivered receipt | `inbox_before_receipt`: shared transaction rolls back; retry commits exactly one input/receipt. |
| Claimed input, exited Driver | `claimed_before_driver`: exact takeover preserves original message ID, no second User message, old generation refused. |
| Result before coordinator notification | `member_result_before_notify`: result notification survives and wakes root. |
| Lost executor | Claimed takeover plus paid scenarios; unconfirmed effects become `outcome_unknown`. |
| External send before receipt | `external_receipt_lost`: one recorded send, no redispatch, reservation retained, manual review. |
| Waiting coordinator exits | `coordinator_wait`: durable wait clears when the committed result arrives. |
| Dispatch committed before wake | `dispatch_before_wake`: one attempt/input, recovered wake. |
| Driver capacity exhausted | `driver_capacity`: an actual child reservation hits the cap; its original Inbox and attempt survive backoff and run after the fixture occupant releases. The separate local Qwen measurement retains ordinary-chat headroom at 0–3 members; the two-owner Qwen observation also retained both ordinary-chat slots (see the multiuser record below). |
| Two recovery workers | `recovery_tick`: concurrent children converge on one attempt and one input per task/message. |
| Pause from another process | `cross_process_pause`: a child observes the exact generation's durable abort and releases before `paused`. |
| Owner supplement response lost | `owner_message`: user Inbox and notice commit together; process exit plus retry preserves one original user input. |
| Natural answer without submit | `natural_answer`: one persisted nudge, then implicit review submission. |
| Reserved tool before send | `paid_before_send`: proven unsent effect releases reservation; zero recorded sends. |
| Corrupt cache | `corrupt_cache`: identical replay; next write restores the cache. Missing/stale/gap cases also have unit coverage. |
| WebSocket disconnect | Browser fault proxy closed the actual socket for 30 seconds: CDP closed=1, proxy connection count 2→3, 43 refused requests, journal watermark 59→75. The panel converged without reload and retained one unknown charge. Three-member subscription isolation was subsequently verified with three clients and a non-truncated browser capture; see the capacity record. |

Local single-user capacity evidence is recorded in
[evaluations/agent-team-capacity-20260921.json](evaluations/agent-team-capacity-20260921.json).
Its failed long-task outcome is retained separately from its capacity findings.
One first-token sample per concurrency level does not establish latency percentiles
or a production rollout threshold.

The separate [two-owner observation](evaluations/agent-team-multiuser-20260921.json)
completed both teams (two accepted deliverables each), with both owners active
for approximately 271.691 seconds. All team Drivers, including coordinators,
peaked at 3. Each owner's ordinary chat was accepted with HTTP 200; one sample
per owner produced first text at 8.172/8.584 seconds. Cross-owner reads returned
404 and neither WebSocket received the other root's deltas. Peak measured
PostgreSQL connections were 10. These are local observations, not production
percentiles or an unlimited user-capacity claim.

The user removed independent team credit budgets on 2026-09-21. There is no
maximum-context model headroom check and no team/per-tool credit ceiling.
Historical specs and grants remain immutable and readable, but deprecated
amounts are ignored. Tool permission is `paid_tools: {"image_gen": {"authorized": true}}`.
Account-credit and model-pricing failures persist an actionable team pause;
they do not consume coordinator provider-error retries. Unknown model charges
remain unknown in usage records rather than preventing accepted work from
finishing. Unknown external operations still block task/run completion and
must never be resent or released without evidence.

`team_finish.summary` is persisted as an ordinary final TextPart. The generic
tool wrapper retains the full final response when truncating model-facing
output. The journal records its closing message/tool identities; recovery can
create the same stable Part after a crash, without repeating a model request.
Historical completed receipts recover their final summary in the shared chat
content projection, including old pre-finish prose mislabeled as final.

## Rollout and rollback

Rollout stages are internal fixture accounts, real T0 models, fixed/mixed/auto
teams, exclusive T1 desktop work, verified/preauthorized T2, mobile, then wider
admission. Before each stage, save the tested version/config and thresholds
for task success, duplicate effects, unresolved usage, ordinary-chat first
token latency and 429 count. The complete four-group evaluation and production
capacity baselines are still open; do not widen admission on the strength of
unit-test counts alone.

The supported starting envelope is an internal cohort of at most two concurrently
active owners, three active team Drivers per process including coordinators,
and two ordinary-chat slots reserved per owner. Keep the tested PostgreSQL
configuration and the existing feature gates. Before any wider admission:

- Require zero duplicate external effects, cross-tenant disclosures, over-cap
  Driver reservations or false successful deliveries. Any occurrence stops new
  admission immediately while preserving journals and unknown effects.
- Require all recovery-matrix checks and no unreconciled usage in the candidate
  cohort. Provider 429 is reported separately from local capacity refusal;
  ordinary chats must have zero local-capacity 429s during the trial.
- Collect at least 20 ordinary-chat observations both with and without teams
  before comparing p95. Use a provisional gate of no more than 1.5 times the
  matched unloaded p95 and no more than 15 seconds. Current one-per-condition
  samples cannot establish that gate.
- Apply the preregistered quality/cost reference lines from the full four-group
  evaluation. Missing media prices and missing multi-process load evidence
  remain closed gates. Do not infer permission for production deployment from
  these local tests or from this guide.

To roll back:

1. Set admission false across all API workers; optionally hide the UI. Confirm
   new lineups are refused while existing runs remain readable/recoverable.
2. Keep a compatible backend and its recovery service. Pause, cancel or finish
   existing work according to the owner's intent.
3. Reconcile or explicitly isolate unknown external work and reservations.
   A paused team is still active and still prevents root/project deletion.
4. Before using an older execution binary, verify from journals and Driver
   state that no team attempt is running, no live team Driver remains, and
   unknown work cannot be picked up by the old process. Keep unresolved runs
   on a compatible isolated worker when necessary.
5. Retain all six tables, Inbox provenance, snapshots and reusable definitions.
   Do not run a destructive schema downgrade. Keep compatible read-only
   history available; restore the compatible backend to resume unfinished work.

The rollback rehearsal must record each gate and retained row count in the
acceptance evidence. This guide by itself is not a completed rehearsal.

`verify_team_rollback.py` rehearses the compatible portion against an isolated
loopback PostgreSQL database. At 08:53 on 2026-09-21, admission was refused
without adding a run, a child Driver observed the pause and released, and
`teamtest_ee03aa7a25f04e` was cancelled with its two sessions and input retained.
`teamtest_540e0c794c3a4a` remained paused with a 6-credit unknown reservation and
one manual-review effect. No attempts or team Drivers remained running. The
exact committed pre-team authority loader (SHA-256
`dabea1add40453f1e08b3fd803945669fd11f1bc90eb4f8a42f409bfa252a5a3`)
refused member execution and allowed the closed root as ordinary build.
Migration `e2b4d6f8a0c1` and all fixture rows were retained. This exercised the
legacy authority module. A separate full deployment rehearsal subsequently
passed at 09:13, as described below.

`verify_team_rollback_deployment.py` starts an untouched pre-team checkout's
whole application, startup recovery, HTTP API and executor against a separate
migrated database containing closed run `teamtest_2992241c0f504a`. Its admission
gate was disabled before cancellation; readiness returned 200 and its unknown
team endpoint returned 404. A direct member prompt reached the legacy authority
guard and was refused before any model call. The legacy Driver retains an
expired recovery marker after this refusal; it is not a live executor. The
closed root completed an ordinary build conversation through a local scripted
HTTP model gateway, returning `ROLLBACK_ROOT_OK`. Team events, sessions and the
additive migration were retained. Unknown run `teamtest_540e0c794c3a4a` stayed on
the compatible database, which the old worker never received; its paused state,
journal digest and full reservation were identical before and after. The old
worker then shut down normally. The report distinguishes this complete local
binary rehearsal from production rollout or real-provider performance.

## Grant updates and cost attribution

`POST /api/team-runs/{id}/messages` accepts the owner's `text`, optional owned
`attachments`, and `delivery` (`steer` by default or `followup`) with a stable
`Idempotency-Key`. It always addresses the coordinator and retains ordinary
user provenance. No recipient, sender or synthetic source can be supplied.
The run lock, notice and Inbox share a transaction. Paused teams retain the
input without resuming; closed teams reject new messages but replay existing
receipts. The ordinary root composer remains another user-input entry point.

The owner can adjust an active or paused run through Permissions and execution limits,
backed by `POST /api/team-runs/{id}/grant` with `expected_revision` and a stable
`Idempotency-Key`. It updates only existing delegated tools and explicit scopes;
it cannot add a different tool. Paid-tool removal affects future calls and does not
release an existing reservation. Saving does not resume a paused run.

Usage rows capture `team_attribution` before the model request or paid
reservation. The categories partition the ledger: coordinator, member_work,
rework. Work on attempt number > 1, after a nudge, or during coordinator failure
recovery counts as rework. Historical usage without this metadata remains
unattributed; its total is preserved. An unknown price is never counted as a
known zero. For evaluations, add pre-admission root usage to coordinator/setup
overhead so proposal work is not hidden from cost comparisons.
