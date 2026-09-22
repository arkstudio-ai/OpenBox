# Agent Team implementation and verification

> 文档类型：实施 / 验收记录。版本、测试数量和部署状态只对应文内记录时点。

The authoritative scope is [AGENT_TEAM_ARCHITECTURE_PLAN.md](../../plans/agent/AGENT_TEAM_ARCHITECTURE_PLAN.md), as amended by the user's latest instructions below. This record preserves historical checkpoints separately from current delivery requirements.

**Latest user scope adjustment (2026-09-21, later):** the native mobile pause is
lifted. The user asked for the Flutter team screens to be finished against the
Web implementation and the §13 design, then exercised on the iOS simulator. The
scope note below it remains the record of the earlier pause.

**Earlier user scope adjustment (2026-09-21):** pause native mobile optimization;
finish API, backend and Web. Existing Flutter edits remain in the working tree,
but mobile follow-up and additional native builds are paused and do not gate
this delivery. The user also stopped the exhaustive four-group evaluation and
requested random comparisons with bug fixes. Mobile requires API integration;
its interface will be designed by the user. The earlier mobile-inclusive and
full-benchmark checklists below are historical.

### The roster graph on mobile — 2026-09-21 18:20 CST

**This reverses a plan decision at the owner's request.** §13.3, §13.6 and
§13.7 all keep the link graph off narrow screens and prescribe a member list
instead; the Web component itself falls back to a two-column grid below 400px.
The owner asked for the same graph the Web panel draws, so the phone now draws
it and the list is gone. The three sections above are superseded on this point.

The port follows `TeamRosterGraph`: coordinator centred above, members in rows
of up to three, one painter for the lines and ordinary buttons for the nodes —
no graph library, no dragging or zoom. Solid lines are delegation, dashed are
messages, the number on a line opens the messages between that pair, and
selecting a node rings it, highlights its lines and opens the detail card
below. The messages tab gained the pair filter that arrival implies, with a
link to clear it.

Three adjustments the narrower screen forced, none of them visible on the Web:

- Two members in the same row sit side by side, so a straight line between them
  hid behind their cards. Those curves dip below the row instead.
- A pair that both delegates and talks drew its two numbers on top of each
  other at a phone's width; the sideways bow is wider here.
- iOS gives a bare `↔` emoji presentation, which turned a line label into a
  blue badge — the variation selector did not stop it. The chips use icons, and
  the shared `link.message` string now uses `⇄`, which has no emoji form. Web
  renders it the same way.

Verification: **457 mobile tests pass**, analyzer clean, web i18n parity holds.
The device found all three defects above after the layout test passed — the
test font's metrics hid a 3px node overflow, and nothing about emoji
presentation reproduces off-device.

### Mobile roster parity with the Web panel — 2026-09-21 17:40 CST

The phone deliberately shows the roster as a list rather than the link graph
(§13.3, §13.6, §13.7). Comparing the two screens side by side showed that the
list had also silently dropped content the graph is not responsible for:

- **The edges.** §13.3 asks the narrow-screen version to show the same data,
  and the `links` summary was not read at all. Each member card now states who
  delegated to it and who it has exchanged messages with, using the `link.task`
  and `link.message` strings the Web uses on the graph's lines.
- **The detail card's actions.** The Web detail card links to the member's
  current task and to its messages, and offers to save that member to the Agent
  library. All three were missing; the first two switch tabs, as the graph's
  detail card does.
- **另存为团队模板.** The Web panel and progress card both offer it on a
  finished run (§13.2 A6, A33) and the phone offered neither. Both saves now
  run through one bottom sheet — name, and for a template which members to keep
  with a name for each temporary one — carrying an idempotency key per distinct
  payload so a retried tap cannot create a second definition. The sheet ends by
  pointing at the library, since editing happens on the Web (§13.6).

Verification: **457 mobile tests pass**, analyzer clean. The layout regression
gained a finished-run case that opens the save sheet. On the simulator, a real
running team showed the delegation and message counts, the current task link
and the per-member save.

### Native mobile Agent team library — 2026-09-21 17:05 CST

The drawer had no "Agent 团队" entry at all: the `workspace:agentTeams` strings
were copied from the Web sidebar but nothing used them, so the phone could
start a team from the composer yet never see what was saved or what had run.

- The drawer now carries the row between the skill centre and the scheduled
  tasks — the Web sidebar's position — shown only when the deployment sets
  `team_ui_enabled`, exactly as the Web row is.
- Behind it, `AgentTeamsScreen` mirrors `AgentLibraryRoute`'s three tabs: my
  Agents, team templates, run history. Editing stays on the Web (§13.6), so
  the rows report a definition and, for a team, offer to run it; the page
  states where editing happens rather than hiding the capability.
- Run history reads `team_runs` rows directly (§13.8): status chips plus
  project and template filters, and per row the state, whether it needs the
  owner, the pause reason, project, age, task count and credits, with "open
  the team conversation" and "run it again".
- Running a template or a past run picks a project and lands on the empty
  conversation with team mode and the template already selected (§13.4). The
  goal comes back too — the history list does not carry it, so the rerun reads
  the run itself, as the Web route does. The composer gained an `initialText`
  seed for this; it is applied on rebuild as well as at mount, because
  returning to the empty screen reuses its state.

Verification: **453 mobile tests pass**, analyzer clean. `team_layout_test`
gained the library page (three tabs × two languages × two colour modes). On
the simulator: the drawer row, all three tabs against real local data, the
template run flow and a rerun that arrived with its original goal typed.

### Native mobile team UI — 2026-09-21 16:10 CST

The Flutter team surfaces were rebuilt in the app's own design language. The
data layer (`TeamsApi`, `teamRunProvider`, `TeamCollection`) was already sound
and is unchanged; what changed is presentation, and one composer gap it exposed.

- **Composer (§13.2 A1).** The mode picker existed (`showModePicker`) but was
  never wired into the input; the team branch had added a second row above it
  with a `PopupMenuButton` and a `CheckboxListTile`, in neither the composer's
  nor the app's idiom. The mode is now a pill on the composer's existing
  toolbar row, and the app layer contributes the team picker as one more pill
  beside it (web: TeamPicker next to the chat model). `ComposerPill` is shared
  so the injected control cannot drift from the chat feature's own pills. The
  "团队" option needs no client change — it comes from the server's agent list.
- **Team picker sheet.** A bottom sheet matching the model/mode pickers:
  automatic team, saved templates with their descriptions, the
  "允许协调者补充成员" tick (disabled with no roster, as on web) and the
  new `manageOnWeb` line, which is §13.6's explicit statement that Agent and
  template editing lives on the Web.
