---
name: video-production
description: Make a vertical spoken-person short video from a topic or script — write the lines, generate each shot with a consistent presenter, check what was actually said, and compose a subtitled or clean cut. Use for 口播/短视频/成片/带货脚本 work, or whenever someone wants a talking-head video built end to end.
allowed-tools:
  - question
  - video_generate
  - video_transcribe
  - video_compose
  - image_gen
  - creator_context
  - share_file
  - bash
---

# Spoken video production

Bundled scripts run at `/opt/openbox/skills/video-production/scripts/`. Set `S=/opt/openbox/skills/video-production/scripts`. This is craft knowledge, not a pipeline: each script advises, it never blocks, grants permission, or returns non-zero for a finding.

Resolve this built-in skill's reference links under `/opt/openbox/skills/video-production/` in the sandbox. A backend-host label from the skill loader describes its instruction source; the documented sandbox paths are where the runtime copy belongs.

## Hard rules

1. The core workflow has three required `question` tool calls: finished script, complete shots + price, and STT results — plus a fourth, 合成确认, whenever the cut goes through `video_compose` (it spends credits). Turbo also has a material-choice card when the choice is unresolved, and an image-confirmation card when frames need acceptance; these do not replace the core cards. A Markdown heading, table, or request for confirmation is not a card. Put every reviewable detail in prose before invoking the tool. If `question` is unavailable, stop and explain; submit nothing.
2. A plan, state hash, or successful estimate is not approval. Before the person chooses “可以” on the current complete shot card, make zero paid video submits. Separately requested image preparation may happen before that card; accepting images does not approve video spending. Script, segment, prompt, material, model, or resolution changes invalidate affected planning and cost confirmation.
3. The person's selected model and resolution are creative premises. Read `person_selected_model` and plan within its limits. Never silently change the model or tier. If none is selected, use and disclose the registry default.
4. Use supplied material first. Call `image_gen` only when the person explicitly asks for generated material, including choosing a generation option on the Turbo material card. With no assets and no such request, use one textual `全片一致的画面基底`, byte for byte in every prompt. Purpose-made, accepted images can be first/last frames; never automatically turn a generated video's still into a general character reference.
5. **素材外传红线：禁图床 / 网盘；禁 ngrok / serveo / `ssh -R` 隧道；禁对外监听。** If an upload or asset channel returns 401/403, stop and explain; do not route around it.
6. Delivery means `share_file` for a sandbox-composed file. A successful `video_compose` result already attaches the final file and needs no duplicate share. Until the selected delivery path returns a playable/downloadable result, never say “已交付”. In user-facing text, 不暴露内部 id/路径/工具名; translate errors into plain language.
7. Two ways to compose, never a third. A plain cut — concat plus burnt ASS captions — is ffmpeg + libass in the sandbox (`$S/build_ass.py` + `$S/compose.sh`). A cut that needs transitions, styled captions with motion, a hook title or a lower third goes through `video_compose` (cloud, Aliyun IMS): `action="schema"` for the timeline format, `action="validate"` (free) before `action="submit"`, then `wait` on the job_id. Shots are the accepted takes' asset_ids; the output attaches to the chat like a generated shot. HyperFrames is retired; never reintroduce it, and never render in the sandbox with Chrome.

## Never probe for parameters

`video_generate(action="models")` is the authority for live ids, resolutions, ratios, duration bounds, references, and its second-line `person_selected_model=<id>`. `action="estimate"` validates the exact request and price for free. A paid submit used to discover a limit spends real money on each guess: read, plan, estimate, then ask.

## Workflow

### Keep preparation focused

- Use the documented absolute paths directly. Do not list directories, search the filesystem, read script source, or request `--help` before using the commands below. If a required file is missing, report the exact missing dependency as an incomplete skill deployment; do not search `/` or substitute an unrelated file. An incomplete-looking tool result warrants one bounded read of the exact file, not broader exploration.
- Query models and creator context together when independent. Initially read only the selected model's required reference; read prompt recipes when writing prompts and quality/composition guidance when those stages arrive. Combine independent file reads in one `bash` call. Do not delay the Turbo material-choice card for later-stage checks.
- Reuse results within the same production task: selected capabilities, material choices, accepted assets, and the fitted shot plan. Recheck only when their inputs change, a warning needs resolving, or a previous result is unavailable. Model/resolution changes still require fresh capability and price checks.
- Group consecutive `state.py` commands into one `bash` call and run them sequentially. Briefly name the current work in user terms, such as “整理讲稿与拆段” or “准备整套首尾帧”; do not narrate filesystem operations as production progress.

