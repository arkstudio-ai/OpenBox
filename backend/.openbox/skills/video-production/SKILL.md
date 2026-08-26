---
name: video-production
description: Create spoken-person videos with Seedance, including image-to-video character references, generated speech with lip sync, multi-segment waiting, subtitles or clean output, and final HyperFrames/FFmpeg composition.
allowed-tools:
  - image_gen
  - video_generate
  - video_render
---

# OpenBox Video Production

Use the skill-only `image_gen`, `video_generate`, and `video_render` tools. Their
schemas are absent from ordinary conversations and become available only after
this skill is loaded for the current agent run. `image_gen` is included so an
end-to-end production can create one durable host portrait before any video
segments are submitted. Provider credentials stay on the backend; never copy a
key into a prompt, command, workspace file, or sandbox.

## Storage and execution contract

- All source images/videos must already be ready OSS assets owned by the user.
  Pass their `asset_id`; a displayed attachment path is also accepted.
- Seedance outputs are immediately copied from the provider's expiring URL to
  OpenBox OSS, indexed in `file_assets`, and attached to chat.
- FFmpeg and HyperFrames run only through `video_render` on the user's WUYING
  desktop. Do not start either program through shell/terminal.
- The WUYING queue is durable and enforces the configured desktop concurrency
  (currently 1). Each user will ultimately have an independent desktop, so the
  queue is deliberately per desktop rather than a global AWS queue.
- The renderer downloads over internal OSS URLs, reuses a cache keyed by the
  stable OSS object, writes attempts under `/tmp/openbox-media/jobs/<job_id>`,
  uploads the final MP4 to OSS, and removes the attempt directory on every
  terminal path.

## Spoken-video workflow

1. Draft the complete script and split it into short, independently regenerable
   segments. Five segments is a good default for a 30-60 second short video.
2. Confirm the script, portrait/reference image, aspect ratio, subtitles versus
   clean output, and expected paid generation calls before submission. A direct
   user request to run a named end-to-end production is confirmation for those
   requested segments, but not for unbounded retries.
   For a multi-segment video presented as one host, require one shared portrait
   asset before submitting any segment and reuse that exact asset for every
   segment. A text-only run cannot guarantee that the generated people are the
   same person; do not describe it as identity-consistent. If a text-only
   fallback is explicitly accepted, at least reuse one fixed `seed` for every
   segment. Never vary seeds while claiming that the host is the same person.
   If the user has not supplied a portrait, call `image_gen` once to create a
   clean vertical host portrait, inspect the attached result, and retain its
   returned OSS `asset_id`. Do not start any Seedance segment until that single
   portrait exists and is visibly suitable. Image generation is also a paid
   provider call, so it is covered only by the same explicit end-to-end approval
   or by separate confirmation.
3. For each segment call `video_generate(action="submit")` with:

   - the same `character_reference_asset=<portrait asset_id>` when identity
     continuity is required; reserve `input_assets` for additional scene or
     motion references and do not duplicate the portrait there;
   - the standard `doubao-seedance-2-0-260128` model for spoken audio;
   - `generate_audio=true`, normally `resolution="720p"`, `ratio="9:16"`, and
     `duration=-1` for intelligent duration;
   - a unique stable `idempotency_key`, such as
     `<project>-segment-03-v1`. Never reuse one for a different prompt;
   - a prompt that quotes the exact Mandarin line and explicitly requires the
     visible person to speak it naturally, with accurate Mandarin lip motion,
     stable identity/camera, clean audio, and no generated captions or watermark.

4. Wait for every segment with `video_generate(action="wait", job_id=...,
   wait_seconds=25)`. The tool polls the provider inside that bounded window.
   Repeat only while the returned status is `queued` or `in_progress`, using
   `retry_after_seconds`; do not create a replacement task merely because it is
   slow. Keep the user informed when several waits are needed.
5. Review each completed segment before composing:

   - confirm the returned asset is ready in OSS;
   - confirm the same person remains visible and the mouth changes naturally
     during speech; listen for missing, duplicated, or badly pronounced words;
   - the renderer will independently require a real audio track and validate
     duration. If speech is materially wrong, regenerate only that segment with
     a new versioned idempotency key after explicit approval for the extra call.

6. Call `video_render(action="submit")` with segment asset IDs in narrative
   order. Supply one caption per segment for subtitled output, or set
   `subtitles=false` for a clean master. Use another stable idempotency key.
   Keep `render_engine="auto"` for ordinary spoken-video concatenation and
   burned captions; the WUYING worker selects its pure-FFmpeg fast path and
   does not start Chrome. Use `render_engine="hyperframes"` only when the
   requested deliverable genuinely needs HTML/GSAP/Lottie animation.
   Match the render size to the generated sources: use `width=720,
   height=1280` for ordinary 720p vertical clips. Reserve 1080x1920 for a
   user-requested 1080p master or genuinely 1080p inputs; upscaling 720p clips
   adds render time without restoring source detail.
7. Wait with `video_render(action="wait", wait_seconds=25,
   after_version=<returned version>, wait_iteration=<counter>)`. Start the
   counter at 0 and increment it on each repeated wait. Always reuse the exact
   returned `version` as `after_version`; never invent or increment a version.
   `wait_iteration` is deliberately ignored by the queue and only distinguishes
   legitimate repeated long polls from a stuck identical-tool loop. Interpret
   the states exactly as follows:

   - `queued`: tell the user the `queue_position`; wait again after the returned
     delay. This is expected when another browser window is rendering.
   - `in_progress`: report the current stage and wait again.
   - `completed`: use the returned OSS `asset_id`; the MP4 is already attached
     and needs no `share_file` call.
   - `failed` or `cancelled`: inspect the error. Use
     `video_render(action="retry")` only for a retryable render failure; this
     retains verified downloads and does not regenerate billed Seedance clips.

8. Before claiming success, inspect the terminal `resource_check`. Require
   `temp_removed=true`, an empty `remaining_job_processes`, a present audio
   track, and a final duration consistent with the input sum. Report a cleanup
   failure rather than hiding it.

## Prompt pattern for one spoken segment

```text
Use the reference portrait as the same on-camera host. Vertical medium close-up,
stable tripod camera, natural daylight and subtle realistic gestures. The host
looks into the lens and says in clear natural Mandarin, exactly: “<line>”. Match
mouth shapes and timing accurately to every spoken word; keep facial identity,
clothing, background and voice consistent. Clean speech audio. No on-screen
text, captions, logos, watermark, cuts, extra people or background voices.
```

Keep each quoted line short enough for a natural take. Do not ask one segment
to deliver several dense paragraphs.

## Failure and retry rules

- `submit` is billable and never auto-retried by the tool. A network timeout can
  be ambiguous: report it and do not submit a duplicate without a decision.
- Reusing the same idempotency key returns the existing OpenBox job rather than
  billing a second generation.
- A provider failure needs a new segment idempotency key only when intentionally
  regenerating. A render retry uses the same render job and cached inputs.
- Never download a completed provider URL manually or upload a final MP4 by
  hand. Doing so bypasses ownership checks, OSS indexing, queue coordination,
  temp cleanup, and chat rendering.
