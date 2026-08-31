---
name: video-production
description: Create a complete vertical spoken-person short video from a topic or script, with a shared host, script and segment approvals, Seedance lip-synced speech, segment-level STT review, selective regeneration, and subtitled or clean composition.
allowed-tools:
  - image_gen
  - video_project
  - video_generate
  - video_transcribe
  - video_render
  - creator_context
---

# OpenBox Spoken Video Production

Create a user-supplied or generated-host vertical spoken video from a topic or full
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
   `propose_memory`; its confirmation card is the confirmation. An empty context
   is normal: do not stop the turn after this read.
2. Call `video_project(action="create")` once. Use `mode="standard"` unless the
   user explicitly delegates a bounded end-to-end test.
3. Draft the complete word-for-word script (normally 45–60 seconds at about 3.2
   Chinese characters/second), show it in chat, call `set_script`, then request
   `script` approval in the same turn. A prose-only “if this is okay” question is
   not approval: never end here without the native approval card or an actionable
   tool error. Do not plan segments until that approval passes.
4. Establish one host reference. For a user attachment, read its ready `asset_id`
   from OpenBox attachment metadata; `/workspace/...` is inspection-only and is
   invalid for asset-taking tools. Pass the exact ID once as the project-level
   `character_reference_asset` in `set_segments`; never repeat the host in
   segment `input_assets`. The backend applies that same anchor to every segment:
   - For a supplied person, use the exact user-owned portrait image directly.
   - For a generated or illustrated host, generate it once with `image_gen` if
     needed, then use that resulting image in the same way.
5. Split the approved script at semantic boundaries (five segments is a useful
   30–60 second default; normally ≤40 Chinese characters and never >48; recount
   every line after splitting). Use the same byte-for-byte `visual_anchor`.
   Every spoken prompt uses explicit lint labels: `全片一致的画面基底：<anchor>`,
   `固定镜头`, half-body/medium framing, `自然肢体动作：...`, `语气：...`, speech
   lead immediately followed by `@<exact dialogue>`, and `无字幕`.
   Leave `model` unset unless the user named one. Call `set_segments` once.
6. Show the complete asset list and every segment's exact dialogue and full
   prompt. Request `segments` approval, then `spend` approval. All gates are
   server-enforced; `video_project(status)` reports what is missing.
7. Read active segment IDs, job IDs, and exact idempotency keys from `status`.
   Submit independent planned segments together in one assistant response, each
   with only its project ID, segment ID, and returned key. Poll independent jobs
   together using each job's returned `version` and incremented `wait_iteration`.
   Keep dependent actions for one job ordered. A timeout is normal; never replace
   an ambiguous paid task. On `polling_paused=true`, end the current run, report
   the durable job ID, and resume that exact job only in a later turn; never cancel
   or resubmit. On `recovery_blocked=true`, preserve it for its original route.
8. Transcribe independent completed speech segments together with each exact
   returned key; never transcribe `role="broll"`. Show each video, intended
   dialogue, actual transcript, similarity, and phrase-level verdict before
   requesting `quality` approval. Captions must use accepted actual STT, not
   intended dialogue.
9. Record explicit per-segment feedback before revision. Regenerate only rejected
   or user-selected suspect segments through `revise_segment`; preserve every old
   output and all approved segments. For `revise_segment`, `script_text` is only
   the replacement dialogue of the selected segment, never the full production
   script; `segment_prompt` is the complete prompt for that selected segment and
   must contain `@` immediately followed by the same exact dialogue. Dialogue
   changes reopen the affected approvals.
10. Request `render` approval, submit the key from `status`, and verify audio,
    duration, cleanup, and no remaining process. Hand off the attached MP4; use
    an exact returned `download_url`, never one invented from a path/ID. On an
    explicit recompose, reuse generated segments with the current key—never regenerate.

## Non-negotiable rules

- For any progress or recovery question, call `video_project(action="status")`
  first and continue only from its active IDs, approvals, jobs, and keys.
- A visible plan or chat confirmation is not a hash-bound approval. Editing
  script, prompts, references, or outputs can reopen downstream gates.
- Never retry an ambiguous paid submit or reuse a key with changed content.
- Never use a generated segment as a new character reference, discard historical
  revisions, manually fabricate captions, or claim completion before checks pass.
- `allow_replan` and `replace_character_reference` require explicit user consent.