### 0. Read the menu first and resume safely

Call `action="models"` before planning. Use the selected model's live floor, ceiling, resolution, and reference support: Seedance ≤15s per shot (about 55 Chinese characters), Wan 3.0 ≤30s, SD tiers per registry. A 720p SD tier drops video references; disclose this and offer a compatible model/tier or material, never silently drop the video. If no selection exists, the card says `未指定模型，按当前默认 <模型> <分辨率> 规划`.

On a continuing run, first use `python3 "$S/state.py" show --slug <slug>` and `python3 "$S/state.py" check --slug <slug>`. Resume recorded paid work; do not reconstruct it from memory.

Resolve the effective model from `person_selected_model`, falling back only when absent to the disclosed `default_model`. For `MiniMax-H3-Max-Turbo`, read [references/minimax-turbo-frames.md](references/minimax-turbo-frames.md) and resolve the material choice before card 1: generate a first frame, generate first/last frames, use existing pictures, or text-only video. Reuse an explicit request or saved decision instead of asking again. This step chooses the mode; generate the actual frames only after the script and shot boundaries are known. Other models follow the existing material workflow. Resume already-submitted shots from their recorded requests without retroactively adding image preparation.

### 1. Read creator context

Use `creator_context(action="get_user_context")`; empty is normal. Propose at most one genuinely stable preference to memory per turn and do not interrupt production for it.

### 2. Write and confirm the whole script — card 1

Write pure spoken lines in the person's voice: hook → development → turn → close. If duration was not supplied, draft 45–60s first; do not ask duration before showing a usable script. **Fit the length before the card, not after it**: run `$S/split_script.py` and `$S/plan_shots.py` on the draft (step 3's commands) and, if the honest total misses the target by more than max(2s, 15%), rewrite until it fits. Print the complete fitted script with its computed length (e.g. 约 15 秒), then invoke the `question` tool once with:

- 时长：`可以` / `短到约 30 秒` / `长到 60–75 秒` / `需要修改`
- 字幕：`配字幕（默认）` / `不配字幕`

Keep the fitted lines, durations and rate for step 3. Once the plan is within the stated tolerance and has no unresolved duration warnings, proceed to the card; do not keep rewriting or changing the rate to make an approximate target land on an exact second. Honour an explicitly exact duration request separately.

If duration was supplied, honour it and still ask the subtitle choice. Card 1 repeats only when the **person** asks for a change: after 「可以」 you never rewrite, shorten or lengthen the script on your own — a length problem found later is reported on the next card as a choice, not fixed silently (2026-09-09: a confirmed 15 s script was re-timed to 17 s in step 3, trimmed by the agent, and card 1 was shown twice). On the person's edits, print the full revision and repeat card 1. After confirmation:

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

The splitter emits `plan_shots_args`. Forty characters is advice, not a universal cap. Always send an explicit duration; do not use `-1`. Never divide the requested total by the shot count. Choose `--rate` from the piece you just wrote: calm 3.4, conversational 4.0, energetic 4.6. Both bounds come from the selected model: Seedance takes 4–15s, Wan 3.0 takes 2–30s; re-read others. The honest total should already match, because step 2 fitted it. If it still differs, do not touch the script: carry the exact duration onto card 2 as a choice (accept the honest length / edit the script) instead of squeezing delivery or re-running card 1.

### 4. Assign materials and write prompts

Use `person`, `scene`, `outfit`, `prop` to describe creative purposes; see `references/prompt-recipes.md`. Actual tool input roles depend on the selected model. Video anchors are an option only where supported. Number images and videos separately and list which shot receives each. With zero assets and no request to generate pictures, use the exact same textual base in every segment and do not call `image_gen`. Incompatibility requires a disclosed user choice, not an improvised anchor or silent model change.

