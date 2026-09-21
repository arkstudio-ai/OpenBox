# Bounded random regression sample — 2026-09-21

The user explicitly stopped the exhaustive four-group evaluation, requested
random comparisons and bug fixes, and paused mobile UI optimization. This
sample replaces the remaining full-evaluation delivery requirement. It does
not claim the original M5 quality, cost or latency thresholds have passed.

The v4 batch retains 11 finished observations and one user-interrupted run.
Its protocol, source fingerprint, observations and failures stay unchanged.
The automatic runs exposed a bug: adding a member also compiled an unrelated
default coordinator, rejecting an explicitly allowed Qwen member when the
deployment default was outside the grant. This sample runs after that repair.

Before execution, `selection.json` records an OS-random 64-bit seed. Python's
seeded `random.Random` draws one case uniformly from each previously untested
text category (data, code, content) and shuffles the three team configurations
across them. Each selected team configuration is paired with the same case
and model in the single-Agent configuration. The resulting six observations
are the complete sample; outcomes cannot cause a different random draw.

Use Qwen Flash (`openai/qwen3.8-flash`) with its default reasoning. Reuse the
unchanged v1 case manifest and v4 account-credit execution helpers: serial
execution, no paid tools, no independent team budgets, a 600-second team limit
and 900-second case deadline. Only the initial matching lineup may be confirmed
automatically. Subsequent questions, timeouts and errors are retained as such.
Do not delete any fixture or replace a failed observation with a later pass.
Any targeted retry after a repair is reported separately with its new source
fingerprint. Gemini/Qwen cross-model browser evidence is recorded separately.

Record protocol, selection, executable source and harness digests, account
billing mode and actual usage before/after execution. Missing prices remain
unknown. Compare final outputs, correctness checks, lifecycle and tool errors;
small samples cannot establish general model/team superiority.

Mobile remains at API integration scope. Its visual design and further native
optimization are paused at the user's direction.
