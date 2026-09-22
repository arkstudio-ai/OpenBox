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
| Default vertical talking head | The configured default (Wan 3.0 today) — widest parameter range: 2–30s, six ratios, seed, first/last frame |
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

In order of strength:

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
4. **`last_frame` of shot N as the `first_frame` of shot N+1**, on a model that
   accepts frame roles. Strongest continuity available, and the honest way to
   do it — the shots genuinely join.

What not to do: never feed a **generated** clip's frame back as the general
character reference. Each generation drifts a little from the anchor; anchoring
to a drifted frame compounds it, and by the fifth shot it is a different person.

If a role is declared by the model but the gateway cannot carry it, the tool
says so explicitly rather than quietly downgrading it to a plain reference.
That refusal is a real answer: use the seed tactic instead.

## Cost

Billing on this route is per second of generated video, so an explicit
`duration` is the whole cost story — `-1` lets the model choose and is the
right default when you do not care, but a wrong guess is what you pay for.
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

`-1` is for a standalone clip, not for a piece assembled from shots. In a cut,
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

Wan 3.0's 2–30 range is the widest by far, and it is the only model that
accepts `-1` for a duration it chooses itself. Prefer it when the script has
lines of very uneven length.
