---
name: video-production
description: Create a complete vertical spoken-person short video from a topic or script, with a shared host, script and segment approvals, Seedance lip-synced speech, segment-level STT review, selective regeneration, and subtitled or clean composition.
allowed-tools:
  - image_gen
  - video_identity
  - video_project
  - video_generate
  - video_transcribe
  - video_render
  - creator_context
---

# OpenBox Spoken Video Production

Create a real-person or virtual-host vertical spoken video from a topic or full
script. Skills teach the workflow; the build agent's allowlist and permission
rules independently decide tool availability. Keep credentials and provider
calls on the backend, and do not replace the native tools with shell or FFmpeg.

Bundled detail is routed through `references/prompt-recipes.md`,
`workflow-gates.md`, `asset-contract.md`, `quality-and-retries.md`, and
`io-schema.json`. The essential workflow below is self-contained when those
host-side files are not readable from the sandbox.

## Workflow

1. Call `creator_context(action="get_user_context")` before drafting. Apply the
   creator's voice, audience, and boundaries. Propose only a new stable fact via
   `propose_memory`; its confirmation card is the confirmation.
2. Call `video_project(action="create")` once. Use `mode="standard"` unless the
   user explicitly delegates a bounded end-to-end test.
3. Draft the complete word-for-word script (normally 45–60 seconds at about 3.2
   Chinese characters/second), show it in chat, call `set_script`, then request
   `script` approval. Do not plan segments until that approval passes.
4. Establish one host reference; reuse its exact `asset_id` in every segment.
   Pass it once as the project-level `character_reference_asset` in
   `set_segments`; the backend applies that same anchor to every segment:
   - Recognizable real person: use the exact user-owned portrait; run
     `video_identity create → status → add_asset`. Stop for the person's H5/QR
     authorization and continue only when the identity and material are active.
   - AI-generated, illustrated, or virtual host: generate once with `image_gen`
     if needed and use `character_reference_type="virtual"`; never misclassify a
     real person to avoid privacy checks.
5. Split the approved script at semantic boundaries (five segments is a useful
   30–60 second default; normally ≤40 Chinese characters and never >48). Use the
   same byte-for-byte `visual_anchor`. Every spoken prompt must contain all five
   parts: fixed medium/half-body camera; exact visual anchor; speech lead followed
   immediately by `@<exact dialogue>`; restrained gesture plus tone; `无字幕`.
   Call `set_segments` with `character_reference_asset`, the correct host type,
   and `character_identity_id` for a real person.
6. Show the complete asset list and every segment's exact dialogue and full
   prompt. Request `segments` approval, then `spend` approval. All gates are
   server-enforced; `video_project(status)` reports what is missing.
7. Read active segment IDs, job IDs, and exact idempotency keys from `status`.
   Submit each planned segment with only its project ID, segment ID, and returned
   key. Wait sequentially with the returned `version` and incremented
   `wait_iteration`. A timeout is normal; never replace an ambiguous paid task.
   If `recovery_blocked=true`, stop all calls for that job and preserve it for
   recovery on its original provider route.
8. Transcribe every completed speech segment with its exact returned key; never
   transcribe `role="broll"`. Show each video, intended dialogue, actual
   transcript, similarity, and phrase-level verdict before requesting `quality`
   approval. Captions must use accepted actual STT, not intended dialogue.
9. Record explicit per-segment feedback before revision. Regenerate only rejected
   or user-selected suspect segments through `revise_segment`; preserve every old
   output and all approved segments. Dialogue changes require the new exact line
   and a complete lintable prompt, followed by the reopened approvals.
10. Request `render` approval for subtitled or clean output. Read the render key
    from `status`, submit and wait with returned versions, then verify a real audio
    track, consistent duration, `temp_removed=true`, and no remaining job process
    before handing off the attached OSS MP4.

## Non-negotiable rules

- For any progress or recovery question, call `video_project(action="status")`
  first and continue only from its active IDs, approvals, jobs, and keys.
- A visible plan or chat confirmation is not a hash-bound approval. Editing
  script, prompts, references, or outputs can reopen downstream gates.
- Never retry an ambiguous paid submit or reuse a key with changed content.
- Never use a generated segment as a new character reference, discard historical
  revisions, manually fabricate captions, or claim completion before checks pass.
- `allow_replan` and `replace_character_reference` require explicit user consent.
