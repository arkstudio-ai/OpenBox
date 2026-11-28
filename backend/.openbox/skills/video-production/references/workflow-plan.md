# Spoken video production — preparation and steps 0–3

Detail for the `video-production` skill, read when a production starts. The hard rules in SKILL.md still apply. Paths such as `references/…` and `$S/…` resolve exactly as in SKILL.md: under `/opt/openbox/skills/video-production/` in the sandbox.

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

## Workflow

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
