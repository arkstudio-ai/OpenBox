# Workflow gates and recovery

The backend, not the conversation, is the production source of truth. Each
approval stores its user/session evidence and the SHA-256 scope it approved.

| Status | Required next action |
|---|---|
| `init` | set the complete script |
| `needs_script_approval` | show full script; request `script` approval |
| `script_ok` | create/reuse the host reference and set all segments |
| `needs_segments_approval` | show all assets, dialogue, and full prompts; request `segments` |
| `needs_spend_approval` | request the bounded paid-call ceiling |
| `spend_ok` / `generating` | submit or wait only the active segment IDs |
| `needs_segment_revision` | revise only failed/cancelled active segments, then reapprove the plan |
| `generated` | transcribe every active segment |
| `needs_quality_approval` | show videos and STT evidence; request `quality` |
| `needs_render_approval` | request subtitled versus clean output |
| `ready_to_render` | use the returned render idempotency key |
| `delivered` | verify the render resource check and hand off the attached MP4 |

A wholesale `set_script` change deactivates all active segment revisions. A
dialogue change made through `revise_segment` is the selective exception: it
replaces only that ordinal and preserves the other active generated segments.
Changing any prompt, dialogue, reference ordering, host asset, or visual anchor
changes the plan hash and invalidates segment/spend approvals. Changing a
generated output or its STT evidence invalidates quality/render approval.
Historical records remain for audit.

After interruption or restart, call `video_project status`. Never recreate a
production because memory is missing. Provider jobs, WUYING jobs, output assets,
segment revisions, transcripts, and approvals are durable and reconcilable.
The status output includes the active revision's `generation_job_id`; use that
exact value for wait/status/cancel. A job from an older revision is historical
evidence, not the current segment result.

Spend approval includes only `planned` revisions. If any active segment is
submitting, generating, failed, or cancelled, resolve that state first; the
backend will not silently count it as another new paid call.

Approval cards are hard boundaries. If a card is rejected or dismissed, stop
that downstream action. In delegated tests, the tester may make the selections,
but the tool still records the same evidence and budget ceiling.
