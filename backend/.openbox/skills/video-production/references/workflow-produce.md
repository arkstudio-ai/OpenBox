# Spoken video production — steps 4–8, invalidation and known failures

Detail for the `video-production` skill, read once card 1 is confirmed. The hard rules in SKILL.md still apply. Paths such as `references/…` and `$S/…` resolve exactly as in SKILL.md: under `/opt/openbox/skills/video-production/` in the sandbox.

## Workflow

### 4. Assign materials and write prompts

Use `person`, `scene`, `outfit`, `prop` to describe creative purposes; see `references/prompt-recipes.md`. Actual tool input roles depend on the selected model. Video anchors are an option only where supported. Number images and videos separately and list which shot receives each. With zero assets and no request to generate pictures, use the exact same textual base in every segment and do not call `image_gen`. Incompatibility requires a disclosed user choice, not an improvised anchor or silent model change.

For frame-based modes, plan shared boundaries and independent scene cuts. Generate the authorised independent first frames, last frames and artwork in the same tool response, up to 10 separate `image_gen` calls. If a frame needs a newly created reference, wait only for that reference's asset ID. Check each shot's assigned frames and any required acceptance as they become available. Reuse shared boundaries by ID. See `references/minimax-turbo-frames.md` for Turbo.

**Designed promotional shots.** Use the current product asset in `image_gen.input_images` and give `image_gen` the exact approved copy for that shot. This can include the product name, shop or brand, price with currency and unit, slogan, or call to action. Verify each string against the current brief or form, and skip optional fields that are blank. Check the generated artwork at delivery aspect ratio for legibility, spelling and safe margins before filming. Pass its asset ID as `first_frame` or as the first and last frames when the model supports both and the layout should hold. Describe a locked or subtle camera and move the product details, scene lighting, steam or background in areas that leave the letters clear. Sample the actual video at the beginning, middle and end. If lettering changes, adjust the artwork or reduce motion and regenerate the affected shot. Use the approved generated shot directly in IMS, with the promotional text already in its pixels.

Write every prompt in full. Put the exact line after `口播台词：` and end the line there (also pass it to `video_generate` as `spoken_line`), describe tone, and restrain scene decoration. For scenes without planned artwork, put `画面中不出现任何文字、字幕、标题、水印；字幕只能后期合成` right after the visual base. For prepared ad artwork, use `保留原画上的已确认宣传文字，不增加字幕、额外文字或水印` there and specify what can move. Never write `@` in a prompt. H3 native dialogue uses its own tags as documented in the ComfyUI H3 Skill:

```bash
python3 "$S/lint_prompt.py" --prompt-file shot1.txt --script "<逐字台词>" --anchor "<逐字基底>" --images <N> --videos <N>
```

For a complete new draft, prefer the single preparation call below instead of separate lint/init/set/shot calls. The standalone lint command remains useful for revising a single shot. `镜头跟随` is valid for a deliberate walking shot; mismatched text after `口播台词：` is a warning to fix or disclose; `dialogue_at_sign` never is — remove the `@`. Issues include `accepted_examples`: correct the identified field directly, without reading the validator's source.