- **Progress card (§13.7 A4).** Rebuilt on `TaskCardFrame` with the todo card's
  status marks, 4px progress bar and fold. The web card's single heading row
  does not survive 402pt: the counter and roster size moved to their own line
  under the title, outside the fold, so a collapsed card still reports
  progress. Task rows carry owner and state; controls are inline links.
- **Run screen (§13.3, §13.6).** Roster, tasks, messages, results and usage as
  cards; tasks fold open to dependencies, acceptance and attempts, replacing an
  `ExpansionTile` whose `PageStorage` entry collided with nested scrollables.
  No link graph, per §13.6. Usage now formats through the billing page's
  `formatCredits`/`formatTokens` instead of printing the raw Decimal string.
- **Lineup detail, workbench entry, artifacts.** The proposal card's detail
  block, the workbench team row (glyph ◇, same `WorkbenchMenuRow` as the
  built-in surfaces) and the unavailable-artifact row all use tokens and the
  type scale rather than default Material.

Verification: **449 mobile tests pass** (analyzer clean), including a new
`team_layout_test.dart` covering the four surfaces at 402×874 in zh-CN and
en-US, light and dark, at 1.2× type. It caught two real defects — a 134px
overflow in the progress-card heading and a flex split that truncated model
ids — both fixed. The same test writes design-review PNGs when given a font
(`--dart-define=TEAM_UI_PREVIEW_FONT=…`), following the admin layout test.

On the iOS simulator against the local verification backend, signed in as the
local test account: mode → team picker → sheet with the supplement tick; a real
run of the saved fixed template produced the proposal card in the ordinary
QuestionDock, a live progress card (running, then two tasks with owners and
states, then completed) and the final answer as ordinary chat text; the run
screen showed roster, task detail with attempts and structured output, and
usage; the member session opened read-only with the return link; the workbench
showed the team row. Not exercised live: pause/resume/cancel on a running team
(covered by the lost-response regression), the results tab with real file
artifacts, and device-level dark mode and English, which were checked from the
rendered previews rather than on the simulator.

### Completed-team chat replay repair — 2026-09-21 18:01 CST

- Session `session_7YBWYFSCBN4P2G35EWS98JVZJG` had completed its team and
  persisted `47`. Two ordinary greetings at 17:53 failed before a provider
  request: earlier Luna coordination calls used Responses, while the new
  Gemini chat used the LiteLLM dialect. The completed team's coordination
  tools were absent from the new executable catalogue, so cross-dialect
  historical replay raised `canonical tool is unavailable in the current provider binding`.
- Historical replay now retains the fixed built-in coordination names in a
  separate map. Current mappings win; wire collisions and unknown dynamic
  tool mappings remain rejected. Executable tools, schemas and permissions
  are unchanged. Both protocol directions now have regression coverage.
- **63 related tests passed** (`team-history-replay-after.log`), including
  identity isolation, canonical history, completion, ordinary-chat inputs
  and provider failures. The earlier failing replay tests remain recorded.
- After restarting the local backend, the same browser session sent a new
  greeting with Gemini and received `你好！请问有什么我可以帮你的？` in 7.9s.
  Server logs confirm one successful step with zero tool calls. Existing
  failed messages and the completed team were retained.

### Sample completion and final regression — 2026-09-21 14:28 CST

- All six preregistered random comparisons have stopped: **five completed and
  one reached the original 600-second team limit**. The fixed content team
  retained its accepted draft and independent review; a third correction task
  was interrupted. Its final answer remains absent and is not scored as a
  success. The single-Agent content answer retains a strict 4/5 score because
  its price is a string, although its price fact agrees with the source.
  Full answers, costs and limitations are in the [sample report](../../evaluations/agent-team-random-20260921/RESULTS.md).
- After the admission repair, real Qwen automatic run
  `team_01M3188RZY4ATB0EMBPMH6FTBP` confirmed once, added a member, accepted
  three tasks and persisted one complete final TextPart. The mixed data run
  also accepted all three tasks; the browser copied its entire 1,030-character
  final result. Both code outputs passed 104 actual function checks each.
- The final fixed-team sample revealed that incoming peer mail rewrote a busy
  reviewer's state from `running` to `queued` while its Driver kept generating.
  Scheduler input delivery now preserves `running` for active members and
  coordinators; waiting recipients enter `queued`. The four-case regression
  reproduced two failures before the fix; **48 related tests passed** afterward.
  No additional real-model sample ran after this second repair. Original sample
  fingerprint and the later repaired fingerprint remain separately recorded.
- PostgreSQL two-process checks passed on the final scheduler source, including
  dispatch, workspace desktop exclusion, effect reservations, cache replay,
  event sequencing, Inbox deduplication, tenant boundaries and usage fences
  (`postgres-mailbox-final.log`). Frontend **951 tests / 135 files**, i18n/SEO,
  ESLint/TypeScript and production bundling passed; its source is unchanged.
  Mobile **three API-only tests passed**, with interface work still paused.
- The first final backend run recorded **4256 passed, 24 skipped, two failed**
  (`backend-unit-check-17.log`). The failures were a context-stall fixture that
  exited early and a background-worker test that checked its heartbeat before
  waiting for that outcome. The latter now waits for both ingestion and the
  heartbeat. The isolated context-stall case passed; its assertions now report
  the actual exit error before the call count, without changing runtime logic.
  The final full rerun passed **4258 tests, 24 skipped, 91 warnings in 290.22s**
  (`backend-unit-check-18.log`). The earlier context-stall failure did not
  reproduce; its original failed run remains recorded.
- Local backend and Web remain available. All real-model evaluation runners
  have stopped; retained fixtures were not deleted. Mobile integration is
  documented in [API handoff](../../reference/AGENT_TEAM_API_HANDOFF.md). API/backend/Web work
  and the bounded comparison are complete under the latest user scope.

### Bounded regression checkpoint — 2026-09-21 13:45 CST

- Stopped the v4 runner at the user's request. Retained 11 finished observations
  (eight completed, one wall-time pause, two unexpected questions) plus the
  interrupted root `session_7YBWYRX92XYABX5KK0VT96S8Q1`. Its exact run was
  canceled, with no fixture deletion. Missing cost entries stay unknown.
- Two automatic runs exposed an admission bug: `team_member_start` compiled
  a synthetic default coordinator before compiling the requested member.
  A deployment default outside the grant caused `MODEL_NOT_ALLOWED` even for
  explicitly allowed Qwen members. Member preparation now uses the shared
  compiler without resolving/recompiling the already frozen coordinator.
