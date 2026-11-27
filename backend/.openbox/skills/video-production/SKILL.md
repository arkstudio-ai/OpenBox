---
name: video-production
description: Make a vertical spoken-person short video from a topic or script — write the lines, generate each shot with a consistent presenter, check what was actually said, and compose a subtitled or clean cut. Use for 口播/短视频/成片/带货脚本 work, or whenever someone wants a talking-head video built end to end.
allowed-tools:
  - question
  - video_generate
  - video_transcribe
  - video_analyze
  - footage_analyze
  - video_compose
  - image_gen
  - creator_context
  - share_file
  - read
  - write
  - bash
---

# Spoken video production

Bundled scripts run at `/opt/openbox/skills/video-production/scripts/`. Set `S=/opt/openbox/skills/video-production/scripts`. Scripts do not grant permission. Existing lint/check commands are advisory; `prepare.py` validates a whole draft before saving and returns exit 2 with actionable errors when it cannot save it.

Resolve this built-in skill's reference links under `/opt/openbox/skills/video-production/` in the sandbox. A backend-host label from the skill loader describes its instruction source; the documented sandbox paths are where the runtime copy belongs.

App 成片任务遵循本次提交的产出语言与完美成片设置。直接成片跳过生成素材和成片的内容验收、补拍，按确认稿使用 IMS 合成。开启完美成片时才逐镜检查和修复。两种模式均保留传输、解码与合成所需的技术校验。

## Secretary conversation (BossIP v14)

Reuse the brief already confirmed in the assistant conversation. Before drafting, fill only missing
business context: publishing destination, presenter (the user, a digital presenter, or no presenter),
approximate duration and one factual selling point. Ask conversationally, one related decision at a
time; do not force the user through a fixed questionnaire or invent their industry/offer. For Douyin
local reach, normally suggest 9:16; select HD only within the accepted tier. A non-talking
showcase must not be forced into a talking-head script. A user who delegates length gets a reasoned
suggestion, not a silent fixed duration.

Use the live business tier label in user-facing guidance; exact model IDs stay in tool arguments and
state unless the user asks. Never replace their explicit model choice. Explain material requirements
from actual capabilities, using “第一张画面/结束画面” before technical terminology. Even a premium tier
may reject a particular reference video. A library selection or upload is not permission to generate
replacement pictures. Inspect pixels when explaining an image. Quick-tier pictures get reviewed
before spending on video; already accepted pictures do not need another identical card.

Explain editing by what it does: joining materials, readable captions and appropriate music. Respect
no-caption/no-music choices. Do not promise professional precision, free voice changes, or one-minute
turnarounds. Use the Aliyun IMS timeline for the final edit; confirm any new composition scope before
it runs. For revisions, keep unaffected assets and confirm any new image/video/voice work. Delivery
must include a playable result. Ask naturally about the pace and voice; offer publishing only after
acceptance, under the person's separate publishing authorization. Account login, private-message
monitoring, advertising and scheduled reports are separate scopes, not automatic side effects.

## Hard rules

1. Confirm the actual script, material use, production scope and final quality only when that decision is unresolved. Reuse an actual user instruction or answered card for the same scope; do not require a fixed number of cards. An upload, recommended option or agent assumption is not a decision. Ask naturally about missing choices or changed scope. A Markdown heading, table, or request for confirmation is not a card. When review is needed, use `question` with the complete current proposal in `review_content`. Keep prompts in the review details, rather than reading them aloud. Do not estimate credits, compare the cost of alternative plans, or request a separate fee confirmation. If `question` is unavailable and a required decision is missing, stop and explain; submit nothing.
2. A plan, state hash, or successful estimate is not approval. Before the person approves the current production scope, make zero paid video submits. Reuse explicit approval for the same content and scope; do not ask again solely to confirm fees. Separately requested image preparation may happen before that card; accepting images does not approve video spending. Changes to script, segments, materials, model or resolution require review only of the affected production scope.
3. The person's selected model and resolution are creative premises. Read `person_selected_model` and plan within its limits. Never silently change the model or tier. If none is selected for a paid generation, ask one short question before submitting: the registry default or one alternative, with their supported specifications; skip it if they already chose or said 随便/你定. After a provider failure or a `暂不可用` model, never switch models without asking.
4. Use supplied material for its confirmed purpose. Distinguish using the whole picture as a frame, directly referencing its person/product/style, and editing/generating new frames from it. Direct reference does not require intermediate image generation. Call `image_gen` within the authorized production scope, including for artwork in a requested promotional shot. Keep confirmed invariants across prompts. Purpose-made, accepted images can be first/last frames. Use the original source for recurring character references.
5. **素材外传红线：禁图床 / 网盘；禁 ngrok / serveo / `ssh -R` 隧道；禁对外监听。** If an upload or asset channel returns 401/403, stop and explain; do not route around it.
6. Deliver the final video from the playable card returned by `video_compose`. Confirm playback before reporting delivery and explain failures in the user's language.
7. Render the final approved edit on Aliyun IMS through `video_compose` for cuts, transitions, mixing and requested speech captions. Prepare the artwork for any designed product promotion (such as a mid-roll advert, animated poster, price reveal or brand end card) with `image_gen` before generating the video. Include its copy in the artwork itself.

