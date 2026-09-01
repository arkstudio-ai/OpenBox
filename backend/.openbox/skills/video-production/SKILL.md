---
name: video-production
description: Make a vertical spoken-person short video from a topic or script — write the lines, generate each shot with a consistent presenter, check what was actually said, and compose a subtitled or clean cut. Use for 口播/短视频/成片/带货脚本 work, or whenever someone wants a talking-head video built end to end.
allowed-tools:
  - video_generate
  - video_transcribe
  - image_gen
  - creator_context
  - share_file
  - bash
---

# Spoken video production

The bundled scripts run in the sandbox, at
`/opt/openbox/skills/video-production/scripts/` — this file is served from the
backend, so a relative path would not resolve where bash actually runs. Set
`S=/opt/openbox/skills/video-production/scripts` once and use `$S/...` below.

This is craft knowledge, not a pipeline. Every tool below works on its own; a
shot that needs to break one of these rules is allowed to. Depart from the
workflow when the person's request calls for it, and say why.

The tools own money and ownership: `video_generate` refuses what a model cannot
do, refuses a second identical job already in flight, and enforces a daily
ceiling. Nothing here needs to re-check those. What this skill knows is what
makes a talking-head video *good*.

## Workflow

1. **Read the creator.** `creator_context(action="get_user_context")` before
   drafting — voice, audience, boundaries. Empty is normal; carry on.
2. **Write the whole script first, in the person's voice.** Roughly 3.2 Chinese
   characters per second, so 45–60s is about 150–190 characters. Show it and get
   a plain yes before spending anything.
3. **Split at meaning, not at length.** Five shots is a good default for 30–60s.
   Keep a line under ~40 characters: past that the model rushes the delivery and
   the caption needs three lines. `$S/lint_prompt.py` counts it for you.
4. **Fix the presenter once.** One image is the anchor for every shot — a photo
   the person supplied, or one from `image_gen`. Pass it as an input asset on
   every shot with the same visual anchor sentence in every prompt.
   Never anchor a later shot to an *earlier generated frame*: drift compounds.
5. **Write one prompt per shot** using `references/prompt-recipes.md`. Check each
   with `python3 "$S/lint_prompt.py" --prompt-file shot1.txt --script "…"
   --anchor "…"`, then read what it says — it advises, it never blocks.
6. **Pick the model deliberately.** `video_generate(action="models")` is the only
   description of what each one accepts; `references/model-guide.md` covers the
   trade-offs. Use `action="estimate"` to validate a shot for free before paying.
7. **Generate the shots.** One `video_generate(action="submit")` per shot with a
   distinct `idempotency_key` (`<slug>:shot<N>:v1`). Independent shots can go out
   together. A finished video lands in `/workspace` automatically.
   **A timeout is normal, and a paid task is never replaced.** On
   `polling_paused=true`, end the turn, report the `job_id`, and resume that same
   id later — never resubmit, never cancel.
8. **Check what was actually said.** Per shot:
   `$S/extract_audio.sh shot1.mp4 shot1.mp3` →
   `share_file(file_path="shot1.mp3", attach=false)` →
   `video_transcribe(action="submit", asset_id=...)` →
   `python3 "$S/compare_transcript.py" --intended "…" --heard "…"`.
   Show the person the video, the intended line, the actual words, and the
   verdict. `suspect` means look, not fail.
9. **Regenerate only what is wrong.** A bad take gets a new key (`:v2`) and
   leaves the old one alone. Keep every good shot.
10. **Compose.** Captions come from the **actual transcript**, never the written
    line — otherwise the words on screen drift from the audio.
    `$S/build_ass.py` then `$S/compose.sh`. Hand over the result with
    `share_file`, and verify it has audio and the length you expect.

Keep notes in `/workspace/videos/<slug>/` with `$S/state.py` so a later
turn can pick this up. It is a notebook, not a gate.

## What actually goes wrong

- **The presenter changes between shots.** Same anchor sentence, byte for byte,
  in every prompt, plus the same reference image. On a model that supports it,
  reuse one `seed` across shots; `last_frame` of the previous shot as the
  `first_frame` of the next is stronger still. See `references/model-guide.md`.
- **The model says something else.** Common and cheap to catch — always step 8.
  Short substitutions (出片 → 出花) keep a high similarity and change the
  meaning, which is exactly why the verdict is not just a number.
- **Burned-in subtitles.** Ask for `无字幕` in every prompt; captions are a post
  step, and a model that renders its own leaves you with two sets.
- **Captions overflow.** `build_ass.py` wraps CJK explicitly because libass
  breaks on whitespace and Chinese has none.
- **Clips drift out of sync when joined.** `compose.sh` normalises fps, timebase
  and audio format before concat. Do not hand-roll a simpler concat.

## Reference

- `references/prompt-recipes.md` — the prompt shape, per shot role
- `references/model-guide.md` — choosing a model, continuity tactics, costs
- `references/quality.md` — the transcript check and when to regenerate