- Regression checks passed **33 tests**, including saved/inline members,
  model overrides, response replay, unchanged coordinator/grant, rejected
  outside models/tools and disabled-member validation. Added schema guidance
  explains that structured input/output must be an object with properties.
- [Random sample protocol](../../evaluations/agent-team-random-20260921/PROTOCOL.md)
  records seed `3443601209790348295`, selecting data-1/mixed,
  code-3/automatic and content-2/fixed, each paired with a single Agent.
  All six use Qwen Flash and unchanged original case facts. The sample is
  executing against a new source fingerprint; it cannot relabel old failures.

### API and Web checkpoint — 2026-09-21 13:28 CST

- Live `/openapi.json` confirms that `TeamPolicy`, `TeamRequest` and
  `GrantChange` expose no independent team credit budget; `PaidToolGrant`
  exposes only `authorized`. Legacy budget fields are ignored on input.
- The Web template editor now exposes the complete configuration of inline
  members, validates it before publishing, and requests saved-member versions
  only when a saved definition is selected. Browser template
  `team_e402ec603d150891431c0c941cc1928fb12d6579c4aa4477f10ae2a8`
  was published from v1 to v2 with a Qwen instruction change. Reload retained
  the change; version comparison showed exactly the changed member roster and
  preserved v1. This QA template is separate from the frozen evaluation batch.
- Frontend complete check: **951 tests / 135 files passed**, plus i18n, SEO,
  ESLint and TypeScript (`frontend-check-20.log`). Production bundling passed
  (`frontend-build-7.log`). The completed Gemini/Qwen result page has no
  current browser error/warning logs.
- The real v4 evaluation started at 12:24:55 CST against unchanged backend
  source. At this checkpoint, ten observations were recorded: seven completed
  with all expected fields, one paused with one unknown-cost entry, and two
  stopped on unexpected questions. All observations are retained. This is
  partial evidence, not a quality/cost/latency pass. The later user reply
  stopped the full comparison and requested the bounded sample above.
- Before the mobile pause, a native iOS read-only walkthrough verified the
  saved-template picker, full final summary, task/attempt details, member-only
  conversation and return to root, and workbench run entry. It exposed and
  fixed a task-expansion PageStorage collision. The last complete native suite
  passed 432 tests; the later task-detail fixes passed three focused tests.
  Additional native verification is paused at the user's request.


## 2026-09-21 user-directed scope update

Independent team budgets and paid-tool monetary caps have been removed. New
schemas expose only tool authorization; legacy limits are readable and ignored.
All members/coordinators use the ordinary account credit ledger, including its
exact settlement and insufficient-credit check. Different teams under one
owner share that balance. Unknown costs remain visible, without becoming a
fabricated zero or blocking already accepted text deliverables.

Final summaries now use canonical final TextParts. A real Gemini/Qwen browser
run exposed that `define_tool` initially dropped the field; its wrapper now
preserves it, and regression tests execute through that wrapper. Historical
receipts also recover correct final prose instead of pre-finish narration.
Crash recovery uses stable closing-message/Part identities from the journal.
The earlier v3 evaluation budget preregistration is superseded before execution;
all earlier observations remain retained. Updated evidence is below/in the
acceptance matrix; this change does not claim the full M5 evaluation is done.


### Verified checkpoint — 2026-09-21 12:22 CST

- Backend full unit suite: 4250 passed, 24 skipped (`backend-unit-check-15.log`).
  The additional v4 evaluation guards passed 8 tests afterward.
- Frontend i18n/SEO/ESLint/TypeScript and 951 tests in 135 files passed
  (`frontend-check-19.log`). An earlier run's unrelated admin polling timeout
  is retained; the full rerun passed without changing that test.
- PostgreSQL two-process scheduling, desktop exclusion, effect reservations,
  replay, Inbox deduplication, tenant boundaries and pending usage fences passed
  (`postgres-account-credit-check-1.log`).
- Browser: original screenshot run now shows its final summary as normal prose;
  template/lineup/card have no independent budget. New Gemini/Qwen run
  `team_01M312V92VPNHYHQW96F25DJND` completed 2/2, displayed 17+29=46 and
  retained exactly one canonical final Part after reload. The earlier real
  run that exposed tool-wrapper field loss is retained in the evidence.
- Evidence: [account and summary receipt](../../evaluations/agent-team-account-summary-20260921.json).
  The v4 account-credit protocol had not yet been executed at this checkpoint;
  execution started at 12:24:55 CST, as recorded above.

## Original work sequence (historical exit criteria)

The original milestone checkboxes below include requirements subsequently
removed by the user. Current API/backend/Web delivery and sampled verification
are recorded in the latest checkpoints, not inferred from these old boxes.

- [ ] M0: runtime binding, typed input provenance, append-only team journal, six tables, dual-database migrations, feature gates, noninteractive members and Inbox integration evidence.
- [ ] M1: confirmed lineup, actual independently executing members, task graph, mailbox, scheduling, recovery, pause/resume/cancel, account credits, chat progress, capacity measurements and first model evaluation.
- [ ] M2: complete versioned Agent library and team templates, compilation, trial sessions, chat proposals, all three member combinations, Skills, saving members/templates and full configuration UI.
- [ ] M3: desktop exclusion, paid-tool reservations/reconciliation, MCP/plugin delegation, immutable Skill resources, recovery/fencing, revocation and deletion constraints.
- [ ] M4: all mockup interactions, workbench graph/tasks/messages/artifacts/usage, run history, accessible responsive Web and mobile flows, event catch-up/subscriptions.
- [ ] M5: acceptance matrix, real-model comparisons, PostgreSQL two-process tests, capacity, operating guide, staged rollout and rollback rehearsal.
- [ ] Start local backend/frontend with password authentication; register a local test account if needed; exercise real user workflows in the browser, fix failures and repeat.

## Reference implementation

The local `/Volumes/fanxiang/workspace2/deepseek-harness/packages/experimental/agent-team/src/` implementation informs the journal, projection, mailbox and dependency validator. OpenBox retains its own SQLAlchemy database, Driver, Inbox, providers, permissions and UI. Team events are separate from model-context events.

## Evidence

Last updated: 2026-09-21. The work is in progress; none of the milestone checkboxes above is a claim that all its exit criteria passed.

### Implemented and exercised