## Light mode

When the system prompt says the person is in Light mode (界面模式：Light), they are on a guided topic page, not in a chat. Ask one thing per `question` card and offer the answers as options (`custom` only when free text is truly needed); set `step_id` on every card from the step list the prompt gives, so the page lights the step up. Fold the script, the shots, the duration and the materials into a single review card instead of separate ones. The style picked in the opening card is the brief: do not ask for it again. Never mention model names, tier internals or credits per second; the page shows the whole-clip price from its own configuration, so do not compute credits. Everything else in this skill still applies.

## Never probe for parameters

`video_generate(action="models")` is the authority for live ids, resolutions, ratios, duration bounds, references, and its second-line `person_selected_model=<id>`. `action="validate"` checks exact parameters and input ownership without submitting or pricing. A paid submit used to discover a limit spends real money on each guess: read, plan, validate, then follow the approved production scope.

## Workflow

Use `$TMPDIR` for extracted frames, audio and other reproducible intermediates.
Keep plans and state in the project. Refer to source clips by asset ID in the IMS timeline.
Asset cache paths may disappear after a completed task; fetch the same asset_id again for inspection.

### Keep preparation focused

- Start with the documented scripts for footage inspection. For an unfamiliar edit, read the IMS timeline schema and effect descriptions.
- Query models and creator context together when independent. Read the shared material guide below for supplied pictures or frame preparation, and the selected model's extra reference when needed; read prompt recipes when writing prompts and quality/composition guidance when those stages arrive. Use `read` on those exact absolute paths, not shell commands to print files. Do not delay material-use confirmation for later-stage checks.
- Reuse results within the same production task: selected capabilities, material choices, accepted assets, and the fitted shot plan. Recheck only when their inputs change, a warning needs resolving, or a previous result is unavailable. Model/resolution changes still require fresh capability checks.
- Use `prepare.py` below to validate and save a complete new draft once. Group later `state.py` updates into one `bash` call and run them sequentially. Briefly name the current work in user terms, such as “整理讲稿与拆段” or “准备整套首尾帧”; do not narrate filesystem operations as production progress.

### 0. Read the menu first and resume safely

Call `action="models"` before planning. Use the selected model's live floor, ceiling, resolution, and reference support: Seedance ≤15s per shot (about 55 Chinese characters), Wan 3.0 ≤30s, SD tiers per registry. A 720p SD tier drops video references; disclose this and offer a compatible model/tier or material, never silently drop the video. If no selection exists, the card says `未指定模型，按当前默认 <模型> <分辨率> 规划`.

On a continuing run, first use `python3 "$S/state.py" show --slug <slug>` and `python3 "$S/state.py" check --slug <slug>`. Resume recorded paid work; do not reconstruct it from memory.

Resolve the effective model from `person_selected_model`, falling back only when absent to the disclosed `default_model`. For supplied pictures or proposed frame preparation, read [references/material-plan.md](references/material-plan.md) for **every model** before choosing a route. Confirm whole-picture use versus person/product/style reference versus new-frame preparation, including each source's preserve/change scope. Offer only modes supported by the live route. Direct `reference_image` is a complete route on a compatible model: do not insert image generation. For `MiniMax-H3-Max-Turbo`, additionally read [references/minimax-turbo-frames.md](references/minimax-turbo-frames.md); its video inputs are explicit frames, with an optional last frame. The original picture can already be a valid first frame. Ask about unresolved intent before card 1; prepare actual new frames only after the script and shot boundaries are known.