For Turbo image modes, now prepare the **whole approved scope's** material set using `references/minimax-turbo-frames.md`. For N consecutive shots in one scene, first/last-frame mode normally uses N+1 planned images; adjacent shots share the exact boundary asset. Generate or resolve all needed images, inspect them, show their shot assignments, and obtain acceptance before the video quote. Existing accepted pictures need no repeat card; text-only mode needs no images or image-confirmation card. Record the preparation decision and each shot's structured frame mapping in state. Frame preparation can depend on a common identity image, but video shots must not depend on earlier generated videos to supply their inputs.

Write every prompt in full. Put the exact line after `口播台词：` and end the line there, use tone not speed, restrain scene decoration, and require `无字幕`. Never write `@` in a prompt: the model reads it aloud (production STT heard “艾特” before the line), so it marks nothing:

```bash
python3 "$S/lint_prompt.py" --prompt-file shot1.txt --script "<逐字台词>" --anchor "<逐字基底>" --images <N> --videos <N>
```

Read the zero-exit advice. `镜头跟随` is valid for a deliberate walking shot; mismatched text after `口播台词：` is a warning to fix or disclose; `dialogue_at_sign` never is — remove the `@`. Record top-level model/resolution and each shot's full script, prompt, planned seconds, assets, model, and resolution with `$S/state.py set/shot` before confirmation.

### 5. Estimate and show complete shots + price — card 2

Run `action="estimate"` for every exact request; each returns `estimated_credits` (requested seconds × the model/tier rate) — sum them and show the total as the planned cost. Before invoking the card, show without abbreviation:

- every complete line and every complete model prompt, character for character;
- each shot's seconds and the honest total; call out mismatch with requested duration;
- every image/video and the shots using it, or zero-material textual-base wording; for Turbo shots with images, label each shot's first/last frame and the shared boundaries using visible picture labels, not internal asset IDs;
- `按你选的 <模型> <分辨率> 规划`, or the disclosed registry-default wording;
- shot count, total paid seconds, planned cost, and any proposed compatibility change with reason. If `estimate` validates parameters but returns no currency amount, say `预计费用暂不可得（estimate 未返回金额）` and stop; never substitute "已产生费用 0 元" for the planned quote. A separately documented dated rate may be shown only as a clearly labelled reference estimate.

Copy every full line, full prompt, seconds, and material assignment into the user-visible response itself; tool-call details do not count as display. Then invoke card 2: `可以` / `修改拆段或 prompt` / `更换模型、分辨率或素材`. This combines shots and spend; no separate fee card. Until the person chooses “可以”, make zero paid submits. Record `python3 "$S/state.py" confirm --slug <slug> --kind shots --note "拆段、prompt、素材、模型、分辨率与费用已确认"`.

### 6. Submit together and record immediately

Submit only the confirmed plan, concurrently: one per shot, `shot=<N>`, unique `<slug>:shot<N>:v1`. All in the same response, then poll together. Split first, then generate. On each return immediately record `$S/state.py shot --job`; keep ids in state, not user text.

Serialize `state.py` writes for the same slug: it updates one JSON file, so concurrent record writes can overwrite each other even though provider jobs run in parallel.

Before that batch, finish all required materials, prompts, explicit durations and exact estimates for its approved scope, and check that the current model/resolution still match the confirmed plan. For Turbo frame modes, every assigned asset must exist and be accepted. Do not launch shot 1 while still preparing shot 2's frames, or serialize shots by extracting each generated video's ending as the next input. Submit within the channel's concurrency limits; a priced sample approves only that sample's scope. Completed shots may enter quality checks while other submitted jobs finish.

A timeout is normal, and a paid task is never replaced. On `polling_paused=true`, end the turn with a human status and later resume the same job; never resubmit or cancel because polling paused.

A true failure's first corrective idea is one unchanged retry. It is another paid shoot: first report affected shot count, seconds, unchanged model/resolution, and fresh estimate, and obtain explicit confirmation. Only after that retry fails propose a targeted prompt change.

### 7. Transcribe and compare — card 3

For every current take:

```bash
"$S/extract_audio.sh" shot1.mp4 shot1.mp3
# share audio without attaching the intermediate file, then transcribe it
python3 "$S/compare_transcript.py" --intended "<逐字台词>" --heard "<实际转写>"
```

Measure actual duration. Before card 3 show every playable shot link, complete intended line, complete actual words, planned/actual seconds, similarity, substitutions/omissions, and verdict. Mark `🔴` if similarity is suspect, any substitution exists, or `abs(actual - planned) > max(2s, planned × 25%)`.