- M0/M1: typed runtime bindings and Inbox provenance, six-table migration, serialized journal commands and replay/cache checks, independent member Drivers, dependency scheduling, durable mailbox, recovery, pause/resume/cancel fences, soft model budget, concurrency headroom and noninteractive permission refusal.
- M2: versioned Agent and template catalog, immutable published versions, frozen trial configuration, compiler/provider/model validation, draft/save/publish UI, template selection in chat, shared durable lineup confirmation, chat `agent_manage` proposals through the same confirmation lifecycle.
- M3 partial: owner-scoped Skill bodies and file lists, byte-preserving resource snapshots, lazy first reads for all-accessible Skills, live user/workspace/member/Skill revocation checks and descriptor-based filesystem confinement. Workspace desktop ownership survives member waiting and unknown effects. MCP service/tool/resource intersections apply during discovery and dispatch, including meta tools; administrator plugin manifests explicitly opt each tool into delegation. Trace recording is not required.
- M4 partial: shared ChatSurface for ordinary chat, trial and read-only member sessions; durable proposal details; progress and accessible roster graph; scoped member subscriptions; tasks/messages/artifacts/usage; SQL-paginated run history with attention ordering, ledger usage, prefilled reruns and terminal-root deletion confirmation. MCP selection and confirmation list exact services and tool/resource patterns.

### Automated evidence

All commands run from `backend` unless specified otherwise. Counts are separate runs, not additive coverage claims.

| Check | Observed result |
| --- | --- |
| Team/core/Driver/Inbox/questions/permission regression set | 163 passed in the earlier implementation checkpoint |
| `pytest tests/unit/test_team_*.py` | 120 passed before Skill/chat-proposal additions |
| Catalog, trials, Skills, runtime, compiler and durable-question set | 70 passed after Skill snapshots |
| `test_team_proposals.py`, `test_team_skills.py`, `test_team_catalog.py` | 19 passed |
| `test_team_runtime.py` | 6 passed, including process-wide member cap across users and ordinary-chat headroom |
| Provider lifecycle/compiler/catalog/trial regression | 50 passed |
| `scripts/verify_team_postgres.py`, isolated PostgreSQL 18 | Two-process dispatch, budget, replay/cache, event sequence, Inbox deduplication, active-NULL uniqueness and tenant isolation passed |
| Frontend TypeScript and modified-component ESLint | Passed at shared-chat/proposal checkpoint; rerun required after later edits |
| Frontend content-view and QuestionDock tests | 37 passed; includes deliberate tool-yield versus incomplete-answer distinction |

### Latest verification checkpoint (2026-09-21, 10:58 local time)

- Real Qwen Flash/Max short run `team_01M30XQRP0TVTT70274R6AB6V0` completed with **3/3 deliverables accepted**. All submitted outputs are schema-bound objects; file bytes are `TEAM_STRUCTURED_QA\nsum=15` (no final newline). The initial proposal used an unsupported `use_template` field and two accepts omitted revisions; the model corrected them. These deviations remain recorded. Template guidance and result-notification revisions have since been clarified.
- That completed run exposed an end-snapshot failure: the fast scheduler inherited its finished model turn's revoked lease/question context. Scheduled convergence now starts with a fresh context while retaining journal owner/workspace checks. A regression tests both completed and cancelled closure under inherited stale credentials. No snapshot has been retroactively invented for the failed historical boundary. Fresh run `team_01M30YFK4KE7KWBTTT53QW4S78` completed **3/3** in 76.685 seconds after admission: the browser opened `snapshot-result.txt` with exactly `TEAM_SNAPSHOT_QA` and `sum=15` (+2/−0). Start/end trees persisted; the root had no failed tool calls.
- Snapshot index operations now hold a sandbox filesystem lock across the full stage/write-tree or restore transaction. Two real subprocesses prove separate versions cannot interleave their index state; paths with spaces are covered. Warm captures remain one remote call. Initializer readiness is per sandbox client, preventing same-path cross-user skips.
- Task-state errors now distinguish a wrong transition from a missing `reason` field. Accept/rework, retry and reopen have explicit state guidance; changes retain attempts and outputs. A submitted JSON-string object is decoded only if it also passes the task's declared object schema; invalid and unstructured strings keep existing semantics. Cancel convergence invalidates only the active team's suspended question generation, including answered/rejected but unapplied cards, without touching later conversation questions. Real browser run `team_01M30YNBC8NY4HCQAMT0M3DCR3` was cancelled while its data-range question was pending: the card and waiting status disappeared, its checkpoint remained `cancelled`/unapplied, and a subsequent ordinary chat returned “取消后可继续”.
- Referenced DSH task-board CAS, queued mailbox receipts and provider retry policy. Injected **429/503** failures exercise real Driver/Inbox/team convergence with provider streams substituted: initial request plus five retries, Retry-After respected, one team blocked while another submits, and coordinator pause after three failed generations without losing accepted work. These are fault injections, not live gateway-429 observations.
- Qwen long run `team_01M30WHH0CMGG6BM8PS9A0HRYN` supplied capacity evidence: three worker Drivers overlapped for an estimated **565.037 seconds**, and total team Drivers including the coordinator never exceeded 3. Ordinary chat returned HTTP 200 at 0/1/2/3 workers with one TTFT sample each: **6.759 / 7.664 / 4.319 / 3.708 seconds**. Separate clients and browser network observations verified aggregate/selected-member stream filtering. See [bounded measurement record](../../evaluations/agent-team-capacity-20260921.json); this is not multiuser or production throughput evidence. The long task itself hit a wall-time limit after structured-result failures and was cancelled; it is not a successful delivery. Its retained 45-line file diff was opened in the browser.
- Gemini 3.8 Flash High is available through the gateway but lacks an exact billing tariff, so its team paused before dispatch. No family-price substitution was made. Normal follow-up testing uses Qwen Flash/Max, per user direction. The earlier Luna failures remain visible.
- Skill editor browser checks passed for missing `read` and for a Skill hint requesting undelegable `question`; the latter shows an explicit capability warning. Member conversations now correctly explain that team-member chats are read-only and direct supplements to the coordinator.
- Full backend run at 10:35: **4,225 passed, 24 skipped, 91 warnings**; latest post-run JSON-result checks: **21 passed**. Current snapshot/runtime/fencing/chat focused checks: **48 passed**. The subsequent complete run passed **4,235 tests, 24 skipped, 91 warnings** in 266.02 seconds, including the final snapshot/context/revision changes. Frontend `npm run check`: **945 tests / 134 files**, TypeScript/i18n/SEO passed, ESLint 0 errors / 43 warnings.
- Combined PostgreSQL recovery verification now passed **all 16 scenarios in one run**, including owner-message crash/retry, actual child-process exits, two recovery workers, cross-process pause and Driver-capacity backoff. Logs retain the prior failing fixture assumptions as well as corrected runs.
- The preregistered v1 pilot has finished all **16 selected text trials**, not the full 100-trial set (80 text + 20 media). Its frozen manifest and failures remain unchanged. Coordinator cost and total-cost reference lines did not pass; blinded rubric review, further adjustments and separate-version follow-up evaluation remain outstanding.