Use `write` to save one JSON plan at an absolute workspace path, using the selected model, tier, fitted durations, full accepted script and prompts. [Plan example](references/material-plan.md#示例制作计划)

For supplied material, include the already resolved `material_plan` object from `references/material-plan.md` and exact `input_assets` entries `{ "asset_id": "<owned asset>", "role": "reference_image" }` (or the confirmed supported frame/video/audio role). Omit `ratio` for image-driven adaptive sizing when appropriate. Set `speech: false` only for a shot without dialogue; do not suppress dialogue checks for a two-person scene. Never put approvals, previous jobs or acceptances into this draft.

```bash
python3 /opt/openbox/skills/video-production/scripts/prepare.py --plan /workspace/<current-workspace>/video-plan.json
```

Use the actual current workspace path. This validates all shots, preserves existing notebook history, saves the complete draft once, and returns exact `validation_requests`. `saved=true` is not approval. If it reports errors, edit only the affected plan fields using the returned examples, then rerun that same file. An identical successful preparation does not need repeating; go straight to validation and any still-needed content review. If the exact script is missing, report that deployment dependency rather than searching for old copies.

### 5. Validate and review the production plan

Run `action="validate"` for the exact `validation_requests` from preparation; do not probe with test prompts. This checks capabilities and assets without submission or estimating credits. Correct only reported errors. Never call retired `estimate` or `quote_plan` actions.

Reuse the person's confirmed script, duration, material assignments, model and resolution. If the current production scope is already explicitly approved, continue without another card. Otherwise use one content review with the full shot plan in `review_content`: every line and complete prompt, shot duration and total, confirmed material assignment, and any actual compatibility change. Tool-call details do not count as display. The question itself should briefly ask whether to start this production, with choices `开始制作` / `修改内容` and `requires_confirmation: true` when starting generation. No price, estimated credits, alternatives by cost or fee-confirmation fields belong on this card.

Record the actual production decision with `state.py confirm --kind shots` only after user approval. Successful preparation or validation never records approval on its own. Do not repeat already confirmed content or introduce a fee card before generation.

### 6. Submit each ready shot and review results as they arrive

Once a shot's inputs, prompt, explicit duration and selected parameters are ready within the confirmed scope, submit it with `shot=<N>`, the exact `spoken_line` or `no_speech=true`, and a stable key for this take. In the same response, dispatch other ready shots and independent image calls. Record each job ID. Serialize writes to the same local `state.py` project file.

Continue preparing shots while the backend follows the renders. With `background_review=true`, it batches nearby completions and reads the picture, speech and timings immediately, pooling minutes across the production. When blocked, call `video_generate(action="await_ready", job_ids=[...])` once with all outstanding IDs. Compare each returned `review` with the approved script and artwork; use existing evidence for step 7. Inspect specific gaps or failures separately. Ordinary jobs return raw clips as they finish. On `polling_paused=true` leave the job in background recovery and end the turn. Reuse existing IDs across interruptions.

For a confirmed failed job, diagnose the reported cause before retrying. For a completed take, identify the defect and correct its source or prompt before reshooting that shot. Reuse the authorized scope and retain working takes.

When `video_generate` returns `INSUFFICIENT_CREDITS`, tell the person which shots were blocked and report the required, available and missing credits returned by the tool. Keep completed clips and keep following already submitted jobs. Pause new submissions until the person tops up or agrees to an affordable change to the remaining shots, such as a shorter duration or a lower-cost tier. Use the existing job IDs for work already submitted.

### 7. Transcribe, look at the frames, compare — card 3

Use each shot’s returned `review` for its transcript, timing, pictured action and visible text. For legacy shots with no background review, pass the candidate asset IDs together to `footage_analyze(transcribe=true, word_times=true)`. Use that same evidence for caption timing and the review card. For an ambiguous letter, a newly appearing caption or a specific point in a join, use `video_analyze(action="screen_text", source="<owned asset ID>")` or capture the relevant frame. The screen-text result gives the text, its type, the sampled times and the cost. If inspection is unavailable, mark the unverified item for review.

For each take compare the actual words, picture text, action, duration and frame rate to the approved shot. Give a playable result and exact time for any defect. Where an ASR character is homophonic with the approved wording, check pronunciation and context and retain the correct spelling in subtitles. Confirm visible brand and offer lettering against the artwork.

For a multi-shot cut, also review each join as described in `references/quality.md`: adjacent actual images, motion, voice and pauses. Shared planned frames constrain endpoints but do not prove a seamless join or a consistent voice. Include join findings in card 3; never report an unobserved visual/audio property as checked.

The person chooses affected shots to regenerate or accept. Before a paid regeneration, show the affected shots, actual changes, seconds, and current model and resolution. Reuse an explicit request to regenerate that same scope; do not estimate credits or add a fee confirmation. Record acceptance with `python3 "$S/state.py" shot --slug <slug> --index <N> --accept "<用户接受理由>"`. Regenerate only affected shots. One shot has exactly one current take; `:v2` supersedes `:v1`.

### 8. Compose, check, deliver

Use the accepted actual transcript for requested speech captions. Build the IMS timeline with approved cuts, audio and consistent dimensions. Existing in-frame promotional copy stays in the generated clip. Keep that copy out of IMS `texts` and `overlays`.

Use `references/compose-timeline.md` and the tool schema. Validate the timeline, then submit with a stable key within the approved composition scope. Use `video_compose(action="await_ready", job_id=...)` once to receive its attached result. Check timing, readability, audio and final playback. In the result, `billing_mode=enforce` means the displayed credits were deducted, and `billing_mode=shadow` means they were metered only.

## Invalidation and samples

- Script changed → identify `受影响段`; splitting and prompts are stale; rebuild and repeat card 2.
- Segment/prompt/material changed → that shot needs review of the changed production scope; keep unaffected generated shots.
- Material use or preparation scope changed → update `material_plan`, confirm the changed choice and rebuild affected prompts; a previous choice does not approve extra image generation. A shared boundary changed → update every shot's `assets` mapping that uses it (both neighbours for an internal boundary), repeat affected image acceptance, and rebuild those requests and content review for the changed scope.
- Composer model/resolution changed → read models again; splitting, capability-dependent prompts and parameter validation are stale; repeat card 2.
- `$S/state.py check` names affected shots; it advises, it never blocks and exits 0.
- “先做一段样片” confirms only that sample. Remaining shots need their own later card 2.

The pitfalls ("What actually goes wrong") are in `SKILL.md`.
