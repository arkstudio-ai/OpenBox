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

Turn a topic or supplied script into one real-person vertical spoken video. This
skill is the only place the five media tool schemas are exposed; do not use shell
commands for generation, FFmpeg, Chrome, HyperFrames, uploads, or provider calls.
Credentials remain on the backend and must never appear in prompts or files.
Call these skill-only tools directly after loading the skill. Do not wrap
`video_project`, `video_generate`, `video_transcribe`, or `video_render` in a
generic Batch/parallel tool: the wrapper does not inherit skill-only schemas or
their sequential safety guarantees.

Before planning segments, read [references/prompt-recipes.md](references/prompt-recipes.md).
For gates and recovery read [references/workflow-gates.md](references/workflow-gates.md).
Read [references/asset-contract.md](references/asset-contract.md) when references
are used, and [references/quality-and-retries.md](references/quality-and-retries.md)
before reviewing or regenerating results. The machine contract is
[references/io-schema.json](references/io-schema.json).

## Required workflow

0. Call `creator_context(action="get_user_context")` before drafting anything.
   Use the persona (表达风格/受众/内容定位) to shape the script; treat every 边界
   entry as a hard constraint. When the user states a stable fact or preference
   about themselves or their brand, call
   `creator_context(action="propose_memory", summary=...)` — one third-person
   sentence; the card the user answers IS the confirmation. Never write a
   USER_NOTE via `write_memory`. Session-scoped impressions may be written with
   `write_memory(scope="SHORT_TERM", ttl_seconds=...)`. 宁可漏不可烦: when
   unsure, don't propose, and never re-propose something already confirmed or
   rejected.
1. Call `video_project(action="create")` once. Use `mode="standard"` unless the
   user explicitly delegates a bounded end-to-end test. Delegation changes who
   evaluates the result, not the stored gates or paid-call ceiling.
   After creation, non-create `video_project` actions may omit `production_id`;
   the backend resolves the session's active production. On "not found" never
   retry with a guessed id — call `status` with no id.
2. Draft the complete word-for-word script. Default to 45–60 seconds and about
   3.2 Chinese characters per second. Show the entire script in chat, call
   `video_project(action="set_script")`, then call
   `video_project(action="request_approval", approval_kind="script")`. Do not
   design segments before that card is approved.
3. Establish one host reference. Reuse a suitable user-owned portrait, or call
   `image_gen` once for a clean vertical host portrait and inspect its attached
   result. Reuse that exact `asset_id` across every segment. Never claim identity
   continuity from unrelated text-only generations.
4. Split the approved script at semantic boundaries. Use 5 segments as a useful
   30–60 second default, normally ≤40 Chinese characters each and never >48.
   Write every prompt with the five-part recipe and one identical `visual_anchor`.
   Call `video_project(action="set_segments")`; its server-side lint is final.
5. Show the user the full asset list and, for every segment, the exact dialogue
   and exact complete prompt that will be sent. Then request `segments` approval.
   After it passes, immediately request `spend` approval. The spend card records
   a hash-bound maximum number of new Seedance submissions. Without it,
   `video_generate` rejects every submit.
6. Read the returned segment IDs, current generation job IDs, and idempotency
   keys from `video_project status`.
   Submit each segment with only its `production_id`, `segment_id`, and exact
   `generation_idempotency_key`; do not invent placeholder prompt/model/media
   arguments. The narrow tool schema intentionally omits those fields. The
   backend supplies the approved prompt,
   references, `duration=-1`, `ratio=9:16`, `resolution=720p`, generated audio,
   and no watermark. Wait on each returned job; never create a replacement merely
   because the provider is slow.
7. For every completed segment, submit `video_transcribe` with its exact
   `transcription_idempotency_key`, then wait. The WUYING queue extracts a mono
   MP3 with FFmpeg; the backend runs STT, persists actual spoken text, similarity,
   and phrase-level omissions/replacements. Show all segment video attachments
   plus each script/transcript/verdict. Request `quality` approval only after all
   active segments have STT evidence.
8. When the user gives per-segment verdicts in chat, first record each explicit
   verdict with `video_project(action="set_segment_feedback", segment_id=...,
   feedback="approved"|"rejected", feedback_note=...)` (`feedback_note` is
   required on reject — it becomes the revision rationale). Then regenerate
   ONLY segments the user rejected (`review_status=user_rejected`) or
   STT-suspect segments the user chose to redo; never revise an approved
   segment. If the user chooses to rework suspect segments, call
   `video_project(action="revise_segment")` only for those segments. This creates
   a new revision while preserving the paid old result. For another take with
   identical words, pass only `segment_id` and `revision_reason`. If the dialogue
   changes, pass the new word-for-word `script_text` and a complete lintable
   `segment_prompt`; this atomically updates that one segment and the full script
   while keeping every other active generated segment. Never use `set_script` or
   `set_segments` as a workaround for a selective revision. Reapprove the script
   when its hash changed, then show and reapprove the new segment plan and spend
   ceiling, generate/transcribe only the planned revision, and repeat quality
   review.
9. Request `render` approval. Its card chooses subtitled or clean output. After
   approval, call status to obtain `render_idempotency_key`, then submit
   `video_render` with `production_id` and that key. Do not supply captions:
   subtitled output is built only from the accepted STT text. Keep
   `render_engine="auto"` unless the user genuinely requests HTML/GSAP/Lottie
   animation; normal concatenation and captions use the fast FFmpeg path.
10. Wait with the exact returned `version` as `after_version` and increment only
    `wait_iteration`. On completion, verify `resource_check.temp_removed=true`,
    no `remaining_job_processes`, a real audio track, and consistent duration.
    Only then say the final video is complete; the OSS MP4 is already attached.
    A displayed `/workspace/generated_videos/...` path describes the attachment
    contract and is not guaranteed to be mounted in a later tool sandbox. Inspect
    the attachment/status rather than trying to reopen that path with `read_file`.

## Non-negotiable rules

- A visible plan or todo is not approval. The approval record must exist for the
  exact current hash; editing content or references invalidates downstream gates.
- User-facing confirmation always follows the complete content it refers to.
- Never retry an ambiguous paid submit. Reusing the same key only reconciles the
  same request; the server rejects that key with a different request hash.
- A `submitting` job without `provider_task_id` or output is ambiguous. Use the
  exact `generation_job_id` reported for that segment, never a job remembered
  from an older revision. Do not bind an older revision's asset to the new one.
- Never manually caption from the intended script. Captions use accepted STT
  actual speech. A clean master may omit captions but may not falsify QA.
- Never discard old segment outputs. Selective regeneration creates a revision.
- `allow_replan` and `replace_character_reference` are deliberate escalations:
  pass them only after the user explicitly confirmed replanning generated
  segments or changing the presenter. Old outputs stay archived as inactive
  revisions; in-flight jobs can never be replanned over.
- A segment's own generated output must never appear in `input_assets` — always
  reference the originally uploaded material.
- For a progress question, call `video_project(action="status")` and continue
  from its `status`, active segment IDs, approvals, and idempotency keys.
