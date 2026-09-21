# Agent Team evaluation v1 — preregistration

Registered 2026-09-21 before the v1 evaluation requests. The earlier arithmetic,
permission and artifact UI checks are pilot/debug runs and are excluded here.
Their failures remain in the implementation record. This protocol may not be
changed after viewing v1 results; adjustments require a separately named round.

## Scope and comparison

Twenty-five frozen cases: five each of supplied-source research, data analysis,
code analysis/correction, account topics/scripts/storyboards with cross-review,
and preauthorized material generation. Each case runs as single Agent, fixed
team, mixed team and automatic team, for 100 intended executions. The first
80 are text-only controlled fixtures; they do not establish open-web research
or production workload performance. Media cases remain blocked until the
service, verified tariff and total spending ceiling are supplied. They must
not be replaced with uploaded or hand-drawn images and counted as generation.

All text roles use configured `openai/gpt-5.6-luna`, default reasoning, the same
case prompt and source material. Fixed teams use two saved roles; mixed teams
use one saved analyst and one inline reviewer; automatic teams choose up to
three temporary workers. The single Agent uses the same analysis role in an
interactive trial. No text case requires file, desktop, MCP or paid tools;
the tool allowlist is empty except the runtime's required team protocol tools.
Each team has at most three workers, two simultaneous workers, 30 coordinator
turns, 30 tasks, 100 messages, 600 seconds and 2 credits. The overall case
deadline is 900 seconds, including proposal. Cases run serially for timing.

The harness creates a fresh project and root per case/group. Setup of the two
reusable role definitions and templates is reported separately. It confirms
only a matching initial lineup within the frozen model/tool/budget limits.
Unexpected questions, broader lineups, provider failures and timeouts are
retained as non-success outcomes. It never alters a task's answer or fills in
missing outputs. A failed case is not silently retried or discarded.

## Frozen reference lines

- In at least two categories, team quality must be no lower than single Agent
  and elapsed time must be shorter, or quality at least 10 percentage points
  higher. Report category means and every individual result; five samples are
  directional evidence, not statistical significance.
- Mean workflow cost per category must be at most 3× single Agent. Count all
  model and tool usage from initial submission, including lineup proposal.
  Also report the in-run ledger total and its attribution separately.
- Coordinator usage must be at most one third of team workflow cost. Include
  pre-admission root calls as coordinator/setup overhead, not free work.
- No duplicate provider effect, incorrect successful deliverable, erased
  unknown cost, or unauthorized tool use is acceptable.

The quality score combines deterministic expected-field checks (60%) and
blinded review of evidence/correction/usefulness (40%, four criteria scored
0–2 each). Blinded review sees shuffled output IDs without group labels.
An unsupported factual claim caps the review score at 4/8. Missing output,
failed execution or no required generated asset scores zero; unknown cost
remains unknown and cannot pass the cost line. Content cases use their stated
counts, duration and forbidden-claim constraints plus the same blinded review.
Media review checks requested composition, usable file, absence of unrequested
text and consistency with the brief. Quality is not automatically inferred
from a team status of `completed`.

## Evidence retained

The case manifest's SHA-256, configuration/version IDs, group/case/root/run IDs,
submission/confirmation/end timestamps, first accepted result and critical DAG
chain time, complete final answer, graded fields, model/tool ledger rows and
category totals. Record task attempts, explicit/implicit results, nudge and
no_progress counts, stale revisions, capacity backpressure, messages, repeated
exchange notices, idle waits and manual interventions. Preparation and abort
times are separate from model execution.

No-progress and stale-revision counts come from persisted tool results; notices
alone undercount them. A `null` metric means missing evidence, not zero. All
unknown outcomes and costs remain on the original local ledger. Do not change
pricing retrospectively to improve a ratio.

The first pass is descriptive. Any failed reference line creates a concrete
adjustment item (prompt/contract, aggregation window, acceptance or concurrency)
and a new preregistered repeat of the affected categories. Automatic enablement
remains off until the evidence supports it.
