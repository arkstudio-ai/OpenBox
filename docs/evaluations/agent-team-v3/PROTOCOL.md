# Agent Team evaluation v3 — preregistration

**Superseded before execution on 2026-09-21:** the user removed independent
team budgets. No v3 model requests or results exist. The original proposed
protocol below is retained as history; do not execute it against the new code.


Registered 2026-09-21 before any v3 request. This separate round follows the
retained v2 pilot: single completed, fixed and mixed paused because a two-credit
ceiling did not cover two concurrent maximum Qwen requests. Automatic was
interrupted during configuration review and is not a completed observation.
No v1/v2 result, protocol, threshold, or model price is rewritten.

## Frozen differences from v2

The per-team soft credit ceiling is 10 instead of 2. This covers the configured
maximum concurrent request headroom without weakening price bounds, inventing
zero charges, or changing the observed cost calculation. No paid tools are
allowed. The executable code records the failed admission calculation so users
can distinguish actual charges, outstanding reservations and request headroom.
A persisted budget/time pause closes the model step as interrupted, without
creating an artificial provider failure or a regenerate action. Catalog display
metadata and template run counts are also present; these do not alter task
prompts, permission grants, model prices or the grading criteria.

All other rules in [v2](../agent-team-v2/PROTOCOL.md) apply: exact Qwen Flash
model, default reasoning, the unchanged v1 manifest and prompts, all four
groups, 80 text trials, separately blocked 20 real-media trials, serial trials,
600-second team limit and 900-second case deadline, only initial matching
lineup confirmation, unchanged quality/cost/time reference lines and scoring.
Every failure is retained. Only explicit cancellation/abort bounds unfinished
fixture work; no records or resources are deleted. Review scores cannot make
incomplete trials count as successful.

Record the protocol SHA-256, executable-source SHA-256, model and credit ceiling
before execution. Refuse resume if these change. Keep timing observations
separate from deliberate capacity/fault workloads; note any overlap rather than
claiming a controlled production latency benchmark. Limits are experiment
configuration, not a new account purchase or a production rollout decision.