Use `question` with `allow_attachments: true` for material selection so the person can select library files or upload in the same card; the voice assistant answers the same card with real asset IDs. Reuse an explicit user decision, including one passed by the personal assistant, rather than asking again. An ambiguous “use this picture” or an upload alone requires clarification. Explain visible details only after inspecting the picture. Re-evaluate intent, roles, preparation scope after a model change; never inherit a Turbo preparation requirement onto another model. Resume already-submitted jobs from their recorded requests without retroactively preparing pictures. `image_gen` follows the person's Default/RunningHub setting; do not pick a different image provider or ask for a concrete image model.

Frame rate is separate from first/last-frame images. Do not promise an adjustable generation FPS when the selected API has no such parameter. Explain provider-fixed settings if asked, preserve a requested delivery FPS for composition, and measure the generated clip's actual frame rate with `ffprobe` alongside duration before acceptance.

### 1. Read creator context

Use `creator_context(action="get_user_context")`; empty is normal. Propose at most one genuinely stable preference to memory per turn and do not interrupt production for it.

### 2. Write and confirm the whole script — card 1

Write pure spoken lines in the person's voice: hook → development → turn → close. If duration was not supplied or delegated, first ask naturally whether a short introduction or a more detailed piece fits the purpose; use a reasonable stated suggestion if the person delegates. Do not silently lock it to 45–60 seconds. **Fit the length before the card, not after it**: run `$S/split_script.py` and `$S/plan_shots.py` on the draft (step 3's commands) and, if the honest total misses the target by more than max(2s, 15%), rewrite until it fits. Print the complete fitted script with its computed length (e.g. 约 15 秒). Also embed that complete script and length in the question text itself: a card saying only “讲稿是否符合要求” is not reviewable, and the voice front desk may not receive your earlier prose. Do not add a model/tier question when the server's confirmed_setup or person_selected_model already supplies the choice. Then invoke the `question` tool once with:

**Language of the lines.** The script, the cards, the subtitles and any new narration are in the output language the system prompt names (Simplified Chinese when it names none); this file being written in English and Chinese changes nothing. For a line that is not Chinese: write the line itself in that language with every diacritic (Vietnamese `phở bò`, never `pho bo`, never a Chinese translation); keep the visual description of each prompt in Chinese and say once in it which language and accent is spoken (`用越南语（北方口音）说`); the line after `口播台词：`, `spoken_line` and the subtitle text are the same verbatim words. The no-text rule matters more here, since a drawn Vietnamese subtitle loses its tone marks. `split_script.py`, `plan_shots.py` and the lint count such a line by how long it takes to say (a Vietnamese syllable ≈ 1.2 Chinese characters, an English word ≈ 2), so use the same commands and rates; `compare_transcript.py` ignores letter case. `video_transcribe` listens for the user's interface language by default; pass `language` (`zh`, `en`, `vi`) when the lines are in another. Measured 2026-11-22 on 8-second shots: MiniMax H3 and Wan 3.0 spoke Vietnamese lines word for word, Seedance 2.5 changed four words in its one shot — the transcript check decides each take, in every language.

- 时长：`可以` / `短到约 30 秒` / `长到 60–75 秒` / `需要修改`
- 字幕：`配字幕（默认）` / `不配字幕`

Keep the fitted lines, durations and rate for step 3. Once the plan is within the stated tolerance and has no unresolved duration warnings, proceed to the card; do not keep rewriting or changing the rate to make an approximate target land on an exact second. Honour an explicitly exact duration request separately.

If duration was supplied, honour it. Ask the subtitle choice only if unresolved; an explicit subtitle/no-subtitle choice passed from the assistant is already a decision. Card 1 repeats only when the **person** asks for a change: after 「可以」 you never rewrite, shorten or lengthen the script on your own — a length problem found later is reported on the next card as a choice, not fixed silently (2026-09-09: a confirmed 15 s script was re-timed to 17 s in step 3, trimmed by the agent, and card 1 was shown twice). On the person's edits, print the full revision and repeat card 1. After confirmation:

```bash
python3 "$S/state.py" set --slug <slug> --key script --value "<完整讲稿>"
python3 "$S/state.py" confirm --slug <slug> --kind script --note "成稿卡已确认"
```

### 3. Split and time against the selected model

Reuse the fitted plan from step 2 when the script and model bounds are unchanged. Split at meaning and calculate each line's honest duration only if no valid plan exists; run both scripts in the same `bash` call and pass the splitter's `plan_shots_args` directly to the planner:

```bash
S=/opt/openbox/skills/video-production/scripts
python3 - "$S" <<'PY'
import json, subprocess, sys
s = sys.argv[1]
split = json.loads(subprocess.check_output([
    sys.executable, f"{s}/split_script.py", "--text", "<完整讲稿>", "--max-chars", "40",
], text=True))
if "error" in split:
    print(json.dumps(split, ensure_ascii=False))
else:
    subprocess.run([
        sys.executable, f"{s}/plan_shots.py", "--target", "<asked>", "--rate", "<pace>",
        "--min-shot-seconds", "<floor>", "--max-shot-seconds", "<ceiling>",
        *split["plan_shots_args"], "--json",
    ], check=True)
PY
```

The splitter emits `plan_shots_args`. Forty characters is advice, not a universal cap. Always send an explicit duration; do not use `-1`. Never divide the requested total by the shot count. Choose `--rate` from the piece you just wrote: calm 3.4, conversational 4.0, energetic 4.6. Both bounds come from the selected model: Seedance takes 4–15s, Wan 3.0 takes 2–30s, MiniMax H3 takes 4–30s; re-read others. The honest total should already match, because step 2 fitted it. If it still differs, do not touch the script: carry the exact duration onto card 2 as a choice (accept the honest length / edit the script) instead of squeezing delivery or re-running card 1.

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

## What actually goes wrong

- Presenter/hair changes: reuse original material and exact base; prefer video; a stable model seed may help when supported.
- Words change: always STT; one-character substitutions can keep high similarity and invert meaning.
- An extra “艾特” before the line: an `@` reached the model. The line belongs after `口播台词：` with no `@` anywhere; fix the prompt before a paid retry, since an unchanged one repeats it.
- Background changes: anchor it and avoid decorative overload.
- Text in the picture, wrong or unwanted: the model drew the line itself (2026-10-15, two of five Seedance shots, despite 无字幕 twice). Full negative wording up front lowers the odds; `video_analyze(action="screen_text")` on every take is what catches it; inspect the affected reference and correct it before a targeted reshoot. Use the verified wording for requested captions.
- Captions overflow or joins drift: correct line breaks or real source intervals in the IMS timeline and validate the updated cut.
- Identity material is rejected: use the product's own authorisation flow; never externalise or bypass it.
- Selected tier ignores a reference: disclose and offer choices; never continue silently.
- Project seems missing: read state/workspace; do not recreate paid work.
- Final has no audio: catch it with `$S/state.py check` before delivery.
- Parameter error: re-read models and validate; it does not imply the tool is broken.
- Planned duration differs from measured output: preserve the requested duration and report the actual result honestly.

## Reference

- `references/prompt-recipes.md` — rules, roles, examples, compliance and script structure
- `references/model-guide.md` — selected-model discipline, references, duration and cost
- [references/material-plan.md](references/material-plan.md) — all-model material-use confirmation, direct references, frame preparation and long-video joins
- [references/minimax-turbo-frames.md](references/minimax-turbo-frames.md) — read for the selected Turbo model: material choice, complete frame preparation, shared boundaries, state and parallel video generation
- `references/quality.md` — pathology, STT, duration acceptance and composition checks
- `references/compose-timeline.md` — the `video_compose` timeline format, card 4, verified effects and billing

Publishing/posting is handled by the `douyin-desktop-publish` skill (load it with the `skill` tool when the person wants the video on Douyin — it publishes through the cloud desktop's logged-in 创作者中心 and only falls back to the QR package when that route is switched off); this skill produces and delivers the file.