Previous 09:05 checkpoint (historical statements below are superseded by later checkpoints):

- The owner supplement endpoint now shares one transaction with ordinary user Inbox acceptance and the team notice. It requires an idempotency key, rejects member/sender overrides, and queues while paused. Focused owner/Inbox/journal coverage passed **39 tests**; owner/commands/liveness/first-message identity passed **14 tests**. A further HTTP boundary test and metrics checks passed **8 tests**. These are separate runs, not additive suite totals.
- Peer mail received before a first task now includes the member's frozen identity and output directory. The same-task liveness regression ensures unrelated progress cannot erase a stall.
- Provider dispatch now durably invalidates stale unsent proof, independently of Trace. A stale job update cannot release an already-dispatched reservation. **61 paid-tool, adapter, fencing, video-analysis and trajectory tests passed**. Full backend regression remains required after these changes; the last full run is listed below.
- The PostgreSQL recovery verifier's new `owner_message` scenario passed in `teamtest_d134f82f97274e`: a real child exited after commit, and the retry retained one original user input. Together with the previous complete 15-scenario run this covers 16 scenarios; a single combined 16-scenario run has not yet been claimed.
- The compatibility rollback verifier passed on PostgreSQL at 08:53: admission off, cross-process pause, cancel/retention, unknown-cost isolation and the committed legacy authority loader. Unknown fixture `teamtest_540e0c794c3a4a` remains paused with its 6-credit reservation. A complete older application deployment is still being prepared.
- Follow-up at 09:13: `verify_team_rollback_deployment.py` passed with a complete untouched pre-team application, startup recovery and actual HTTP execution against separate PostgreSQL database `openbox_team_rollback2_checks`. Closed run `teamtest_2992241c0f504a` retained its journal and sessions; an attempted member prompt was refused before any provider call, while the ordinary root returned `ROLLBACK_ROOT_OK` through a loopback model fixture. The unknown run remained unchanged on the compatible database. The first attempt had an overly narrow test assertion (expected idle instead of the old executor's expired recovery marker); its rows and failure log are retained. No destructive downgrade, production rollout or real-provider throughput claim is made.
- Real browser WebSocket outage passed: a local fault proxy closed the socket and refused reconnect/API traffic for 30 seconds. CDP observed one close; the proxy observed a fresh connection. Without refreshing, run `team_01M30QAQ7AHZS2VSSTVTAJD4ER` converged from seq 59 to 75, showing cancelled 1/2 tasks, **0.00962316 credits / 93,336 tokens**, and one unknown charge. Earlier HTTP-only offline evidence is excluded. A32 three-member filtering remains open.
- The v1 four-group text pilot is still running against the unchanged 08:05 backend. Research fixed/mixed costs were **51.97× / 40.55×** the single-Agent case, with coordinator shares **56.31% / 56.34%**. Research automatic asked an unexpected amendment; data automatic and code fixed paused after provider failures with one unknown charge each. Failures are retained and these results do not pass M5. The report uses actual journal completion times, distinguishes polling lag, partitions attributed/setup/unattributed costs, and includes critical-path and member-state durations. **3 metrics tests passed** after the timestamp correction. Blinded rubric review and the full task set remain outstanding.

Previous 08:20 checkpoint:

- Full backend `tests/unit`: **4,190 passed, 24 skipped, 91 warnings** (279.11 seconds). Includes runtime fencing, capacity backoff, deterministic liveness, owner grant updates and cost attribution. The task identity fallback was subsequently checked by the PostgreSQL recovery script.
- Frontend `npm run check`: **943 tests / 134 files**, TypeScript, i18n/SEO, ESLint 0 errors / 43 warnings. A later recovered-turn error-display fix passed **46 focused tests**; a final full frontend run is still required after that fix.
- `verify_team_recovery.py`: **15 scenarios passed** on isolated PostgreSQL, including 12 original crash boundaries, actual quota refusal/recovery, two concurrent recovery processes and a child Driver observing a parent-process pause. Fixtures retain rows, simulate no model/provider calls, and advance only their own expired leases.
- Runtime port now exposes capabilities, observe/reconcile snapshots and exact-generation interruption. Capacity refusal retains the existing accepted input and attempt, records 10-second exponential backoff capped at 5 minutes, and clears it on actual turn start. UI shows queued execution honestly.
- Deterministic liveness detects no executable required work, ten-minute stalls, repeated same-task peer exchange and failed dependencies. Persisted notices are deduplicated; legitimate pending questions are excluded.
- Owner-only `POST /api/team-runs/{id}/grant` checks revision and committed costs. The UI updates budget, coordinator turns, wall time and existing operation scopes without automatically resuming. Browser run `team_01M30MNNWW2KPRVFJ0S8TWW1VE` paused at 0.000001 credits, accepted an explicit change to 2 credits / 20 turns / 1200 seconds, remained paused, then completed 1/1 after clicking Continue. Correct results: 6000, 3655, 30.
- The same browser run shows **0.01082824 credits / 68,754 tokens**: coordinator **0.00964652 / 62,983**, member work **0.00118172 / 5,771**, rework zero. Categories are mutually exclusive, stamped before requests/reservations and preserved through settlement. Older rows remain visibly unattributed. A later successful continuation now resolves its visible error banner without erasing the original error message.
- Artifact heading and diff-content visual rechecks passed: the uploaded asset opens as “蓝绿测试图”, and the permission fixture's new file displays exactly two added lines, `TEAM_SCOPE_QA` and `sum=45`.
- Added [A01–A47 evidence matrix](AGENT_TEAM_ACCEPTANCE.md), [operations guide](../../operations/AGENT_TEAM_OPERATIONS.md), and [v1 evaluation preregistration](../../evaluations/agent-team-v1/PROTOCOL.md). The frozen manifest has 25 cases, SHA-256 `8b1aa2c483c7b2b0042d4fab917928d12a13b076db5a4042d2d652fa8eaa95f6`. Real text comparisons have begun. No M5 quality/cost/latency pass is claimed; media pricing/budget clarification remains pending.

Previous 07:28 checkpoint:

- Full backend unit suite at 07:16: **4,173 passed, 24 skipped, 91 warnings** (257.16 seconds). The subsequent verified-price admission changes passed **80 focused tests**; explicit failure/completion, coordinator recovery, journal/cache, execution limits and model-view changes then passed **63 focused tests**. A final full run after those changes is still required.
- Frontend `npm run check` at 07:17 passed: **943 tests in 134 files**, TypeScript, i18n/SEO and ESLint (0 errors, 43 warnings). Later changes only add pause-reason translations. Browser final-result clipboard verification passed.
- Frozen execution settings now enforce output caps on streaming and auxiliary model calls, serialize a member's model requests through usage settlement, apply tool-category ceilings at compile and dispatch time, and enforce task-attempt wall time across cold wakes. Expired work pauses with an explicit reason and preserves unresolved effects and reservations.
- T2 reservation admission now requires a tariff carrying `price_bound_verified: true`. The repository's image and transcription rates remain labeled placeholders; they have not been reclassified as verified. Real-material evaluation needs an actual service, verified upper-bound tariff and evaluation spending ceiling. A user clarification is pending; non-media verification continues independently.
- A failed goal can finish explicitly with `status=failed` and a concrete reason. Successful finish refuses unresolved blockers. Completion waits for unknown model cost as well as outstanding tool effects; repeated polling does not create extra events. Three distinct coordinator execution failures pause the run, with generation-based deduplication and bounded durable retries. Cache format 2 forces old caches to replay without changing the append-only event schema.
- Model-facing reads now paginate full task details and filter before truncation, expose artifact IDs, and restrict private mail by participant. Blank optional task filters normalize to no filter. Structured permission errors retain `metadata.code` across canonical history serialization, preserving the complete permission and alias patterns for a resumed model.
- Isolated PostgreSQL verification passed the live Driver usage-fence and unknown-cost history checks in addition to the existing two-process dispatch/budget/desktop tests. Rerunning against the latest recovery/cache changes is pending.
- Artifact preview and permission-amendment browser workflows passed as detailed below. These are protocol and UI acceptance runs, not the preregistered four-group M5 evaluation.

Previous 06:16 checkpoint:

- Full backend unit suite at 06:00: **4,137 passed, 24 skipped, 91 warnings** (233.26 seconds). After that run, the question/steer recovery change passed **44 canonical-history/Inbox tests**, and fenced model usage passed **68 budget/model-bound/paid-tool/billing tests**. These focused results do not replace a final complete-suite run.
- Frontend `npm run check` at 06:01 passed: **943 tests in 134 files**, TypeScript, i18n/SEO and ESLint (0 errors, 43 warnings). Completed progress cards now provide an explicit final-result copy button; browser clipboard verification is still pending.
- Submitted artifacts are now read from the owned source asset's actual bytes, size-checked, SHA-256 hashed and frozen in a first-writer-only object-store snapshot. A model-supplied digest is optional and, when supplied, must match. Artifact events retain both source and immutable snapshot identities. Owned/foreign assets, false digests, size mismatch and source mutation after freezing are covered by tool-boundary tests. Browser preview against a local object store is still outstanding.
- Model headroom uses each configured provider's explicit context limit, the wire output cap (including reasoning reserve), frozen price catalog and highest applicable cache/peak/long-context rates. This bound depends on the deployment's asserted provider hard input limit. Pending usage is exempt from unpriced blocking only when its stored request fence matches a currently live Driver; expired, replaced, idle, missing or legacy unfenced meters retain unknown cost and block further work. Both SQLite and PostgreSQL verification are required; the new fence checks have passed SQLite unit tests so far.
- Durable amendment answers survive a user pause. Resume requeues saved answers, and each amendment applies in a savepoint: a stale second answer cannot roll back an earlier approved card or leave partial admission. A pending question is a legitimate coordinator wait, not a stall. These cases passed focused recovery tests.
- Browser testing also exposed a question continuation after an Inbox steer using the wrong logical User anchor. New continuation events now keep their parent's durable turn; existing aliases are normalized only from immutable User evidence. Tests cover both new events and the previously persisted alias shape, preserving a live resumed step without replaying its tools.

Previous 05:36 checkpoint:

- Full backend unit suite: **4,113 passed, 24 skipped, 91 warnings** (238.15 seconds). The run includes the shared-root coordinator fix, the independent Skill store and actual HTTP MCP integration. Subsequent model-catalog and operation-scope changes passed **244 focused tests**, and two additional model-facing artifact-submission tests passed separately. The full unit suite predates those last changes.
- Frontend `npm run check` passed again after the operation-scope editor and model catalog changes: i18n, SEO, TypeScript, ESLint (0 errors, 43 existing warnings), **943 tests in 134 files**.
- Ordinary build chats now honor the durable, frozen coordinator binding after lineup confirmation and on cold recovery. Old message mode (`build` or `plan`) cannot strip the coordinator's tools. Dedicated tests also preserve normal unbound chat mode behavior.
- Agent editor and `agent_manage` now use the same public model-capability catalog as the compiler. A model declaring no reasoning variants shows only the default option; unsupported stored values remain visible with an explanation. This was rechecked with Luna in the browser.
- Confirmed operation scopes now pass through `TeamPolicy.permission_rules`, the frozen initial grant and versioned amendments. The model receives the exact missing permission and patterns when a member is refused. Proposal or rejection alone changes nothing; approval grants only the listed operations within already delegated tools; deployment denies still win; an empty confirmed replacement revokes scopes. The editor and lineup card show these scopes. Browser refusal → amendment → retry verification is in progress.
- The model-facing `team_task_update` schema now includes artifact references, which previously existed only in the domain command. Tests exercise the real tool boundary and reject a foreign-project asset atomically. Submitted digests still need authoritative byte verification; browser artifact previews remain outstanding.
- All five T2 tool entry points have adapter-level tests proving that a failed reservation submits no provider work. Billing-key cardinality is validated, and a positively known undispatched failed effect can release its reservation; ambiguous effects retain it. The larger crash/recovery matrix and model-call maximum-cost bound remain outstanding.
- A local TCP HTTP/SSE MCP fixture now exercises the actual tool, SandboxClient, container action server and MCP manager, including bearer-authenticated notifications, more than 40 tools, resource scope and live revocation. The notification header bug was fixed in the container source. This is an integration test, not a claim that a production MCP service was connected.
- Skill blobs use `skill/storage.py`, independent of Trace. Local writes use a synced temporary file and atomic first-writer publication; concurrent writers cannot expose partial bytes or overwrite an existing digest. OSS uses the existing object-store abstraction. Local runtime uses `TEAM_SKILL_BLOB_PROVIDER=local` and retains all earlier test snapshots.

Previous 04:34 checkpoint:

- Current backend regression: **430 passed** across team, billing/media, image/video tools, effect ledger, MCP/plugin/lifecycle, permission, subagent-authority and preference tests. Existing Pydantic deprecation warnings remain.
- Current frontend `npm run check`: i18n parity, SEO, ESLint (0 errors, 43 warnings), TypeScript and **943 tests in 134 files passed**. The first full run exposed an extra config read on admin trajectory pages; `useConfigQuery` now accepts an enable flag and those pages retain their read-only isolation.
- Chat Agent creation supports an atomic batch of one to four definitions, server-side Skill-search evidence, per-conversation limits including automatic creations, per-definition confirmation, default-off T0 autoapproval, and an exact-version undo back to draft. Desktop, paid and MCP definitions always require confirmation; updates also require confirmation. Twenty-four focused preference/proposal tests passed before the additional batch-tool test; the later 38-test catalog/proposal/template/projection set and 430-test regression include it.
- All five T2 tool entry points now call the team reservation boundary before provider submission. Existing usage settlement applies frozen admitted tariffs; reconciliation links existing job/effect and usage records without resubmitting or charging twice. Unknown outcomes retain reservations and block premature task submission. Audio inputs require a finite bounded duration and a bounded staged copy; analysis requires a configured context limit and capped output. Thirteen focused paid-tool/media-limit checks passed. Adapter-specific provider barriers and the complete fault matrix are still outstanding; the model-step headroom remains a configurable estimate, not a proven hard per-step upper bound.
- Progress cards are anchored to the proposing chat turn; workbench Team appears only for a session with a run. Member-change metadata feeds the shared conversation divider. Task details filter attempts by task ID and can resolve beyond the first page. Artifact projections expose only live owned project assets and reuse the chat media renderer through an app-level bridge. Three new projection tests cover pagination, bounded snapshots and live asset ownership.
- Dark and light workbench views passed at desktop and 390px phone widths, including keyboard activation of pair-filtered messages. A dark-mode `a800` token omission made selected confirmation options unreadable; the token now follows the dark foreground and was visually rechecked on the multi-Agent card.

Earlier checkpoints retained for scope and chronology:

- `test_team_*.py`, MCP security, platform plugin/lifecycle, permission monotonic/pattern and subagent-authority regressions: **239 passed** before the final lazy-Skill and MCP-selector changes.
- Skill/catalog/compiler checks after lazy loading and symlink-race protection: **26 passed**.
- Platform plugin/lifecycle checks including explicit administrator delegation: **51 passed**.
- Isolated PostgreSQL two-process verification also covers **two independent projects racing for the same workspace desktop**; exactly one dispatches, and waiting retains ownership. Existing budget, replay, event sequence, Inbox deduplication, active-NULL and tenant checks still pass.
- TypeScript and ESLint passed for the MCP selector and confirmation details. The original npm lockfile and dependencies are retained; no package-manager migration is part of this change.
- Confirmed roster amendments now support additions to a fixed roster and grant-only changes, with version/roster checks and atomic admission. Retirement refuses unresolved attempts and unfinished assignments. Unit tests cover confirmation/rejection/stale roots and replay; browser amendment checks remain outstanding.
- Save-as-template preserves result/input schemas, acceptance mode, resource references, coordinator selection and pinned member versions. Terminal root deletion cascades through team data without deleting reusable definitions; destructive browser verification has not been performed.

### Browser evidence (real model calls, password authentication)

The local backend is `127.0.0.1:8080`, Vite is `127.0.0.1:3000`, PostgreSQL is an isolated cluster on port 55439, and Redis is isolated on port 56379. Password authentication uses a dedicated local test account. No production database, Redis, desktop or object store is used. The test sandbox is a minimal local image, not the complete production desktop image.

1. Agent form save, real trial and publish passed. `本地验收审校员` produced the correct 96-yuan answer through Gemini in trial `trial_cd8945e6909b61bfb0f9065516cc8bec40f596c2e1ffc994e3`.
2. Template autosave, preview, publish and run-to-chat navigation passed. `本地分析审校团队` version 2 uses GPT-5.6 Luna for coordinator and both members.
3. Root `session_7YBX0245WMC7HTNEPBCFH23B98`: confirmed lineup, two independent members, writer followed by dependent reviewer, coordinator acceptance and 2/2 completion all occurred. **Initial delivery quality failed:** the summary lacked the requested financial numbers. The third run below verifies the corrected finish instructions and deliberate-yield handling.
4. Root `session_7YBX0116NFAPQKS4ZYKB7S41N1`: second test reached confirmation, then paused on genuinely unpriced usage after a redundant member request. Terminal member submissions now yield immediately. This run was canceled through the UI, and saved as `复用验收利润团队`.
5. Chat Agent creation exposed a local snapshot-storage path error, which was fixed. `聊天验收利润复核员` was subsequently created and activated through the confirmation card. Its real trial read `team-profit-review/reference.txt` and returned the correct figures. An initial blank resource argument was rejected; blank optional resources now normalize to a Skill-body request and pass unit coverage.
6. The configured DeepSeek route returned provider 503 (no available channel). Gemini Flash High lacks a matching price entry in the existing rates; no price was invented. Priced GPT-5.6 Luna is used for team budget tests.

7. Root `session_7YBWZYTEJA0XDZ543PQ59NBQXE`, run `team_01M301938D7HY4PZJSAB7CK4EE`: **numeric delivery passed**, 3/3 tasks accepted. The final conclusion contains revenue 9,600, gross profit 3,600, operating profit 1,600 and whole-unit break-even quantity 67. The saved template was reused, confirmation occurred before admission, and writer/reviewer executed independently.
8. The third run’s workbench usage and history both show **0.01848584 credits / 259,016 tokens**, split into coordinator 0.0117476 / 221,579, writer 0.00351664 / 22,515, reviewer 0.0032216 / 14,922. These are ledger totals, not just the final assistant message. The canceled second run explicitly shows one unfinished or unpriced usage record.
9. The roster graph passed desktop dark-mode inspection and keyboard navigation to pair-filtered messages. “Run again” opened a new composer prefilled with the same goal and selected template, without starting work automatically.
10. Automatic roster root `session_7YBWZVM2TJ0DWWFH36JD966RPQ`, run `team_01M304VKG6NTR8TS2GS0VDKK6D`: **2/2 accepted and numeric delivery passed**, with the same 9,600 / 3,600 / 1,600 / 67 results. Ledger total **0.00962216 credits / 105,115 tokens**: coordinator 0.00480092 / 83,280; writer 0.00189892 / 10,041; reviewer 0.00292232 / 11,794. Earlier invalid inline-policy attempts prompted clearer schema guidance and typed policy errors; the subsequent mixed proposal used only three tool calls before confirmation.
11. Mixed root `session_7YBWZRQ1VZG5Q188M9N2FFYKJP`, run `team_01M307MGGQK2DZ5DC4P7P1GB1Q`: the card correctly combined the saved reviewer and temporary calculator, preserved the reviewer's Skill and used Luna throughout. **Execution did not pass**: a model response failed without reporting usage, leaving `usage_01M307NN36JG8KM3JV5S6BMXF3` unreported. The run paused instead of treating unknown cost as free and was canceled through the UI. No usage data was rewritten. A fresh mixed execution and retirement check remain necessary.
12. Batch root `session_7YBWZQSNBVMN411ZBJK5RBZ96A`: Skill search and Agent search preceded one `agent_manage` call; four confirmation pages appeared. The user-facing browser workflow selected and submitted at 390px width. Database evidence confirms `本地批量验收资料员` and `本地批量验收审校员` active, `本地批量验收计算员` and `本地批量验收摘要员` draft. No file or team run was created by this request.
13. Autoapproval/undo root `session_7YBWZQEGVKR49YG1BETDXR7HXS`: two T0 definitions were automatically enabled after the preference was explicitly enabled in the local test workflow. Undo returned `本地撤销复验甲` to draft while `本地撤销复验乙` stayed active, including after reload. The preference was turned off again at 04:46.
14. Mixed root `session_7YBWZNF0AVFFAWYNMCRR1RCB84`, run `team_01M30AR6W86BQT3RHRMK5QHTY4`: saved Skill-backed reviewer plus temporary calculator, **2/2 tasks accepted**, correct revenue 13,500, contribution 5,400, operating profit 2,900 and break-even quantity 70. The calculator retired after acceptance; its conversation divider and roster state survived reload. Execution lasted 123.887 seconds after confirmation. Total **0.02018288 credits / 274,794 tokens / 25 model calls**; coordinator **0.01334024 / 224,036 / 14**. Coordinator cost is about 66%, exceeding the evaluation reference line; this protocol success is not an M5 efficiency pass.
15. At 390×844, light/dark Agent lists, basic/advanced editor, template inspection and run history were exercised. Editor document width equals viewport width (390). Template configuration check passed, history navigated to the correct root Team panel, and source labels no longer expose a translation key. Actual artifact media preview subsequently passed in run 17 below.
16. Permission/amendment root `session_7YBWZM8T8EJR9FQ2E788KZSX96`, run `team_01M30C2JF8H66QGCGT7W64EXW1`: initial write refusal, a confirmed narrow grant and one added reviewer were visible. **End-to-end execution failed**: the model omitted relative aliases for the same file; subsequent ordinary-question recovery exposed the logical-anchor bug above. The run was canceled through the UI at 06:15 after exceeding its original 30-minute window; all history and unknown usage were retained. A fresh run is being tested with the fixes. This is not a passed amendment/retry acceptance test.
17. Artifact root `session_7YBWZFXZQ45SP1FJK21SPAX6KC`, run `team_01M30G4RX68A0Y9V2KJEB5H0N2`: **1/1 accepted**, completed at 06:58:41. An uploaded synthetic 120×80 PNG was submitted as artifact `tart_01M30G6QGJBTZZ714EG71TW9MM`; source asset and immutable snapshot asset are distinct. SHA-256 is `4aa3fba7e8f76d82869188a256a24d842e4d1542fdd547c73d5a4389184c4720`. The browser opened the actual 264-byte blue/green image, confirmed its natural dimensions, and reopened it after reload. This run included a model correction and installing the missing `file` utility in the private QA sandbox; it is not an intervention-free benchmark. The corrected uploaded-file heading was visually rechecked and passed at 07:28.
18. Earlier permission run `team_01M30EAGN1TK6YV5MBVWM0J28P` remains a **failed delivery** despite its historical completed state: the requested file was blocked while a substitute blocker report was accepted. The new finish guard prevents this pattern; historical data was preserved.
19. Fresh permission root `session_7YBWZF3KJ5VDQ679VQZ3JXSE6Z`, run `team_01M30H6C3NR5CPGZK2H0MQKJBF`: **2/2 accepted**, completed at 07:11:36, 240.931 seconds after confirmation. Initial write refusal reported all three exact path aliases without manually supplying them. An approved amendment granted those aliases and added a read-only reviewer; the original task retried, succeeded and was independently reviewed. A read-only byte check confirmed exactly `TEAM_SCOPE_QA\nsum=45\n` (21 bytes) in the member's output directory. The browser showed the reviewer join divider, accepted tasks and the file's +2 diff link. The first lineup's wrong model was corrected before approval; one malformed amendment and three empty-filter errors occurred and are retained as protocol deviations. Empty filters are now normalized. Final visual recheck passed: the diff contains exactly the two requested added lines.

### Earlier outstanding acceptance work (historical)

This list predates the later library, recovery, WebSocket, multiuser and
rollback evidence above. Full four-group evaluation and mobile UI optimization
were subsequently removed from this delivery by the user.

- M2: complete the remaining library/editor/template interaction audit. Operation-scope refusal, approved amendment, original-task retry and reviewer addition now have successful browser evidence, alongside retirement, mixed execution and autoapproval/undo.
- M3: complete recovery/fault-injection matrix, provider-bound/concurrency validation and full deletion/project-retention evidence. Artifact bytes now have authoritative immutable digest snapshots; actual local HTTP MCP integration and adapter-specific provider-barrier tests pass. Unreported real-model usage remains visible and requires genuine reconciliation; it is never changed to zero just to continue a test.
- M4: record the remaining library/editor/history acceptance interactions and actual network-loss/three-stream observations. Actual artifact preview, final-delivery copy, member-change dividers and principal mobile/light/dark screens now have browser evidence. Review-tab audit found only read-only diffs/file links; no review approve/reject mutation controls are rendered there.
- M5: four-group real-model evaluation with success/cost/latency/protocol-deviation data, multiuser capacity measurements, operating/recovery guide and staged rollback rehearsal.
- A01–A47 must each have a recorded result before completion. A successful protocol run alone is not evidence of task success or a throughput benefit.
