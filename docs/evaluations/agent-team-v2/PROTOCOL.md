# Agent Team evaluation v2 — preregistration

Registered 2026-09-21 before any v2 evaluation request. This is a separate
round after the v1 pilot and the user's direction to use Qwen/Gemini for normal
testing because Luna frequently returns 429. Historical v1 outcomes, prices,
manifest and protocol remain unchanged. The earlier v5/v6 short arithmetic and
file checks are debug runs, not evaluation cases.

## Frozen changes from v1

- All text roles, including the single Agent and each coordinator, use
  `openai/qwen3.8-flash`, default reasoning. Gemini 3.8 Flash High is excluded
  because its exact configured model has no tariff in the current catalog.
  No fallback or model substitution occurs within this round.
- The current code includes the corrected coordinator capacity count, bounded
  provider failure handling, schema-valid JSON-object string normalization,
  explicit task-action/reason/revision guidance, task revisions in result
  notifications, selected-template instructions and independently scoped
  scheduler context. Record the code snapshot digest before execution.
- The manifest is unchanged: `../agent-team-v1/cases.json`, SHA-256
  `8b1aa2c483c7b2b0042d4fab917928d12a13b076db5a4042d2d652fa8eaa95f6`.
  Twenty text cases (five per category) run in all four groups, for 80 text
  trials. The five media cases, 20 trials, remain separately blocked until
  their real provider, verified price and overall spending ceiling are known.
  They are never replaced by fabricated or uploaded assets.

## Unchanged comparison rules

Use the same case prompt, supplied sources and expected fields in every group.
Fixed uses two saved roles, mixed one saved analyst and one inline reviewer,
automatic chooses up to three temporary workers, and single uses the same
saved analyst in an interactive trial. The text tool allowlist is empty except
mandatory team protocol tools. Do not add Skills or desktop/media tools.

Limits per team: four members including coordinator, two simultaneously active
team Drivers including coordinator, 30 coordinator turns/tasks, 100 messages,
600 seconds, and 2 shadow-billing credits. Role limits remain 30 steps,
4096 output tokens and 600 seconds. Overall case deadline is 900 seconds
including the proposal. Serial execution preserves timing comparability.
Only the initial matching lineup is answered automatically; unanticipated
questions, provider failures and deadlines remain failed observations.
Cancel/abort unfinished fixture work without deleting results or rerunning it.

Reference lines remain unchanged from v1: in at least two categories, mean
quality is no lower than single with shorter elapsed time, or quality improves
by at least ten percentage points; mean workflow cost is at most three times
single; coordinator workflow cost is at most one third of the team total.
Count proposal and all known execution costs, preserve unknown charges, and
report each result alongside means. Five samples per category give direction,
not statistical significance. No duplicate external effect, unauthorized tool,
incorrect successful deliverable or erased unknown cost is acceptable.

Quality combines expected-field checks (60%) with shuffled, group-blinded
evidence/correction/usefulness review (40%, four criteria each 0–2). Unsupported
facts cap the review at 4/8. Missing/failed outputs score zero. Record elapsed
and first-result time, critical dependency path, coordinator/member/retry costs,
idle/wait states, messages, implicit submissions, nudges, stale revisions,
no-progress, capacity backpressure and manual interventions. Unknown evidence
is null, never zero. Setup and abort time are reported separately.

Record model and protocol digests in each receipt. Resuming with another round
or changed protocol is rejected. Report failed reference lines as adjustment
items; no retrospective threshold changes or automatic production enablement.
