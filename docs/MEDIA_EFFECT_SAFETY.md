# Media external-effect safety

Image, video, speech-to-text (STT), TokenSpace materials, and OSS operations
cross process and provider boundaries. A network exception is therefore not
evidence that the remote side did nothing.

The current P0 contract is fail-closed:

- Video cancellation claims the exact job/provider-task state, checks the
  current Agent generation immediately before the remote DELETE, and settles
  the job plus reserved asset under the same run fence. An ambiguous DELETE is
  stored as `outcome_unknown` and is not sent again automatically.
- DashScope STT stores the returned `task_id` in the existing `VideoJob`
  receipt fields before polling. Recovery polls that receipt; a `transcribing`
  row without a receipt becomes `outcome_unknown` instead of being resubmitted
  because it is old.
- Video OSS finalization heartbeats its `finalizing` row independently of
  stream progress. A rejected heartbeat cancels the old transfer before the
  300-second recovery window can create an overlapping upload. A lost PUT
  response is accepted only after OSS `HEAD`, size, and ETag/MD5 reconciliation.
- TokenSpace create paths reserve their existing unique business row before
  POST. Concurrent callers converge using the existing uniqueness constraints
  and row locks. Stable request IDs are correlation tokens only. Because
  TokenSpace does not publish an idempotency guarantee for these APIs, a
  create without a durable provider id is quarantined for manual review.
- Image generation derives a stable operation and output asset/OSS identity
  from the session, tool-call identity, and content fingerprint. A second
  execution that finds a non-ready reservation never makes another paid call.
  OSS PUT/COPY response loss is reconciled by `HEAD`, size, and ETag digest
  where available.
- Third-party HTTP transport failures are classified from the actual request
  origin versus the platform-owned sandbox client origin. Provider/OSS outages
  are not presented as cloud-desktop failures. A `ToolResult` carrying
  `metadata.error=true` always closes as a tool error, never `completed`.

## Durable external-effect ledger

Revision `c6f9a1d3e5b7` adds `external_effects` plus append-only
`external_effect_evidence`. The protocol is implemented in
`backend/agent/effect_ledger.py`:

- `effect_id` and the provider idempotency/correlation key are stable across an
  Agent generation replacement. A request-hash mismatch for the same logical
  identity fails closed.
- `prepare` verifies the exact live `(tenant, session, run, generation)` using
  the database clock. Only an intent proven not to have crossed `submitting`
  may be rebound to a replacement Agent generation.
- Dispatch and reconciliation have a separate monotonically increasing claim
  generation, random token, owner and DB-clock lease. Every mutation is an
  exact CAS. A late old worker cannot record a receipt or projection after
  takeover.
- `submitting` commits before the external request. Recovery never changes it
  back to `prepared` and never invokes a dispatch body. It only queries a
  registered reconciler; no queryable receipt means `manual_review`.
- Receipt plus final projection can be committed in one transaction with a
  domain projector. A projector error rolls the receipt, domain writes and
  terminal state back together.
- Attempts and recovery scans are bounded. Evidence is field-redacted,
  size/depth bounded and Unicode-safe; raw authorization, provider bodies and
  signed URL queries are not persisted.
- The independent Agent recovery service runs one bounded scan at startup and
  on every periodic pass after Driver, Subagent and Inbox convergence.

Image generation is the first end-to-end adapter. Its deterministic
`FileAsset`/OSS identities are recorded in safe context. A complete set of
ready asset rows can reconcile to `succeeded`; the synchronous image endpoint
has no provider handle, so lost response bytes or an incomplete asset
projection become `manual_review`, never a second paid call. `ImageGenCache`
remains only a completed-output reuse index.

## Explicit residual boundaries

The pre-existing TokenSpace, video, STT and OSS guards remain authoritative and
fail closed, but have not been falsely advertised as generic-ledger exact-once:

- TokenSpace liveness/material APIs are also exposed as direct user HTTP APIs,
  outside an Agent Driver generation. Their unique business reservations and
  provider IDs prevent blind re-creation; an ambiguous create without an ID is
  still quarantined. Wiring them into the generic ledger requires a separate
  durable HTTP request fence, not a fabricated Agent run.
- DashScope STT durably stores `task_id` and recovery polls that exact task. A
  receipt-less submission remains `outcome_unknown`. Moving its existing
  receipt callback and `VideoJob` projection into one ledger transaction is a
  future adapter migration, not a prerequisite for the current no-resubmit
  safety property.
- Video submit/cancel and long OSS finalization retain their job-local CAS,
  run-fence checks, heartbeat and HEAD/ETag reconciliation. A future adapter
  may mirror their provider handles into this ledger; current recovery does
  not replay their external request bodies.
