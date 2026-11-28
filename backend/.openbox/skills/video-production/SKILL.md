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

This file holds the hard rules and the order of work. Read `references/workflow-plan.md` (secretary conversation, preparation, steps 0–3) when a production starts and `references/workflow-produce.md` (steps 4–8, invalidation, known failures) once card 1 is confirmed, with `read` on their absolute paths.

If either workflow file is missing, this desktop has the previous bundle: read `/opt/openbox/skills/video-production/SKILL.md` instead, which still holds those sections in full, and continue; it is not a missing dependency.

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

Steps in order; the workflow files say how.

0. **Read the menu first and resume safely.** `video_generate(action="models")` before planning; on a continuing run `python3 "$S/state.py" show --slug <slug>` and `check`, and resume recorded paid work. `$S/state.py check` names affected shots; it advises, it never blocks and exits 0. Supplied pictures or frame preparation: `references/material-plan.md` for every model, plus `references/minimax-turbo-frames.md` for Turbo.
1. **Creator context.** `creator_context(action="get_user_context")`; empty is normal.
2. **Card 1: the whole script**, fitted to length before the card with step 3's commands, complete with its length in the `question` text. After 「可以」 never rewrite it on your own.
3. **Split at meaning and time against the selected model** with `$S/split_script.py` and `$S/plan_shots.py` (`--min-shot-seconds`, `--max-shot-seconds`). Never divide the requested total by the shot count. Always send an explicit duration; do not use `-1`. Choose `--rate` from the piece you just wrote: calm 3.4, conversational 4.0, energetic 4.6. Seedance takes 4–15s, Wan 3.0 takes 2–30s, MiniMax H3 takes 4–30s; re-read others.
4. **Materials and prompts** (`references/prompt-recipes.md`); one JSON plan, `prepare.py` once. `saved=true` is not approval.
5. **Validate, then card 2** with every line and complete prompt. Tool-call details do not count as display.
6. **Submit each ready shot** with `shot=<N>` and a stable key, other ready shots in the same response; when blocked, call `video_generate(action="await_ready", job_ids=[...])` once with all outstanding IDs. On `polling_paused=true` leave the job in background recovery and end the turn.
7. **Card 3**: words, picture text, action, duration and frame rate of each take (`references/quality.md`). One shot has exactly one current take; `:v2` supersedes `:v1`.
8. **Compose, check, deliver** (`references/compose-timeline.md`). Use the accepted actual transcript for requested speech captions. Use `video_compose(action="await_ready", job_id=...)` once to receive its attached result.

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
- `references/workflow-plan.md` — secretary conversation, preparation, menu and resume, card 1, timing (steps 0–3)
- `references/workflow-produce.md` — prompts, card 2, submission, card 3, composition, invalidation (steps 4–8)

Publishing/posting is handled by the `douyin-desktop-publish` skill (load it with the `skill` tool when the person wants the video on Douyin — it publishes through the cloud desktop's logged-in 创作者中心 and only falls back to the QR package when that route is switched off); this skill produces and delivers the file.
