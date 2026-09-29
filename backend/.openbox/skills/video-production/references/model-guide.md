# Choosing a model, and holding a look steady

`video_generate(action="models")` prints the live registry — ids, resolutions,
ratios, duration ranges and which extras each model accepts. **Read it there.**
The relay publishes no model list of its own, so the registry is the only
description that exists, and a copy in this file would drift.

If the output contains `person_selected_model=<id>`, the person has already
chosen. That model and the composer resolution are authoritative, even when a
different entry looks cheaper, faster, or appears first in the registry. If the
choice cannot satisfy the request, explain the exact capability conflict and
ask the person to switch it in the composer; never submit a substitute. The
recommendations below apply only when there is no person-selected model, or
when the person explicitly asks for help choosing one.

What the registry cannot tell you:

| Situation | Choose |
|---|---|
| Default vertical talking head | The configured default from the live registry, within its current limits |
| A quick look at whether an idea works | The cheapest fast tier at 480p, then regenerate the keeper at full resolution |
| Final delivery | A 1080p tier |
| The shot uses a reference **video** | Not the 720p tier — it drops video references upstream, and you pay for a take that ignored them |

Always `action="estimate"` first on anything unusual. It runs the full
validation and costs nothing.

## The selected model is binding

The model menu's second line may contain `person_selected_model=<id>`, backed
by the model and resolution selected in the composer session. Treat that as a
creative premise, not a default that may be improved or replaced silently.

- Split and run `plan_shots.py` with that model's live duration floor and
  ceiling. Seedance is at most 15s per shot (about 55 Chinese characters at a
  normal pace); Wan 3.0 is at most 30s; SD tiers follow the registry.
- State “按你选的 <模型> <分辨率> 规划” on the complete shot card.
- If the selection cannot carry a line, resolution, or requested reference,
  explain the exact incompatibility and offer model/material choices. Only the
  person changes the selection; never silently change model or tier.
- A mid-session selection change invalidates splitting, prompts affected by
  capabilities, and the estimate. Read the menu again and show a new shot card.
- With no `person_selected_model`, use the registry default and label that fact
  on the card.

Reference material has its own compatibility constraints. The 720p SD tier
drops video references upstream; propose 1080p or different material rather
than paying for a take that ignores the video. An image/video reference should
have aspect ratio 0.4–2.5. Outside that range, explain that a centered crop or
different asset is needed; do not upload it elsewhere or tunnel around access.

## Keeping the presenter identical across shots

Use only techniques supported by the selected model:

1. **The same original person video on every shot**, where the selected model
   supports it. A video exposes multiple angles and is generally a stronger
   identity anchor than one image. The 720p SD tier is not eligible because it
   drops video references.
2. **The same original reference image on every shot** when video is absent or
   unsupported, with a prompt that describes the *action* rather than
   re-describing the person. "画面中的人物自然看向镜头" beats a paragraph
   about her face and clothes — a full description competes with the image.
3. **One `seed` reused across shots**, on a model that accepts one. Same seed
   plus same anchor removes most of the remaining drift, for free.
4. **A shared, prepared boundary image as shot N's `last_frame` and shot N+1's
   `first_frame`**, on a model that accepts frame roles. Prepare and accept the
   images before submitting either video so the shots can run concurrently.
   This constrains endpoints; inspect actual joins rather than promising
   identical motion or voices.

What not to do: never automatically feed a **generated video's** still back as
the general character reference. Repeatedly deriving identity from altered
outputs can accumulate drift. Purpose-made images requested and accepted as
first/last frames are valid inputs; keep their common identity source stable.

If a role is declared by the model but the gateway cannot carry it, the tool
says so explicitly rather than quietly downgrading it to a plain reference.
That refusal is a real answer: explain the incompatibility and use only an
alternative that the selected model actually supports and the person accepts.

## MiniMax-H3-Max-Turbo material preparation

For this exact effective model, follow
[minimax-turbo-frames.md](minimax-turbo-frames.md). Resolve the person's material
choice early, then confirm the script and split it before preparing the images.
Finish the entire approved batch's frames, prompts and quotes before submitting
any video. A same-scene N-shot first/last-frame plan normally uses N+1 images,
sharing the actual boundary asset between neighbours so videos can run in parallel.

This route accepts explicit `first_frame` and optional `last_frame`, not generic
image/video/audio references. Image requests use `ratio="adaptive"`; text-only
requests need an explicit ratio. It currently uses explicit 5–15s durations and
480p/768p tiers, without seed or smart duration; read `models` and `estimate` for
current limits rather than applying the separate MiniMax H3 rows below. A 720×1280
input picture does not select a 720p video tier. First/last frames do not lock the
voice, so include audio continuity in join review.

## Cost

Use each exact request's free `estimate` for the planned cost. This spoken-video
workflow always supplies an explicit `duration`, including a one-shot cut;
support for smart duration on another model is not a reason to use `-1` here.
The daily ceiling is back-pressure, not permission: if it refuses, tell the
person rather than retrying.


## Shot length per model

Every range below was probed at both edges and the output file measured — a
gateway that accepts a value and ignores it looks identical at submit time.
Out-of-range values are refused outright, never clamped, so a wrong number
costs a whole submit:

| model | probed | delivered |
|---|---|---|
| Seedance 2.0 | 3 ✗ · 4 ✓ · 15 ✓ · 16 ✗ | -1 → 12.05s (model chose) |
| Wan 3.0 | 2 ✓ · 30 ✓ · 31 ✗ | 20 → 20.04s · 30 → 30.02s |
| MiniMax H3 | 3 ✗ · 4 ✓ · 7 ✓ · 15 ✓ · 16 ✗ | 4 → 4.46s · 7 → 7.30s · 15 → 15.08s |

Delivered length runs a fraction over the request (encoder rounding), never
under.


| model | seconds | smart (-1) |
|---|---|---|
| Seedance 2.0 / 2.0 Fast | 4–15 | yes |
| SD 480p / 720p / 1080p | 4–15 | **no** |
| Wan 3.0 / Prime | 2–30 | yes |
| MiniMax H3 | 4–15 | no |

### Use an explicit duration for anything that gets cut together

Outside this workflow, a model that supports `-1` may choose a standalone clip's
length. This workflow uses explicit seconds for every shot. In a cut,
a length the model chose per shot means the total drifts from the script, the
shots do not sit at the rhythm the writing implies, and the captions built
from each transcript stop matching where the pauses fall. The planner computes
a number per line precisely so the cut has a shape; passing `-1` throws that
away one shot at a time. Send the number.

`-1` asks the model to choose the length. Where it works it really does
choose: Seedance 2.0 returned 12.05s for a line that would otherwise have got
the 5s default. The three SD tiers accept `-1` and then return exactly 5.06s —
indistinguishable from the default, so treat it as unsupported there and send
a number. Wan 3.0's range is 2–30 on this deployment, wider than the 2–15 in
通义万相 2.7's public docs, and 30s was measured as 30.02s of actual video.

Two consequences for splitting. A line that needs less than the model's floor
gets padded up to it — on Seedance a three-character line still occupies 4s,
so merge it into a neighbour instead. And a line needing more than the ceiling
has to be split: 15s at 4 chars/second is about 55 spoken characters, which is
the real upper bound on one Seedance shot.

When the person asks for help choosing a model, a wider live duration range can
fit uneven line lengths. Keep an existing selection binding and read current
capabilities before making a recommendation.