For a multi-shot cut, also review each join as described in `references/quality.md`: adjacent actual images, motion, voice and pauses. Shared planned frames constrain endpoints but do not prove a seamless join or a consistent voice. Include join findings in card 3; never report an unobserved visual/audio property as checked.

The person chooses affected shots to regenerate or accept. Before a paid regeneration, show incremental shot count, seconds, unchanged/current model and resolution, and estimate. Record acceptance with `python3 "$S/state.py" shot --slug <slug> --index <N> --accept "<用户接受理由>"`. Regenerate only affected shots. One shot has exactly one current take; `:v2` supersedes `:v1`.

### 8. Compose, check, deliver

Captions use the accepted actual transcript, never the written line. Two paths:

- **Plain cut (free):** `$S/build_ass.py`, `$S/compose.sh`, then `python3 "$S/state.py" check --slug <slug> --final final.mp4`. Resolve or explain advisory findings: drift, missing jobs/audio, shot duration deviation, final audio, and final duration versus measured shot sum. Deliver only through `share_file`.
- **Cut with effects (costs credits):** write the timeline JSON from the accepted takes' asset_ids and the accepted transcript (`references/compose-timeline.md`), `video_compose(action="validate")`, then show shots, captions, banners, transitions, `duration_sec` and the exact `estimated_credits` in prose and invoke **card 4**: `可以` / `改字幕或时间` / `换转场或动效` / `改用无特效拼接（ffmpeg）`. Only after `可以`: `submit` with `<slug>:compose:v1`, `wait` on the job_id, report the returned `credits=` — as 已扣积分 only when the result says `billing_mode=enforce`; with `billing_mode=shadow` call it 统计消耗 and say no credits were deducted. The result is attached by the tool; `share_file` is not needed.

## Invalidation and samples

- Script changed → identify `受影响段`; splitting and prompts are stale; rebuild and repeat card 2.
- Segment/prompt/material changed → that shot and combined price are stale; keep unaffected generated shots.
- Shared Turbo boundary changed → update every shot's `assets` mapping that uses it (both neighbours for an internal boundary), repeat image acceptance, and rebuild those requests and the combined quote before repeating card 2. A preparation-note change alone does not update shot hashes.
- Composer model/resolution changed → read models again; splitting, capability-dependent prompts, and estimate are stale; repeat card 2.
- `$S/state.py check` names affected shots; it advises, it never blocks and exits 0.
- “先做一段样片” confirms only that priced sample. Remaining shots need their own later card 2.

## What actually goes wrong

- Presenter/hair changes: reuse original material and exact base; prefer video; a stable model seed may help when supported.
- Words change: always STT; one-character substitutions can keep high similarity and invert meaning.
- An extra “艾特” before the line: an `@` reached the model. The line belongs after `口播台词：` with no `@` anywhere; fix the prompt before a paid retry, since an unchanged one repeats it.
- Background changes: anchor it and avoid decorative overload.
- Generated text is wrong: require `无字幕`; burn accepted STT words later.
- Captions overflow or joins drift: use `$S/build_ass.py` and `$S/compose.sh`, not an ad-hoc concat.
- Identity material is rejected: use the product's own authorisation flow; never externalise or bypass it.
- Selected tier ignores a reference: disclose and offer choices; never continue silently.
- Project seems missing: read state/workspace; do not recreate paid work.
- Final has no audio: catch it with `$S/state.py check` before delivery.
- Parameter error: re-read models and estimate; it does not imply the tool is broken.
- Estimate differs from measured output: preserve the quote, then report actual duration honestly.

## Reference

- `references/prompt-recipes.md` — rules, roles, examples, compliance and script structure
- `references/model-guide.md` — selected-model discipline, references, duration and cost
- [references/minimax-turbo-frames.md](references/minimax-turbo-frames.md) — read for the selected Turbo model: material choice, complete frame preparation, shared boundaries, state and parallel video generation
- `references/quality.md` — pathology, STT, duration acceptance and composition checks
- `references/compose-timeline.md` — the `video_compose` timeline format, card 4, verified effects and billing

Publishing/posting is handled by the `douyin-desktop-publish` skill (load it with the `skill` tool when the person wants the video on Douyin — it publishes through the cloud desktop's logged-in 创作者中心 and only falls back to the QR package when that route is switched off); this skill produces and delivers the file.
